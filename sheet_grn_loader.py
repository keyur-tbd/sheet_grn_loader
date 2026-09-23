#!/usr/bin/env python3
"""Google Sheet -> Supabase loader for GRN reports that are downloaded from a partner portal
and pasted into a sheet (Zepto, Amazon Vendor Central). One table per source, one row per
sheet row, idempotent on row_hash, so the sheet can be re-read every day.

    python sheet_grn_loader.py --source zepto --discover          # print tabs, headers, inferred types, proposed DDL
    python sheet_grn_loader.py --source zepto --apply-schema      # create the table if it does not exist
    python sheet_grn_loader.py --source zepto                     # load (upsert) every tab
    python sheet_grn_loader.py --source amazon --tab "GRN"        # one tab only

Environment (.env next to this file is loaded; real environment wins):
    SUPABASE_DB_URL      postgres connection string (session pooler host; GitHub runners are IPv4 only)
    SUPABASE_DB_SSLMODE  optional, default require (this office machine needs verify-full + SUPABASE_DB_SSLROOTCERT)
    GOOGLE_TOKEN_JSON    path to an OAuth token.json whose account can OPEN the sheet (default: token.json)

Reads cells with valueRenderOption=UNFORMATTED_VALUE so long identifiers never arrive as 3.23E+12
(the CSV export of these sheets is lossy). Conventions follow the other GRN loaders: row_hash,
source_file, drive_file_id, raw_data jsonb, processed_at/created_at, lower-snake column names.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import time

import psycopg2
from psycopg2.extras import execute_values
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=False)
except ImportError:
    pass

log = logging.getLogger('sheet-grn')

# ------------------------------------------------------------------ sources
# columns: sheet header (case/space-insensitive) -> (column name, type). Leave {} to let --discover propose one;
# in that case the loader lands every header as snake_case text and the typed columns can be added later.
SOURCES = {
    'zepto': {
        'sheet_id': '1Txws1Qan9QVyR3qJTlup5BadQ9KwI6qk84KQb8bMbEQ',
        'table': 'zepto_grn',
        'platform': 'Zepto',
        'tabs': ['Sheet1'],      # 2026-09-23: a scratch 'Sheet2' (SKU name list) appeared and broke the load
        'header_row': 1,
        'columns': {},           # filled after --discover once the sheet is shared with the loader account
        'line_key': ('po_no', 'sku'),   # upsert key; status moves PENDING_GRN -> COMPLETED in place
        # 2026-09-21: the sheet lacked almost every qty >= 100 PO line up to Jun-2026, so those 4,285 lines were backfilled
        # from the portal PO export in Drive "6) PO Data/Zepto PO Data" (drive_file_id below). The sheet wins: once it
        # carries the same PO x SKU, the backfilled row is deleted, so a re-export can never double-count a receipt.
        'superseded_backfill': {'drive_file_id': '1ASryvVaDInzGKqGyUOPou5wUux5KIMXy', 'key': ('po_no', 'sku')},
    },
    'amazon': {
        'sheet_id': '1IjwfjZg_l9JTiRlosZaNId-QPpfHi48JMH1_Tz8trPo',
        'table': 'amazon_grn',
        'platform': 'Amazon',
        'tabs': ['Amazon'],          # the other tabs are September-2025 revenue-assurance snapshots for other partners
        'header_row': 1,
        'columns': {                # header (normalised) -> (column, type); blanks in the sample made these look like text
            'invoicequantity': ('invoice_quantity', 'numeric'), 'totalamountexcludingtax': ('total_amount_excluding_tax', 'numeric'),
            'taxamount': ('tax_amount', 'numeric'), 'totalamount': ('total_amount', 'numeric'), 'shortagequantity': ('shortage_quantity', 'numeric'),
            'amountshortage': ('amount_shortage', 'numeric'), 'pricediscrepancyamount': ('price_discrepancy_amount', 'numeric'), 'amazonpaidcost': ('amazon_paid_cost', 'numeric'),
            'externalid': ('external_id', 'text'), 'ponumber': ('po_number', 'text'), 'count': ('row_count', 'numeric'), 'date': ('report_date', 'text'),
        },
        # Amazon rewrites a line in place (Confirmed -> Closed, received qty filled in). The load upserts on
        # PO x ASIN: the sheet's current version replaces the stored one; lines only in Supabase are kept.
        'line_key': ('po_number', 'asin'),     # the sheet's Invoice Number column is always blank
    },
}
BASE_COLUMNS = [  # every table gets these
    ('id', 'bigint generated always as identity primary key'),
    ('row_hash', 'text not null unique'),
    ('source_file', 'text not null'),          # "<sheet title> / <tab>"
    ('drive_file_id', 'text'),                 # the spreadsheet id
    ('sheet_row', 'integer'),                  # 1-based row in the tab at load time
    ('raw_data', 'jsonb not null'),
    ('processed_at', 'timestamptz not null default now()'),
    ('created_at', 'timestamptz not null default now()'),
]
PAGE = 10000


# ------------------------------------------------------------------ helpers
def snake(name: str) -> str:
    s = re.sub(r'[^0-9a-zA-Z]+', '_', str(name).strip()).strip('_').lower()
    if not s: s = 'col'
    if s[0].isdigit(): s = 'c_' + s
    return s[:63]


def norm_header(name: str) -> str:
    return re.sub(r'[^0-9a-z]', '', str(name).lower())


def parse_date(v):
    if v in (None, ''): return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        # Google serial date (days since 1899-12-30)
        if 20000 < float(v) < 80000:
            return (dt.date(1899, 12, 30) + dt.timedelta(days=int(v))).isoformat()
        return None
    s = str(v).strip()
    for f in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d', '%d.%m.%Y', '%d-%b-%Y', '%d %b %Y', '%b %d, %Y', '%Y-%m-%d %H:%M:%S', '%d-%m-%Y %H:%M', '%d/%m/%Y %H:%M', '%m/%d/%Y'):
        try: return dt.datetime.strptime(s[:len(f) + 2] if '%H' in f else s, f).date().isoformat()
        except ValueError: pass
    try:
        return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        return None


def parse_num(v):
    if v in (None, ''): return None
    if isinstance(v, bool): return None
    if isinstance(v, (int, float)): return v
    s = str(v).replace(',', '').replace('₹', '').strip()
    try: return float(s)
    except ValueError: return None


def infer_type(values):
    vals = [v for v in values if v not in (None, '')]
    if not vals: return 'text'
    if sum(1 for v in vals if parse_num(v) is not None) >= 0.95 * len(vals):
        return 'numeric'
    if sum(1 for v in vals if parse_date(v) is not None) >= 0.95 * len(vals):
        return 'date'
    return 'text'


# ------------------------------------------------------------------ google
_creds = None


def sheets_service():
    """Sheets client on the token's own scopes (asking for new scopes on refresh fails with invalid_scope)."""
    global _creds
    tok = os.environ.get('GOOGLE_TOKEN_JSON', 'token.json')
    _creds = Credentials.from_authorized_user_file(tok)
    if not _creds.valid:
        _creds.refresh(Request())
    return build('sheets', 'v4', credentials=_creds, cache_discovery=False)


def token_account() -> str:
    try:
        return build('drive', 'v3', credentials=_creds, cache_discovery=False).about().get(fields='user(emailAddress)').execute()['user']['emailAddress']
    except Exception:                                    # noqa: BLE001
        return 'the account behind ' + os.environ.get('GOOGLE_TOKEN_JSON', 'token.json')


def with_retry(fn, what):
    for attempt in range(1, 6):
        try:
            return fn()
        except HttpError as e:
            if e.resp.status in (429, 500, 502, 503, 504) and attempt < 5:
                wait = 2 ** attempt
                log.warning('%s: HTTP %s, retry in %ss', what, e.resp.status, wait); time.sleep(wait)
            else:
                raise


def read_tab(svc, sheet_id, tab, header_row):
    """Return (headers, rows) with rows as lists aligned to headers, using UNFORMATTED_VALUE."""
    hdr = with_retry(lambda: svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"'{tab}'!{header_row}:{header_row}", valueRenderOption='UNFORMATTED_VALUE').execute(), 'header').get('values', [[]])[0]
    headers = [str(h).strip() for h in hdr]
    while headers and headers[-1] == '': headers.pop()
    rows, start = [], header_row + 1
    while True:
        rng = f"'{tab}'!{start}:{start + PAGE - 1}"
        vals = with_retry(lambda: svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng, valueRenderOption='UNFORMATTED_VALUE', dateTimeRenderOption='FORMATTED_STRING').execute(), rng).get('values', [])
        if not vals: break
        for i, r in enumerate(vals):
            r = list(r)[:len(headers)] + [''] * max(0, len(headers) - len(r))
            if any(str(v).strip() != '' for v in r):
                rows.append((start + i, r))
        if len(vals) < PAGE: break
        start += PAGE
    return headers, rows


# ------------------------------------------------------------------ database
def connect():
    dsn = os.environ['SUPABASE_DB_URL']
    kw = dict(sslmode=os.environ.get('SUPABASE_DB_SSLMODE', 'require'))
    if os.environ.get('SUPABASE_DB_SSLROOTCERT'): kw['sslrootcert'] = os.environ['SUPABASE_DB_SSLROOTCERT']
    return psycopg2.connect(dsn, **kw)


def ddl(src, headers, typed):
    cols = list(BASE_COLUMNS)
    seen = {c for c, _ in cols}
    for h in headers:
        if not h: continue
        col, typ = typed.get(norm_header(h), (snake(h), 'text'))
        if col in seen: continue
        seen.add(col); cols.append((col, typ))
    body = ',\n'.join(f'    {c:28s} {t}' for c, t in cols)
    t = src['table']
    return (f"create table if not exists public.{t} (\n{body}\n);\n"
            f"create index if not exists {t}_source_file_lower_idx on public.{t} (lower(source_file));\n"
            f"alter table public.{t} enable row level security;\n"
            f"comment on table public.{t} is 'LIVE GRN. {src['platform']} GRN report pasted into a Google Sheet (drive_file_id = the sheet), loaded by sheet_grn_loader; one row per sheet row, idempotent on row_hash.';\n")


def column_plan(src, headers, sample_rows):
    """Typed column map for these headers: configured entries win, otherwise inferred from the sample."""
    plan = {}
    for i, h in enumerate(headers):
        if not h: continue
        key = norm_header(h)
        if key in src['columns']:
            plan[i] = src['columns'][key]
        else:
            plan[i] = (snake(h), infer_type([r[i] for _, r in sample_rows if i < len(r)]))
    return plan


def convert(v, typ):
    if typ == 'numeric': return parse_num(v)
    if typ == 'date': return parse_date(v)
    if v in (None, ''): return None
    return str(v).strip()


def load(src, tab, title, headers, rows, conn, dry_run):
    plan = column_plan(src, headers, rows[:2000])
    cols = [c for _, (c, _) in sorted(plan.items())]
    dup = {c for c in cols if cols.count(c) > 1}
    if dup: raise SystemExit(f'duplicate column names after snake_case: {dup}; set explicit names in SOURCES columns')
    out = []
    for sheet_row, r in rows:
        raw = {headers[i]: (None if r[i] in (None, '') else r[i]) for i in range(len(headers)) if headers[i]}
        canonical = json.dumps(raw, sort_keys=True, default=str, ensure_ascii=False)
        rh = hashlib.sha256(f"{src['sheet_id']}|{tab}|{canonical}".encode('utf-8')).hexdigest()
        vals = [convert(r[i], typ) for i, (_, typ) in sorted(plan.items())]
        out.append([rh, f'{title} / {tab}', src['sheet_id'], sheet_row, json.dumps(raw, default=str, ensure_ascii=False)] + vals)
    # rows identical in content within one tab (a partner report repeating a line) collapse to one; keep the first
    seen, uniq = set(), []
    for row in out:
        if row[0] in seen: continue
        seen.add(row[0]); uniq.append(row)
    log.info("%s / %s: %d sheet rows, %d distinct", title, tab, len(out), len(uniq))
    key = src['line_key']
    missing = [k for k in key if k not in cols]
    if missing: raise RuntimeError(f'line_key columns {missing} are not in the sheet headers of {title} / {tab}')
    kidx = [5 + cols.index(k) for k in key]
    count = {}
    for row in uniq:
        k = tuple(row[i] for i in kidx); count[k] = count.get(k, 0) + 1
    clash = [k for k, n in count.items() if n > 1]
    if clash:   # the upsert would keep only one of them; stop rather than lose a line
        raise RuntimeError(f'{len(clash)} {key} keys appear on more than one sheet row (e.g. {clash[:3]}); the line key no longer identifies a line')
    if dry_run or not uniq: return 0, 0
    names = ['row_hash', 'source_file', 'drive_file_id', 'sheet_row', 'raw_data'] + cols
    t = src['table']
    # Upsert on the line key: a line the sheet still carries is replaced by its current version (id and created_at
    # stay, processed_at moves); a line only in Supabase is left as it is; an unchanged line is not touched.
    sets = ', '.join(f'{c} = excluded.{c}' for c in names if c not in key)
    sql = (f"insert into public.{t} ({', '.join(names)}) values %s "
           f"on conflict ({', '.join(key)}) do update set {sets}, processed_at = now() "
           f"where public.{t}.row_hash is distinct from excluded.row_hash "
           f"returning (xmax = 0)")
    inserted = updated = 0
    with conn.cursor() as cur:
        for i in range(0, len(uniq), 500):
            res = execute_values(cur, sql, uniq[i:i + 500], template='(' + ', '.join(['%s'] * 5 + ['%s'] * len(cols)) + ')', page_size=500, fetch=True)
            ins = sum(1 for (x,) in res if x)
            inserted += ins; updated += len(res) - ins
    conn.commit()
    return inserted, updated


def ensure_line_key_index(src, conn):
    """The unique index the upsert's ON CONFLICT resolves against (idempotent)."""
    t, key = src['table'], src['line_key']
    with conn.cursor() as cur:
        cur.execute(f"create unique index if not exists {t}_line_key_uidx on public.{t} ({', '.join(key)}) nulls not distinct")
    conn.commit()


def drop_superseded_backfill(src, conn):
    """Delete backfilled rows (from another file) whose key now also arrives from the sheet itself."""
    bf = src['superseded_backfill']
    on = ' and '.join(f'upper(trim(s.{k})) = upper(trim(b.{k}))' for k in bf['key'])
    with conn.cursor() as cur:
        cur.execute(f"delete from public.{src['table']} b where b.drive_file_id = %s and exists "
                    f"(select 1 from public.{src['table']} s where s.drive_file_id = %s and {on})",
                    (bf['drive_file_id'], src['sheet_id']))
        n = cur.rowcount
    conn.commit()
    if n: log.info('%s: %d backfilled rows superseded by the sheet, deleted', src['table'], n)


def log_run(conn, source, status, rows, msg='', started=None):
    """Same shared workflow_logs table the other schedulers write to (workflow, source, status, details, rows_written ...)."""
    try:
        with conn.cursor() as cur:
            cur.execute("select 1 from information_schema.tables where table_schema='public' and table_name='workflow_logs'")
            if cur.fetchone():
                cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) values (%s, %s, %s, now(), %s, %s::jsonb, %s, %s, now())",
                            (f'sheet_grn_loader:{source}', f'sheet_grn:{source}', started or dt.datetime.now(dt.timezone.utc), status, json.dumps({'message': msg[:500], 'rows_written': rows}), rows, rows))
        conn.commit()
    except Exception as e:                                    # noqa: BLE001
        conn.rollback(); log.warning('workflow_logs not written: %s', str(e)[:100])


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True, choices=sorted(SOURCES))
    ap.add_argument('--tab', help='load only this tab')
    ap.add_argument('--discover', action='store_true', help='print tabs, headers, inferred types and the DDL; write nothing')
    ap.add_argument('--apply-schema', action='store_true', help='create the table if missing, then exit')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('-v', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    src = SOURCES[a.source]
    svc = sheets_service()
    try:
        meta = with_retry(lambda: svc.spreadsheets().get(spreadsheetId=src['sheet_id'], fields='properties.title,sheets(properties(title,gridProperties))').execute(), 'metadata')
    except HttpError as e:
        if e.resp.status in (403, 404):
            log.error("cannot open the %s sheet (HTTP %s). Share https://docs.google.com/spreadsheets/d/%s with %s (Viewer) and run again.", a.source, e.resp.status, src['sheet_id'], token_account())
            return 2
        raise
    title = meta['properties']['title']
    tabs = [s['properties']['title'] for s in meta['sheets']]
    if a.tab: tabs = [a.tab]
    elif src['tabs']: tabs = [t for t in tabs if t in src['tabs']]
    log.info('%s: tabs %s', title, tabs)

    if a.discover:
        for tab in tabs:
            headers, rows = read_tab(svc, src['sheet_id'], tab, src['header_row'])
            plan = column_plan(src, headers, rows[:2000])
            print(f"\n== tab '{tab}': {len(rows)} data rows, {len(headers)} headers")
            for i, h in enumerate(headers):
                if h: print(f"   {h!r:45s} -> {plan[i][0]:30s} {plan[i][1]:8s} e.g. {[r[i] for _, r in rows[:3]]}")
            print('\n' + ddl(src, headers, {norm_header(h): plan[i] for i, h in enumerate(headers) if h}))
        return

    conn = connect()
    if a.apply_schema:
        headers, rows = read_tab(svc, src['sheet_id'], tabs[0], src['header_row'])
        plan = column_plan(src, headers, rows[:2000])
        with conn.cursor() as cur: cur.execute(ddl(src, headers, {norm_header(h): plan[i] for i, h in enumerate(headers) if h}))
        conn.commit(); log.info('schema applied for %s', src['table']); return

    total, started = 0, dt.datetime.now(dt.timezone.utc)
    try:
        if not a.dry_run: ensure_line_key_index(src, conn)
        inserted = updated = 0
        for tab in tabs:
            headers, rows = read_tab(svc, src['sheet_id'], tab, src['header_row'])
            if not headers: log.warning("tab '%s' has no header row; skipped", tab); continue
            ins, upd = load(src, tab, title, headers, rows, conn, a.dry_run)
            inserted += ins; updated += upd
        total = inserted + updated
        log.info('%s: %d new lines inserted, %d existing lines replaced by their current sheet version', src['table'], inserted, updated)
        if src.get('superseded_backfill') and not a.dry_run and not a.tab:
            drop_superseded_backfill(src, conn)
        log_run(conn, a.source, 'OK', total, f'{title}: tabs {tabs}; inserted {inserted}, updated {updated}', started)
    except Exception as e:
        log_run(conn, a.source, 'ERROR', total, str(e), started); raise
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
