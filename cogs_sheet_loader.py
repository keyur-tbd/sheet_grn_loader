#!/usr/bin/env python3
"""Load the costing team's product COGS (RM / packaging / factory overheads per unit) into public.cogs_rm_pm.

Source: the "Product COGS w/o tax" Google Sheet (1f8FxpGdaw-CdpCxEUZPa0e141as8A03xTvlSvLFtCNQ), one tab per month
("October'26 w/o tax"): BC Item Code | City | Item | Category | RM | Packaging | RM + Pkg | Factory Overheads |
Cost Price. The costing team adds a tab every month and revises the last one, so each run loads the NEWEST TWO month
tabs (--months to load others). It replaces the hand-exported "COGS Data.xlsx" (cogs_pipeline/load_cogs.py).

Birbal migration 223 owns the item_no column; migration 039 the table. What a row becomes:
  * item_no = the BC item code (FG/xxxx, TG/xxxx) -- v_cogs_rate prefers it to matching the product by name;
  * city = the sheet's City, blank = 'ALL' (the national rate; per-city rows win over it);
  * rm = RM, pm = Packaging (it already includes the carton), carton_cost = 0, fo = Factory Overheads;
  * category = the MIS category the product last had in the table (TG items are bought in, so theirs always reads
    OUTSOURCED ...: v_cogs_rate adds FO to the cost of outsourced items only), else the sheet's own Category.
A row the costing team has not costed yet (RM and Packaging blank or zero, or an error such as #REF!) is skipped, and
whatever the table held for that product and month stays. For every product the sheet DOES cost in a month, all of
that month's rows for it are replaced, so an old per-city row cannot shadow the sheet's national rate.

The P&L reads these rates through public.cogs_refresh(), which runs on the o2c beat (09:00 / 14:00 IST, pg_cron);
--refresh runs it right after the load instead.

Environment: SUPABASE_DB_URL (+ SUPABASE_DB_SSLMODE / SUPABASE_DB_SSLROOTCERT),
COGS_SHEET_TOKEN_JSON or STAFF_SALARY_TOKEN_JSON (birbal@'s Sheets token;
default D:\\Python\\Birbal\\salary_loader\\token_birbal.json).

    python cogs_sheet_loader.py --dry-run                 # read + compare with the table, write nothing
    python cogs_sheet_loader.py                           # the newest two month tabs
    python cogs_sheet_loader.py --months 2026-09,2026-10  # these months
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from collections import defaultdict

import psycopg2
from psycopg2.extras import execute_values
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:                                            # noqa: BLE001
    pass

SHEET_ID = '1f8FxpGdaw-CdpCxEUZPa0e141as8A03xTvlSvLFtCNQ'
DEFAULT_TOKEN = r'D:\Python\Birbal\salary_loader\token_birbal.json'
FIRST_MONTH = dt.date(2026, 4, 1)          # FY27: the tabs before it are laid out differently
MIN_COSTED = 20                            # a tab costing fewer products than this is not loaded
MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
TAB_RE = re.compile(r"^\s*([A-Za-z]+)\s*'\s*(\d{2})\s+w/o\s+tax\s*$", re.I)
CODE_RE = re.compile(r'^(FG|TG)/\d+$')

# header (lower-cased, spaces collapsed) -> field. Matched by NAME; a missing one skips the tab.
HEADERS = {
    'bc item code': 'item_no',
    'city': 'city',
    'item': 'sku',
    'category': 'category',
    'rm': 'rm',
    'packaging': 'pm',
    'factory overheads': 'fo',
}

log = logging.getLogger('cogs_sheet')


def sheets_client():
    path = os.environ.get('COGS_SHEET_TOKEN_JSON') or os.environ.get('STAFF_SALARY_TOKEN_JSON') or DEFAULT_TOKEN
    creds = Credentials.from_authorized_user_file(path)
    if not creds.valid:
        creds.refresh(Request())
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def norm(s) -> str:
    return re.sub(r'\s+', ' ', str(s if s is not None else '')).strip()


def num(v):
    if isinstance(v, (int, float)):
        return float(v)
    s = norm(v).replace(',', '')
    try:
        return float(s) if s else None
    except ValueError:
        return None                       # '#REF! ...', '#N/A ...'


def tab_month(title: str):
    m = TAB_RE.match(title)
    if not m or m.group(1)[:3].lower() not in MONTHS:
        return None
    return dt.date(2000 + int(m.group(2)), MONTHS[m.group(1)[:3].lower()], 1)


def month_tabs(svc):
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID, fields='sheets.properties.title').execute()
    out = {}
    for s in meta['sheets']:
        t = s['properties']['title']
        m = tab_month(t)
        if m and m >= FIRST_MONTH:
            if m in out:
                raise SystemExit(f'two tabs for {m:%b %Y}: {out[m]!r} and {t!r}. Refusing to guess.')
            out[m] = t
    return out


def read_tab(svc, title):
    grid = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{title}'", valueRenderOption='UNFORMATTED_VALUE',
    ).execute().get('values', [])
    if not grid:
        return None, {'empty tab': 1}
    idx = {}
    for i, h in enumerate(grid[0]):
        k = norm(h).lower()
        if k in HEADERS and HEADERS[k] not in idx:
            idx[HEADERS[k]] = i
    missing = sorted(set(HEADERS.values()) - set(idx))
    if missing:
        return None, {f'header(s) not found: {", ".join(missing)}': 1}
    rows, skipped = {}, defaultdict(int)
    for row in grid[1:]:
        cell = lambda f: row[idx[f]] if idx[f] < len(row) else None   # noqa: E731
        code = norm(cell('item_no')).upper()
        if not CODE_RE.match(code):
            if code:
                skipped['not an item code'] += 1
            continue
        rm, pm, fo = num(cell('rm')), num(cell('pm')), num(cell('fo'))
        if not ((rm or 0) > 0 or (pm or 0) > 0):
            skipped['not costed yet'] += 1
            continue
        sku = norm(cell('sku')).upper()
        if not sku:
            skipped['no item name'] += 1
            continue
        city = norm(cell('city')).upper() or 'ALL'
        rec = {'item_no': code, 'city': city, 'sku': sku, 'sheet_category': norm(cell('category')),
               'rm': round(rm or 0.0, 6), 'pm': round(pm or 0.0, 6), 'fo': round(fo or 0.0, 6)}
        k = (city, sku)
        if k in rows:
            if (rows[k]['item_no'], rows[k]['rm'], rows[k]['pm'], rows[k]['fo']) != (code, rec['rm'], rec['pm'], rec['fo']):
                skipped['same product + city twice, different rates (first kept)'] += 1
            continue
        rows[k] = rec
    return list(rows.values()), dict(skipped)


def connect():
    dsn = os.environ['SUPABASE_DB_URL']
    kw = dict(sslmode=os.environ.get('SUPABASE_DB_SSLMODE', 'require'))
    if os.environ.get('SUPABASE_DB_SSLROOTCERT'):
        kw['sslrootcert'] = os.environ['SUPABASE_DB_SSLROOTCERT']
    return psycopg2.connect(dsn, keepalives=1, keepalives_idle=30, keepalives_interval=10, **kw)


def categorise(rows, known):
    """The MIS category the product last had; a bought-in (TG) item always reads OUTSOURCED."""
    for r in rows:
        prev = known.get(r['sku'])
        outsourced = r['item_no'].startswith('TG/')
        if prev and (prev.upper().startswith('OUTSOURCED') == outsourced):
            r['category'] = prev
        else:
            base = (r['sheet_category'] or 'UNCATEGORISED').upper()
            r['category'] = ('OUTSOURCED ' + base) if outsourced and not base.startswith('OUTSOURCED') else base


COLS = ['month', 'city', 'sku', 'category', 'rm', 'pm', 'carton_cost', 'fo', 'item_no', 'source_file']


def load_month(cur, month, title, rows, dry):
    cur.execute("""select city, sku, rm::float8, pm::float8, coalesce(carton_cost, 0)::float8, fo::float8
                     from public.cogs_rm_pm where month = %s""", (month,))
    before = {(c, s): (rm, pm, ct, fo) for c, s, rm, pm, ct, fo in cur.fetchall()}
    skus = sorted({r['sku'] for r in rows})
    changed = sum(1 for r in rows
                  if (lambda b: not b or abs(b[0] - r['rm']) > 1e-4 or abs(b[1] + b[2] - r['pm']) > 1e-4 or abs((b[3] or 0) - r['fo']) > 1e-4)(
                      before.get((r['city'], r['sku']))))
    replaced = sum(1 for (c, s) in before if s in set(skus))
    log.info('%s (%s): %d rates for %d products; %d new or changed; replaces %d of %d rows held',
             f'{month:%b %Y}', title, len(rows), len(skus), changed, replaced, len(before))
    if dry or (changed == 0 and replaced == len(rows)):
        return changed                    # the 3-hourly run writes only when the sheet moved
    cur.execute('delete from public.cogs_rm_pm where month = %s and sku = any(%s)', (month, skus))
    execute_values(cur, f'insert into public.cogs_rm_pm ({", ".join(COLS)}) values %s',
                   [(month, r['city'], r['sku'], r['category'], r['rm'], r['pm'], 0.0, r['fo'], r['item_no'],
                     f'sheet:{title}') for r in rows], page_size=500)
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--months', help='comma-separated YYYY-MM (default: the newest two month tabs)')
    ap.add_argument('--refresh', action='store_true', help='run public.cogs_refresh() after loading')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    svc = sheets_client()
    tabs = month_tabs(svc)
    if args.months:
        want = [dt.date(int(m[:4]), int(m[5:7]), 1) for m in args.months.split(',')]
        missing = [m for m in want if m not in tabs]
        if missing:
            raise SystemExit('no tab for ' + ', '.join(f'{m:%b %Y}' for m in missing))
    else:
        want = sorted(tabs)[-2:]
    work = []
    for m in want:
        rows, skipped = read_tab(svc, tabs[m])
        if rows is None or len(rows) < MIN_COSTED:
            log.warning('%s (%s): not loaded (%s)', f'{m:%b %Y}', tabs[m], skipped or f'{len(rows or [])} costed rows')
            continue
        if skipped:
            log.info('%s: skipped %s', tabs[m], skipped)
        work.append((m, tabs[m], rows))
    if not work:
        log.info('nothing to load')
        return 0

    conn = connect()
    total = 0
    try:
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = '300000'")
            cur.execute("""select distinct on (sku) sku, category from public.cogs_rm_pm
                            where category is not null order by sku, month desc""")
            known = dict(cur.fetchall())
            for m, title, rows in work:
                categorise(rows, known)
                total += load_month(cur, m, title, rows, args.dry_run)
            if not args.dry_run:
                try:
                    cur.execute("select 1 from information_schema.tables where table_schema='public' and table_name='workflow_logs'")
                    if cur.fetchone():
                        cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) "
                                    "values (%s, %s, now(), now(), 'success', %s::jsonb, %s, %s, now())",
                                    ('sheet_grn_loader:cogs_sheet', 'cogs_sheet',
                                     json.dumps({'tabs': [t for _, t, _ in work], 'changed': total}),
                                     sum(len(r) for _, _, r in work), sum(len(r) for _, _, r in work)))
                except Exception as e:                        # noqa: BLE001
                    log.warning('workflow_logs not written: %s', str(e)[:100])
        if args.dry_run:
            conn.rollback()
            log.info('dry run: nothing written')
            return 0
        conn.commit()
        if args.refresh and total:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("set statement_timeout = '900000'")
                cur.execute('select public.cogs_refresh()')
                log.info('cogs_refresh: %s', cur.fetchone()[0])
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
