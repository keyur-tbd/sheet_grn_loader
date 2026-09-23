#!/usr/bin/env python3
"""The never-billed PO list, as a Google Sheet the SCM team works from (Birbal SCM Tracker, 2026-09-23).

    python never_billed_sheet.py                 # rewrite the two tabs of the sheet
    python never_billed_sheet.py --create        # create the spreadsheet once, share it, print its id
    python never_billed_sheet.py --dry-run       # query only, print the counts

What it writes, from warehouse.fill_rate_lines (the SCM Tracker's own snapshot, rebuilt after each
order-to-cash beat), this financial year (PO date >= 2026-04-01), one row per PO:
    tab "Never billed"             POs with NOTHING billed against them and no invoice ever raised
    tab "Reversed by credit memo"  POs whose only invoice a credit memo reversed (nothing re-billed)
A part-billed PO is short-shipped, not pending, exactly as the board's Pending POs view has it. Each tab
is cleared and rewritten in full; row 1 says when, row 2 is the totals, row 3 the headers. PO numbers
travel as text so the sheet never turns 2873410037167 into 2.87E+12.

Environment (.env next to this file is loaded; the real environment wins):
    SUPABASE_DB_URL, SUPABASE_DB_SSLMODE, SUPABASE_DB_SSLROOTCERT   as sheet_grn_loader.py
    GOOGLE_TOKEN_JSON        OAuth token.json (drive + spreadsheets scope; the same one the GRN loads use)
    NEVER_BILLED_SHEET_ID    overrides SPREADSHEET_ID below
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

import psycopg2
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=False)
except ImportError:
    pass

log = logging.getLogger('never-billed')

# Created once with --create (2026-09-23) by instamart@thebakersdozen.in and shared with birbal@ (writer)
# and the thebakersdozen.in domain (reader). The SCM Tracker's Pending POs view links to it.
SPREADSHEET_ID = '18oXNb4bHhQ-ealzgcLysEd0EAyzGKBjOHxUDhSvcY7I'
TITLE = 'Birbal - Never billed POs'
SHARE_WRITER = ['birbal@thebakersdozen.in']
SHARE_DOMAIN = 'thebakersdozen.in'
FY_FROM = '2026-04-01'
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

COLUMNS = ['Party', 'PO number', 'PO date', 'Age (days)', 'State', 'Warehouse', 'Customer', 'Partner location',
           'City', 'PO lines', 'Ordered units', 'PO value (Rs, landing rate)', 'Categories', 'Cancelled invoice',
           'Credit memo', 'Reason', 'Source']

# PO grain, classified exactly as the board's Pending POs view classifies it (api/_lib/fillrate.js
# loadPendingPos): grouped by platform + PO number, pending only when NO line carries a live invoice.
SQL = """
with po as (
  select coalesce(f.trade_party, pp.party, f.platform) as party,
         f.po_number,
         min(f.po_date) as po_date,
         max(f.warehouse_name) as warehouse_name,
         max(f.customer_name) as customer_name,
         max(f.platform_location) as platform_location,
         max(f.city) as city,
         max(f.source_system) as source_system,
         count(*) as po_lines,
         sum(f.po_qty) filter (where not f.item_unmapped) as po_qty,
         sum(f.po_qty * f.landing_rate) filter (where not f.item_unmapped) as po_amount,
         sum(greatest(coalesce(f.po_qty, 0) - coalesce(f.invoiced_qty_vs_po, 0), 0)) filter (where not f.item_unmapped) as uninvoiced_qty,
         string_agg(distinct nullif(f.item_category, ''), ', ') as categories,
         bool_or(f.is_invoiced) as any_live_invoice,
         bool_or(f.was_reversed) as was_reversed,
         string_agg(distinct f.reversed_invoice_nos, ', ') as reversed_invoice_nos,
         string_agg(distinct f.cancellation_memo_nos, ', ') as cancellation_memo_nos,
         string_agg(distinct f.cancellation_kinds, ', ') as cancellation_kinds
    from warehouse.fill_rate_lines f
    left join warehouse.ref_platform_party pp on pp.platform = f.platform
   where f.po_date >= %(fy_from)s
   group by f.platform, f.trade_party, pp.party, f.po_number
)
select party, po_number, po_date, (current_date - po_date) as age, case when was_reversed then 'Reversed by credit memo' else 'Never billed' end as state,
       warehouse_name, customer_name, platform_location, city, po_lines, po_qty, round(po_amount, 0) as po_amount, categories,
       reversed_invoice_nos, cancellation_memo_nos, cancellation_kinds, source_system
  from po
 where not any_live_invoice and coalesce(uninvoiced_qty, 0) > 0
 order by was_reversed, po_qty desc nulls last, po_date
"""


# ------------------------------------------------------------------ google
_creds = None


def creds():
    global _creds
    if _creds is None:
        _creds = Credentials.from_authorized_user_file(os.environ.get('GOOGLE_TOKEN_JSON', 'token.json'))
        if not _creds.valid:
            _creds.refresh(Request())
    return _creds


def sheets():
    return build('sheets', 'v4', credentials=creds(), cache_discovery=False)


def drive():
    return build('drive', 'v3', credentials=creds(), cache_discovery=False)


def sheet_id():
    return os.environ.get('NEVER_BILLED_SHEET_ID') or SPREADSHEET_ID


def create_sheet():
    svc = sheets()
    body = {'properties': {'title': TITLE},
            'sheets': [{'properties': {'title': 'Never billed', 'gridProperties': {'frozenRowCount': 3}}},
                       {'properties': {'title': 'Reversed by credit memo', 'gridProperties': {'frozenRowCount': 3}}}]}
    ss = svc.spreadsheets().create(body=body).execute()
    sid = ss['spreadsheetId']
    print('created', sid, ss.get('spreadsheetUrl'))
    d = drive()
    for who in SHARE_WRITER:
        try:
            d.permissions().create(fileId=sid, body={'type': 'user', 'role': 'writer', 'emailAddress': who}, sendNotificationEmail=False).execute()
            print('shared writer', who)
        except HttpError as e:
            print('could not share with', who, e)
    # Wider sharing (the thebakersdozen.in domain as reader) is a one-click decision for the
    # owner in the sheet's Share dialog; the loader only ever shares with the Birbal account.
    print('share it with the domain %s from the sheet itself if the SCM team should open it directly' % SHARE_DOMAIN)
    return sid


# ------------------------------------------------------------------ database
def connect():
    dsn = os.environ['SUPABASE_DB_URL']
    kw = dict(sslmode=os.environ.get('SUPABASE_DB_SSLMODE', 'require'))
    if os.environ.get('SUPABASE_DB_SSLROOTCERT'):
        kw['sslrootcert'] = os.environ['SUPABASE_DB_SSLROOTCERT']
    return psycopg2.connect(dsn, **kw)


def fetch(conn):
    with conn.cursor() as cur:
        cur.execute("set statement_timeout = '600000'")
        cur.execute(SQL, {'fy_from': FY_FROM})
        rows = cur.fetchall()
        cur.execute('select refreshed_at, max_po_date from warehouse.fill_rate_fact_meta where id = 1')
        meta = cur.fetchone()
    return rows, meta


def log_run(conn, status, rows, msg='', started=None):
    """The same workflow_logs table the GRN loads write to, so the freshness check is one query."""
    try:
        with conn.cursor() as cur:
            cur.execute("select 1 from information_schema.tables where table_schema='public' and table_name='workflow_logs'")
            if cur.fetchone():
                cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) values (%s, %s, %s, now(), %s, %s::jsonb, %s, %s, now())",
                            ('sheet_grn_loader:never_billed', 'sheet_grn:never_billed', started or dt.datetime.now(dt.timezone.utc), status,
                             json.dumps({'message': msg[:500], 'rows_written': rows}), rows, rows))
        conn.commit()
    except Exception as e:                                    # noqa: BLE001
        conn.rollback(); log.warning('workflow_logs not written: %s', str(e)[:100])


# ------------------------------------------------------------------ the sheet
def reason_word(kinds):
    if not kinds:
        return ''
    parts = []
    if 'INC_MEMO' in kinds:
        parts.append('INC memo')
    if 'BC_CANCELLED' in kinds:
        parts.append('Cancel/Correct')
    return ' + '.join(parts) or kinds


def cell(v):
    if v is None:
        return ''
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime('%Y-%m-%d')
    if hasattr(v, 'quantize'):           # Decimal
        return float(v)
    return v


def tab_values(rows, meta, state):
    now = dt.datetime.now(IST).strftime('%d %b %Y %H:%M IST')
    snap = meta[0].astimezone(IST).strftime('%d %b %Y %H:%M IST') if meta and meta[0] else 'unknown'
    n_pos = len(rows)
    units = sum(float(r[10] or 0) for r in rows)
    value = sum(float(r[11] or 0) for r in rows)
    head1 = ['Refreshed %s from the SCM Tracker snapshot of %s (POs to %s). FY27, PO date >= %s. %s: POs with nothing billed against them; a part-billed PO is short-shipped, not pending. Rewritten every 3 hours -- do not edit here.'
             % (now, snap, meta[1] if meta else '?', FY_FROM, state)]
    head2 = ['Total', '%d POs' % n_pos, '', '', '', '', '', '', '', sum(int(r[9] or 0) for r in rows), units, round(value), '', '', '', '', '']
    out = [head1, head2, COLUMNS]
    for r in rows:
        (party, po, po_date, age, st, wh, cust, loc, city, lines, qty, amt, cats, rinv, memo, kinds, src) = r
        out.append([cell(party), str(po), cell(po_date), int(age) if age is not None else '', st, cell(wh), cell(cust), cell(loc), cell(city),
                    int(lines or 0), cell(qty), cell(amt), cell(cats), cell(rinv), cell(memo), reason_word(kinds), cell(src)])
    return out


def write_tab(svc, sid, tab, values):
    # make sure the tab exists (a fresh sheet created elsewhere may lack it)
    meta = svc.spreadsheets().get(spreadsheetId=sid, fields='sheets(properties(sheetId,title))').execute()
    titles = {s['properties']['title']: s['properties']['sheetId'] for s in meta.get('sheets', [])}
    if tab not in titles:
        r = svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={'requests': [{'addSheet': {'properties': {'title': tab, 'gridProperties': {'frozenRowCount': 3}}}}]}).execute()
        titles[tab] = r['replies'][0]['addSheet']['properties']['sheetId']
    gid = titles[tab]
    svc.spreadsheets().values().clear(spreadsheetId=sid, range="'%s'" % tab).execute()
    # RAW: a PO number string stays a string; numbers stay numbers
    svc.spreadsheets().values().update(spreadsheetId=sid, range="'%s'!A1" % tab, valueInputOption='RAW', body={'values': values}).execute()
    svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={'requests': [
        {'updateSheetProperties': {'properties': {'sheetId': gid, 'gridProperties': {'frozenRowCount': 3}}, 'fields': 'gridProperties.frozenRowCount'}},
        {'repeatCell': {'range': {'sheetId': gid, 'startRowIndex': 1, 'endRowIndex': 3}, 'cell': {'userEnteredFormat': {'textFormat': {'bold': True}}}, 'fields': 'userEnteredFormat.textFormat.bold'}},
        {'repeatCell': {'range': {'sheetId': gid, 'startRowIndex': 0, 'endRowIndex': 1}, 'cell': {'userEnteredFormat': {'textFormat': {'italic': True}}}, 'fields': 'userEnteredFormat.textFormat.italic'}},
        {'repeatCell': {'range': {'sheetId': gid, 'startRowIndex': 3, 'startColumnIndex': 11, 'endColumnIndex': 12}, 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'autoResizeDimensions': {'dimensions': {'sheetId': gid, 'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 12}}},
    ]}).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--create', action='store_true', help='create the spreadsheet once, share it and print its id; then exit')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('-v', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if a.create:
        create_sheet()
        return
    sid = sheet_id()
    if not a.dry_run and (not sid or sid.startswith('__')):
        sys.exit('no spreadsheet id: run --create once and put the id in SPREADSHEET_ID (or NEVER_BILLED_SHEET_ID)')
    started = dt.datetime.now(dt.timezone.utc)
    conn = connect()
    try:
        rows, meta = fetch(conn)
        never = [r for r in rows if r[4] == 'Never billed']
        reversed_ = [r for r in rows if r[4] != 'Never billed']
        log.info('%d never-billed POs, %d reversed; snapshot %s', len(never), len(reversed_), meta[0] if meta else None)
        if a.dry_run:
            return
        svc = sheets()
        write_tab(svc, sid, 'Never billed', tab_values(never, meta, 'Never billed'))
        write_tab(svc, sid, 'Reversed by credit memo', tab_values(reversed_, meta, 'Reversed by credit memo'))
        log_run(conn, 'success', len(rows), 'never billed %d, reversed %d' % (len(never), len(reversed_)), started)
        log.info('written: https://docs.google.com/spreadsheets/d/%s', sid)
    except Exception as e:                                    # noqa: BLE001
        log.exception('failed')
        try:
            log_run(conn, 'failed', 0, str(e), started)
        finally:
            sys.exit(1)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
