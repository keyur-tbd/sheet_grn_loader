#!/usr/bin/env python3
"""Load the warehouse team's cancellation log into public.o2c_cancel_reasons (Birbal migration 236).

Source: "All WH Cancellation Data April 2026. March 2027" (1MHVWYAddQLJq3GnH2VhyPkwJyvAXFuEyRteRt_ckPOc), tab
"Master Data": one row per cancelled invoice, every warehouse --
  Wh | PO Number | CN DTAE | OLD INVOICE NUMBER | OLD INVOICE DATE | OLD INVOICE AMOUNT | NEW INVOICE NUMBER |
  NEW INVOICE DATE | NEW INVOICE AMOUNT | Reason Category | CANCEL REMARKS | Approver's Nam
The month tabs and pivots are the team's working copies; only Master Data is read. Birbal shows the reason on
SCM > RTV > Cancellations, joined on the cancelled invoice (OLD INVOICE NUMBER).

What a row becomes: invoice_no = OLD INVOICE NUMBER upper-cased (a row without one, or without a Reason Category, is
skipped); bu_code from the invoice's own prefix (27MUM- -> MUMWH, the ERP's
warehouse: Wh is sometimes mistyped, 27BLR-03266 says KOL), else from Wh; dates as typed ("01-April-26",
"13-Mar-26", "9June-26") or real sheet dates; amounts with or without commas. An invoice typed twice keeps the LATER
row (the team's correction). The whole tab replaces the table, written only when it moved; a read of fewer than
MIN_ROWS rows is refused, so a half-cleared tab cannot wipe the reasons.

Environment: SUPABASE_DB_URL (+ SUPABASE_DB_SSLMODE / SUPABASE_DB_SSLROOTCERT), CANCEL_REASON_SHEET_ID (override),
CANCEL_REASON_TOKEN_JSON or STAFF_SALARY_TOKEN_JSON (birbal@'s Sheets token; the sheet is shared with birbal@;
default D:\\Python\\Birbal\\salary_loader\\token_birbal.json).

    python cancel_reason_loader.py --dry-run     # read + compare with the table, write nothing
    python cancel_reason_loader.py               # replace public.o2c_cancel_reasons when the tab moved
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from collections import Counter

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

SHEET_ID = os.environ.get('CANCEL_REASON_SHEET_ID', '1MHVWYAddQLJq3GnH2VhyPkwJyvAXFuEyRteRt_ckPOc')
TAB = 'master data'                        # matched trimmed + lower-cased (the tab is "Master Data ")
DEFAULT_TOKEN = r'D:\Python\Birbal\salary_loader\token_birbal.json'
MIN_ROWS = 200
MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
DATE_RE = re.compile(r'^\s*(\d{1,2})\s*[-/ ]?\s*([A-Za-z]{3,9})\s*[-/ ]?\s*(\d{2}|\d{4})\s*$')
DMY_RE = re.compile(r'^\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})\s*$')
INVOICE_RE = re.compile(r'^\d{2}([A-Z]{3})-\d+')
WH_CODE = {'KOL': 'KOLWH', 'MUM': 'MUMWH', 'BLR': 'BLRWH', 'GUR': 'GURWH', 'HYD': 'HYDWH', 'AHM': 'AHDWHN', 'AHD': 'AHDWHN'}

# header (lower-cased, spaces collapsed) -> field; 'approver' matches by prefix ("Approver's Nam").
HEADERS = {
    'wh': 'wh',
    'po number': 'po_number',
    'cn dtae': 'cn_date', 'cn date': 'cn_date',
    'old invoice number': 'invoice_no',
    'old invoice date': 'invoice_date',
    'old invoice amount': 'invoice_amount',
    'new invoice number': 'new_invoice_no',
    'new invoice date': 'new_invoice_date',
    'new invoice amount': 'new_invoice_amount',
    'reason category': 'reason_category',
    'cancel remarks': 'reason',
}
REQUIRED = ('invoice_no', 'reason_category', 'reason')
COLS = ['invoice_no', 'wh', 'bu_code', 'po_number', 'cn_date', 'invoice_date', 'invoice_amount', 'new_invoice_no',
        'new_invoice_date', 'new_invoice_amount', 'reason_category', 'reason', 'approval', 'sheet_row']

log = logging.getLogger('cancel_reasons')


def sheets_client():
    path = os.environ.get('CANCEL_REASON_TOKEN_JSON') or os.environ.get('STAFF_SALARY_TOKEN_JSON') or DEFAULT_TOKEN
    creds = Credentials.from_authorized_user_file(path)
    if not creds.valid:
        creds.refresh(Request())
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def norm(s) -> str:
    return re.sub(r'\s+', ' ', str(s if s is not None else '')).strip()


def num(v):
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = norm(v).replace(',', '').replace('₹', '')
    try:
        return round(float(s), 2) if s else None
    except ValueError:
        return None


def day(v):
    """A sheet date: a serial number (a real date cell) or text as the team types it. None when it does not parse."""
    if isinstance(v, (int, float)) and 30000 < v < 80000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
    s = norm(v)
    if not s:
        return None
    m = DATE_RE.match(s)
    try:
        if m and m.group(2)[:3].lower() in MONTHS:
            y = int(m.group(3))
            return dt.date(y + 2000 if y < 100 else y, MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
        m = DMY_RE.match(s)
        if m:
            y = int(m.group(3))
            return dt.date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return None


def text(v):
    s = norm(v)
    return s or None


def read(svc):
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID, fields='sheets.properties.title').execute()
    titles = [s['properties']['title'] for s in meta['sheets'] if norm(s['properties']['title']).lower() == TAB]
    if len(titles) != 1:
        raise SystemExit(f'expected one "Master Data" tab, found {titles}')
    grid = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{titles[0]}'", valueRenderOption='UNFORMATTED_VALUE',
    ).execute().get('values', [])
    if not grid:
        raise SystemExit('Master Data is empty')
    idx = {}
    for i, h in enumerate(grid[0]):
        k = norm(h).lower()
        f = HEADERS.get(k) or ('approval' if k.startswith('approver') else None)
        if f and f not in idx:
            idx[f] = i
    missing = [f for f in REQUIRED if f not in idx]
    if missing:
        raise SystemExit(f'header(s) not found in Master Data: {", ".join(missing)}')
    rows, skipped, bad_dates, conflicts = {}, Counter(), Counter(), 0
    for n, row in enumerate(grid[1:], start=2):
        cell = lambda f: row[idx[f]] if f in idx and idx[f] < len(row) else None   # noqa: E731
        inv = norm(cell('invoice_no')).upper().replace(' ', '')
        cat = text(cell('reason_category'))
        if not inv:
            if any(norm(c) for c in row):
                skipped['no old invoice number'] += 1
            continue
        if not cat:
            skipped['no reason category'] += 1
            continue
        wh = (text(cell('wh')) or '').upper() or None
        m = INVOICE_RE.match(inv)
        rec = {'invoice_no': inv, 'wh': wh, 'bu_code': (WH_CODE.get(m.group(1)) if m else None) or WH_CODE.get(wh or ''),
               'po_number': text(cell('po_number')), 'reason_category': cat, 'reason': text(cell('reason')),
               'invoice_amount': num(cell('invoice_amount')), 'new_invoice_no': (text(cell('new_invoice_no')) or '').upper() or None,
               'new_invoice_amount': num(cell('new_invoice_amount')), 'approval': text(cell('approval')), 'sheet_row': n}
        if isinstance(cell('po_number'), float) and cell('po_number').is_integer():
            rec['po_number'] = str(int(cell('po_number')))        # 4547774381 read as a number
        for f in ('cn_date', 'invoice_date', 'new_invoice_date'):
            rec[f] = day(cell(f))
            if rec[f] is None and norm(cell(f)):
                bad_dates[f] += 1
        if inv in rows and (rows[inv]['reason_category'], rows[inv]['reason']) != (rec['reason_category'], rec['reason']):
            conflicts += 1
            log.info('%s twice with different reasons: row %d "%s" -> row %d "%s" (later row kept)', inv,
                     rows[inv]['sheet_row'], rows[inv]['reason'], n, rec['reason'])
        rows[inv] = rec
    if skipped:
        log.info('skipped: %s', dict(skipped))
    if bad_dates:
        log.warning('dates that did not parse (stored empty): %s', dict(bad_dates))
    return list(rows.values()), conflicts


def connect():
    dsn = os.environ['SUPABASE_DB_URL']
    kw = dict(sslmode=os.environ.get('SUPABASE_DB_SSLMODE', 'require'))
    if os.environ.get('SUPABASE_DB_SSLROOTCERT'):
        kw['sslrootcert'] = os.environ['SUPABASE_DB_SSLROOTCERT']
    return psycopg2.connect(dsn, keepalives=1, keepalives_idle=30, keepalives_interval=10, **kw)


def canon(v):
    """One spelling per value on both sides: the table's Decimal 2349 and the sheet's 2349.0 compare equal."""
    if v is None:
        return None
    if isinstance(v, (int, float)) or type(v).__name__ == 'Decimal':
        return f'{float(v):.2f}'
    return str(v)


def key(r):
    return tuple(canon(r[c]) for c in COLS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    rows, conflicts = read(sheets_client())
    cats = Counter(r['reason_category'] for r in rows)
    log.info('Master Data: %d invoices, %d categories %s, %d reasons; %d typed twice with different reasons',
             len(rows), len(cats), dict(cats.most_common()), len({r['reason'] for r in rows}), conflicts)
    if len(rows) < MIN_ROWS:
        raise SystemExit(f'only {len(rows)} rows read (< {MIN_ROWS}): not replacing the table')

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = '120000'")
            cur.execute(f'select {", ".join(COLS)} from public.o2c_cancel_reasons')
            before = {tuple(canon(v) for v in r) for r in cur.fetchall()}
            after = {key(r) for r in rows}
            new_or_changed = len(after - before)
            gone = len({k[0] for k in before} - {k[0] for k in after})
            log.info('vs the table (%d rows): %d new or changed, %d no longer in the sheet', len(before), new_or_changed, gone)
            if args.dry_run or (new_or_changed == 0 and gone == 0):
                conn.rollback()
                log.info('dry run: nothing written' if args.dry_run else 'unchanged: nothing written')
                return 0
            cur.execute('delete from public.o2c_cancel_reasons where true')
            execute_values(cur, f'insert into public.o2c_cancel_reasons ({", ".join(COLS)}) values %s',
                           [tuple(r[c] for c in COLS) for r in rows], page_size=500)
            try:
                cur.execute("select 1 from information_schema.tables where table_schema='public' and table_name='workflow_logs'")
                if cur.fetchone():
                    cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) "
                                "values (%s, %s, now(), now(), 'success', %s::jsonb, %s, %s, now())",
                                ('sheet_grn_loader:cancel_reasons', 'cancel_reasons',
                                 json.dumps({'changed': new_or_changed, 'gone': gone, 'conflicts': conflicts}), len(rows), len(rows)))
            except Exception as e:                            # noqa: BLE001
                log.warning('workflow_logs not written: %s', str(e)[:100])
        conn.commit()
        log.info('replaced: %d rows', len(rows))
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
