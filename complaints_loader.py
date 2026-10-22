#!/usr/bin/env python3
"""Customer complaints from the "Query/Complaint Tracker" sheet -> public.crm_complaints + public.crm_foscos_complaints
(Birbal migration 204, the /complaints board).

    python complaints_loader.py              # read every month tab + the FoSCoS tab, replace both tables
    python complaints_loader.py --dry-run    # parse and print counts per tab, write nothing (no database needed)

The workbook IS the customer-care log: one tab per month ("Oct'26", "Sept'26", ... "March'22", plus "From Nov'24",
which holds Nov AND Dec 2024), one row per complaint or query, typed in by the customer-care team from WhatsApp,
email, trade stores, the marketplaces' escalations and FSSAI. Column ORDER differs between tabs and headers are
spelled several ways, so every column is found by its normalised header name (HEADERS below); the header row is
whichever of the first rows names "Product Name" (row 1 holds a count on newer tabs). A row with no product is a
blank, a VLOOKUP #N/A tail or a non-product query (job vacancies, store numbers) and is skipped.

PRIVACY. The customer's Name and Number / Contact columns are NEVER read into a row, stored or logged. Free text
(description, resolution, the FoSCoS customer discussion) has every run of 10+ digits replaced by "[number]" and
every e-mail address by "[email]".

Dates. The sheet mixes serial dates, '1-October-26', '5-Jan-2026', '03.09.23', '12 Aug- 14 Aug' and 'JUNE'.
What parses becomes complaint_date (a date implausibly far from the tab's month is dropped, a day/month swap is
tried first); the tab's month is ALWAYS stored as `month` and is the reporting month. On the "From Nov'24" tab the
month is the complaint date's month when that falls in the tab's span.

"FoSCoS - Open complaints" is the regulatory log: complaints lodged against us on FSSAI's FoSCoS portal, keyed by
ticket number.

A read with fewer than MIN_ROWS complaints or MIN_MONTHS month tabs is treated as broken (a renamed tab, a sharing
change), not as deletions: nothing is written. Otherwise both tables are replaced in one transaction.

Two Sheets API requests per run (the tab list, one batchGet of every tab we read): the read quota is per user per
minute and every instamart@ job of the workflow shares it (see targets_loader.with_retry).

Environment: as targets_loader.py (SUPABASE_DB_URL, GOOGLE_TOKEN_JSON = the instamart@ token, which can read the
sheet). COMPLAINTS_SHEET_ID overrides the workbook.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sys
from collections import Counter

from googleapiclient.discovery import build

from targets_loader import creds, with_retry

log = logging.getLogger('complaints')

SHEET_ID = os.environ.get('COMPLAINTS_SHEET_ID', '1F2OxZ07zN4_TAozATOGOr_Zn5_Rc8HK98lr_jIlEOnM')
FOSCOS_TAB = 'FoSCoS - Open complaints'
MIN_ROWS = 5000                  # 8,491 product rows on 2026-10-22
MIN_MONTHS = 30                  # 54 month tabs on 2026-10-22

MONTHS = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
MONTH_TAB = re.compile(r"^(from\s+)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*'(\d\d)\s*$", re.I)


def norm(h) -> str:
    """header -> key: lower case, one space, no trailing punctuation ('Batch No.' -> 'batch no')."""
    s = re.sub(r'\s+', ' ', str(h or '').strip().lower())
    return re.sub(r'[\s.:;,?]+$', '', s)


# normalised header -> field. Name / Number / Contact / Order ID are deliberately absent: never read.
HEADERS = {
    'date': 'date_raw',
    'product name': 'product',
    'category': 'category',
    'qty': 'qty', 'quantity': 'qty', 'no. of pieces/ quantity': 'qty', 'no. of pieces': 'qty',
    'batch no': 'batch_no',
    'mfg date': 'mfg_raw',
    'issue': 'issue',
    'city': 'location', 'location': 'location', 'loction': 'location', 'store name/ location': 'location', 'store name': 'location',
    'source': 'source',
    'platform': 'platform',
    'exp': 'exp_raw', 'use by/ exp': 'exp_raw', 'use by date/ exp': 'exp_raw',
    'description': 'description', 'detailed description': 'description',
    'response/ resolution/ comment': 'resolution', 'response/ resolution': 'resolution', 'corrective action': 'resolution',
    'respinse/ resolutiin': 'resolution',
    'no. of carts (sp. for blinkit)': 'carts',
    'rca link (if provided)': 'rca_link',
    'complaint / query': 'kind', 'type': 'kind',
    'complaint / query type': 'query_type',
}
COLUMNS = ['tab', 'sheet_row', 'month', 'tab_month', 'complaint_date', 'date_raw', 'product', 'category', 'qty', 'batch_no',
           'mfg_date', 'exp_date', 'exp_raw', 'issue', 'kind', 'query_type', 'location', 'source', 'platform',
           'description', 'resolution', 'carts', 'rca_link']

FOSCOS_HEADERS = {
    'date': 'entry_date', 'ticket no': 'ticket_no', 'priority': 'priority', 'complaint date': 'complaint_date',
    'processed date': 'processed_date', 'has tbd responded': 'tbd_responded', 'rca shared': 'rca_shared',
    'any contact with customer': 'customer_contacted', 'details of customer discussion': 'customer_discussion',
    'status': 'status', 'action by fssai': 'fssai_action', 'action required by tbd': 'tbd_action',
}
FOSCOS_COLUMNS = ['ticket_no', 'sheet_row', 'entry_date', 'priority', 'complaint_date', 'processed_date', 'tbd_responded',
                  'rca_shared', 'customer_contacted', 'customer_discussion', 'status', 'fssai_action', 'tbd_action']

PHONE = re.compile(r'\+?\d(?:[\s\-]?\d){9,}')
EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')


def mask(v) -> str | None:
    s = text(v, 4000)
    if s is None:
        return None
    return EMAIL.sub('[email]', PHONE.sub('[number]', s))


def text(v, cap=400) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = re.sub(r'[ \t]+', ' ', str(v)).strip()
    if not s or s in ('-', '--', 'NA', 'N/A', '#N/A') or s.startswith('#N/A'):
        return None
    return s[:cap]


def number(v) -> float | None:
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        m = re.match(r'^\s*(\d+(?:\.\d+)?)', str(v or ''))
        if not m:
            return None
        n = float(m.group(1))
    return n if 0 <= n < 10000 else None          # a 5-digit "quantity" is a date serial typed in the wrong column


def first_of(d: dt.date) -> dt.date:
    return d.replace(day=1)


def add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


def _ymd(y, mo, d):
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def parse_date(v, anchor: dt.date, before_days=400, after_days=62, latest: dt.date | None = None) -> dt.date | None:
    """A sheet date -> a date near `anchor` (the tab's month), or None; never after `latest` when given. Indian
    order (day first); a day/month swap is used only when the first reading falls outside the window and the
    swapped one inside it -- also for a serial date, which Sheets reads month-first when someone types 11-08-2026."""
    lo, hi = anchor - dt.timedelta(days=before_days), add_months(anchor, 1) + dt.timedelta(days=after_days)
    if latest is not None:
        hi = min(hi, latest)

    def ok(d):
        return d is not None and lo <= d <= hi

    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if 20000 < v < 80000:
            d = dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
            if ok(d):
                return d
            d2 = _ymd(d.year, d.day, d.month) if d.day <= 12 else None
            return d2 if ok(d2) else None
        return None
    s = str(v or '').strip().lower()
    if not s:
        return None
    # dd.mm.yy, dd/mm/yyyy, dd-mm-yy
    m = re.match(r'^(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{2}|\d{4})\b', s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
        d = _ymd(y, b, a)
        if ok(d):
            return d
        d2 = _ymd(y, a, b)
        return d2 if ok(d2) else None
    # 1-October-26, 5-Jan-2026, 12 Aug- 14 Aug (the first day of a range), 9 Sep - 9 Sep 2026
    m = re.match(r'^(\d{1,2})\s*[- ]?\s*([a-z]{3,9})\.?\s*(?:[-, ]\s*(\d{2}|\d{4})\b(?!\s*[a-z]))?', s)
    if m and m.group(2)[:3] in MONTHS:
        mo = MONTHS[m.group(2)[:3]]
        y = m.group(3)
        if y is None:
            y4 = re.search(r'\b(20\d\d)\b', s)
            y = int(y4.group(1)) if y4 else None
        else:
            y = int(y)
            y = y + 2000 if y < 100 else y
        cands = [y] if y else [anchor.year, anchor.year - 1, anchor.year + 1]
        for yy in cands:
            d = _ymd(yy, mo, int(m.group(1)))
            if ok(d):
                return d
        return None
    # October 1, 2026
    m = re.match(r'^([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$', s)
    if m and m.group(1)[:3] in MONTHS:
        d = _ymd(int(m.group(3)), MONTHS[m.group(1)[:3]], int(m.group(2)))
        return d if ok(d) else None
    return None


def parse_expiry(v, anchor: dt.date) -> dt.date | None:
    """an EXP / MFG cell: a date, or "Aug'26" (the first of that month); within three years of the tab month."""
    d = parse_date(v, anchor, before_days=3 * 366, after_days=3 * 366)
    if d:
        return d
    m = re.match(r"^([a-z]{3,9})\s*'\s*(\d\d)$", str(v or '').strip().lower())
    if m and m.group(1)[:3] in MONTHS:
        d = dt.date(2000 + int(m.group(2)), MONTHS[m.group(1)[:3]], 1)
        return d if abs((d - anchor).days) <= 3 * 366 else None
    return None


def tab_month(title: str) -> dt.date | None:
    m = MONTH_TAB.match(title.strip())
    if not m:
        return None
    return dt.date(2000 + int(m.group(3)), MONTHS[m.group(2).lower()[:3]], 1)


def header_row(grid, want):
    for i, r in enumerate(grid[:6]):
        if any(norm(x) == want for x in r):
            return i
    return None


def parse_month_tab(title: str, grid: list, stats: Counter) -> list[tuple]:
    tm = tab_month(title)
    spans = title.strip().lower().startswith('from')
    hi = header_row(grid, 'product name')
    if hi is None:
        raise ValueError(f"{title}: no 'Product Name' header in the first rows")
    idx = {}
    for j, h in enumerate(grid[hi]):
        f = HEADERS.get(norm(h))
        if f and f not in idx:                   # the first of a repeated header wins
            idx[f] = j
    if 'product' not in idx:
        raise ValueError(f'{title}: no product column')
    out = []
    for i, r in enumerate(grid[hi + 1:], start=hi + 2):          # sheet_row is the spreadsheet's own row number
        cell = lambda f: r[idx[f]] if f in idx and idx[f] < len(r) else None   # noqa: E731
        product = text(cell('product'), 200)
        if not product:
            continue
        d = parse_date(cell('date_raw'), tm)
        stats['dated' if d else 'undated'] += 1
        month = tm
        if spans and d and tm <= first_of(d) < add_months(tm, 3):
            month = first_of(d)
        raw_date = cell('date_raw')
        out.append((
            title, i, month, tm, d,
            None if isinstance(raw_date, (int, float)) or raw_date is None else text(raw_date, 60),
            product, text(cell('category'), 60), number(cell('qty')), text(cell('batch_no'), 60),
            parse_expiry(cell('mfg_raw'), tm), parse_expiry(cell('exp_raw'), tm),
            None if isinstance(cell('exp_raw'), (int, float)) else text(cell('exp_raw'), 60),
            text(cell('issue'), 120), text(cell('kind'), 60), text(cell('query_type'), 120),
            text(cell('location'), 120), text(cell('source'), 60), text(cell('platform'), 60),
            mask(cell('description')), mask(cell('resolution')), number(cell('carts')), text(cell('rca_link'), 500),
        ))
    return out


def parse_foscos(grid: list) -> list[tuple]:
    hi = header_row(grid, 'ticket no')
    if hi is None:
        raise ValueError(f"{FOSCOS_TAB}: no 'Ticket no' header in the first rows")
    idx = {}
    for j, h in enumerate(grid[hi]):
        f = FOSCOS_HEADERS.get(norm(h))
        if f and f not in idx:
            idx[f] = j
    today = dt.date.today()
    rows = {}
    for i, r in enumerate(grid[hi + 1:], start=hi + 2):
        cell = lambda f: r[idx[f]] if f in idx and idx[f] < len(r) else None   # noqa: E731
        ticket = text(cell('ticket_no'), 40)
        if not ticket:
            continue
        pd = lambda f: parse_date(cell(f), first_of(today), before_days=5 * 366, latest=today)   # noqa: E731
        rows[ticket] = (ticket, i, pd('entry_date'), text(cell('priority'), 40), pd('complaint_date'), pd('processed_date'),
                        text(cell('tbd_responded'), 40), text(cell('rca_shared'), 40), text(cell('customer_contacted'), 40),
                        mask(cell('customer_discussion')), text(cell('status'), 120), mask(cell('fssai_action')),
                        mask(cell('tbd_action')))
    return list(rows.values())


def read_sheet(svc):
    meta = with_retry(lambda: svc.spreadsheets().get(spreadsheetId=SHEET_ID, fields='sheets.properties.title').execute(), 'tab list')
    titles = [s['properties']['title'] for s in meta['sheets']]
    months = [t for t in titles if tab_month(t)]
    want = months + ([FOSCOS_TAB] if FOSCOS_TAB in titles else [])
    res = with_retry(lambda: svc.spreadsheets().values().batchGet(
        spreadsheetId=SHEET_ID, ranges=[f"'{t}'!A1:AH" for t in want], valueRenderOption='UNFORMATTED_VALUE').execute(),
        f'{len(want)} tabs')['valueRanges']
    grids = {t: vr.get('values', []) for t, vr in zip(want, res)}
    stats = Counter()
    complaints, per_tab = [], []
    for t in months:
        rows = parse_month_tab(t, grids[t], stats)
        complaints.extend(rows)
        per_tab.append((t, tab_month(t), len(rows)))
    foscos = parse_foscos(grids[FOSCOS_TAB]) if FOSCOS_TAB in grids else None
    return complaints, per_tab, foscos, stats


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    svc = build('sheets', 'v4', credentials=creds(os.environ.get('GOOGLE_TOKEN_JSON', 'token.json')), cache_discovery=False)
    complaints, per_tab, foscos, stats = read_sheet(svc)
    for t, m, n in sorted(per_tab, key=lambda x: x[1], reverse=True):
        log.info('%-14s %s %5d rows', t, m.isoformat()[:7], n)
    log.info('%d complaint rows from %d month tabs (%d with a parsed date, %d without); FoSCoS: %s tickets',
             len(complaints), len(per_tab), stats['dated'], stats['undated'], 'no tab' if foscos is None else len(foscos))
    if len(complaints) < MIN_ROWS or len(per_tab) < MIN_MONTHS:
        log.error('only %d rows from %d month tabs (expected at least %d from %d); refusing to replace the table',
                  len(complaints), len(per_tab), MIN_ROWS, MIN_MONTHS)
        return 1
    if foscos is None:
        log.warning("no '%s' tab: the FoSCoS tickets are left as they are", FOSCOS_TAB)
    if a.dry_run:
        log.info('dry run: nothing written')
        return 0

    from psycopg2.extras import execute_values
    from targets_loader import connect
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = '300000'")
            cur.execute('delete from public.crm_complaints where true')
            execute_values(cur, f"insert into public.crm_complaints ({', '.join(COLUMNS)}) values %s", complaints, page_size=1000)
            if foscos is not None:
                cur.execute('delete from public.crm_foscos_complaints where true')
                if foscos:
                    execute_values(cur, f"insert into public.crm_foscos_complaints ({', '.join(FOSCOS_COLUMNS)}) values %s", foscos)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log.info('loaded %d complaints%s', len(complaints), '' if foscos is None else f' and {len(foscos)} FoSCoS tickets')
    return 0


if __name__ == '__main__':
    sys.exit(main())
