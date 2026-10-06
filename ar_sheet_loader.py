#!/usr/bin/env python3
"""Load the finance team's receivables judgement for Birbal's Receivables board (Birbal migration 157).

Source: the Google Sheet "Sundry Debtors Ageing Report <Mon>.<yy>" (Oct.26 = 1gP3aIt_UMueMdRSWOOffuPRcx_yQ_1FH1z_cBSrxlzU,
owner maulik@, shared with birbal@). A new file starts each month, so the newest spreadsheet whose name starts
"Sundry Debtors Ageing Report" is found on Drive; AR_SHEET_ID pins one instead.

Birbal computes every ERP figure in the workbook from the ledger itself (receivable, ageing, TB, collections all tie
to the paise: migration 157). From the sheet it takes only what the team types:

  Customer Master          customer -> customer group, credit period, category   -> public.ar_sheet_customer_master
  <dd.mm.yy> (each week)   DSO, missing invoices, collection aim + PDC (lakh),
                           comments, and the tab's own figures                    -> public.ar_sheet_weekly
  Receivable Adj. Details  pending RTV / previous-year RTV / previous-year debits
                           / pending debits, one line per claim                   -> public.ar_sheet_adjustments

Columns are found by their header NAME, so a moved or added column is fine. Every run replaces each weekly tab it
reads (older tabs already loaded stay, as history) and the adjustments register, and upserts the master (never
emptied). A register read with less than half the lines already loaded is refused (a half-filled new file).

Environment: SUPABASE_DB_URL (+ SUPABASE_DB_SSLMODE / SUPABASE_DB_SSLROOTCERT), AR_SHEET_TOKEN_JSON (path to birbal@'s
Google token with Drive read scope; default D:\\Python\\Birbal\\salary_loader\\token_birbal_drive.json), AR_SHEET_ID
(optional).

    python ar_sheet_loader.py --dry-run     # read + check, write nothing
    python ar_sheet_loader.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
from collections import Counter, defaultdict

import psycopg2
from psycopg2.extras import Json, execute_values
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:                                            # noqa: BLE001
    pass

DEFAULT_TOKEN = r'D:\Python\Birbal\salary_loader\token_birbal_drive.json'
NAME_PREFIX = 'Sundry Debtors Ageing Report'
SHEETS_EPOCH = dt.date(1899, 12, 30)
WEEKLY_TAB = re.compile(r'^\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})\s*$')     # 05.10.26

# the register's Tagging -> the weekly tab column it feeds
KINDS = {'pending rtv': 'pending_rtv', 'previous year rtvs': 'py_rtv', 'previous year rtv': 'py_rtv',
         'previous year debits': 'py_debits', 'previous year debit': 'py_debits', 'pending debits': 'pending_debits',
         'pending debit': 'pending_debits'}

log = logging.getLogger('ar_sheet')


def creds():
    path = os.environ.get('AR_SHEET_TOKEN_JSON') or DEFAULT_TOKEN
    c = Credentials.from_authorized_user_file(path)
    if not c.valid:
        c.refresh(Request())
    return c


def find_sheet(c):
    if os.environ.get('AR_SHEET_ID'):
        return os.environ['AR_SHEET_ID']
    drive = build('drive', 'v3', credentials=c, cache_discovery=False)
    res = drive.files().list(
        q=f"name contains '{NAME_PREFIX}' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
        orderBy='modifiedTime desc', fields='files(id,name,modifiedTime)', pageSize=10,
        includeItemsFromAllDrives=True, supportsAllDrives=True, corpora='allDrives').execute()
    files = [f for f in res.get('files', []) if f['name'].lower().startswith(NAME_PREFIX.lower())]
    if not files:
        raise SystemExit(f'no spreadsheet named "{NAME_PREFIX} ..." is shared with this account')
    # the newest MONTH wins, not the most recently touched file (an old month can be edited late)
    def month_of(f):
        m = re.search(r'([A-Za-z]{3})[a-z]*\.?\s*[\'.]?(\d{2,4})\s*$', f['name'])
        if not m:
            return (0, 0, f['modifiedTime'])
        mon = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'].index(m.group(1).lower()) + 1 \
            if m.group(1).lower() in ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec') else 0
        y = int(m.group(2))
        return (y + 2000 if y < 100 else y, mon, f['modifiedTime'])
    files.sort(key=month_of, reverse=True)
    log.info('sheets found: %s', ', '.join(f['name'] for f in files))
    return files[0]['id']


def norm(s) -> str:
    return re.sub(r'\s+', ' ', str(s if s is not None else '')).replace('\xa0', ' ').strip()


def key(s) -> str:
    return norm(s).lower()


def num(v):
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 4)
    s = norm(v).replace(',', '')
    if s in ('-', ''):
        return None
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def text_id(v):
    """An invoice / customer code cell: 2670151100393.0 -> '2670151100393'."""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return norm(v) or None


def as_date(v):
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)) and 20000 < v < 80000:
        return SHEETS_EPOCH + dt.timedelta(days=int(v))
    s = norm(v)
    for f in ('%d-%m-%Y', '%d/%m/%Y', '%d-%b-%Y', '%d-%B-%Y', '%Y-%m-%d', '%d.%m.%Y', '%d %b %Y', '%d %B %Y'):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def grid(api, sid, tab):
    return api.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{tab}'", valueRenderOption='UNFORMATTED_VALUE',
        dateTimeRenderOption='SERIAL_NUMBER').execute().get('values', [])


def find_tab(names, want):
    for n in names:
        if key(n) == key(want):
            return n
    for n in names:
        if key(want) in key(n):
            return n
    return None


# ------------------------------------------------------------------------------------------- Customer Master
def read_master(g, sid):
    hi = next((i for i, r in enumerate(g[:5]) if r and key(r[0]) == 'code'), None)
    if hi is None:
        raise SystemExit('Customer Master: no header row starting "Code"')
    h = [key(c) for c in g[hi]]
    col = lambda name: h.index(name) if name in h else None             # noqa: E731
    c_name, c_grp, c_search = col('customer name'), col('customer group'), col('search name')
    c_pg, c_cp, c_cat = col('customer posting group'), col('credit period'), col('customer category')
    out = {}
    for row in g[hi + 1:]:
        row = list(row) + [''] * len(h)
        code = text_id(row[0])
        if not code or not re.fullmatch(r'C\d{4,6}', code):
            continue
        out[code] = {'customer_no': code, 'customer_name': norm(row[c_name]) or None,
                     'customer_group': norm(row[c_grp]) or None,
                     'search_name': norm(row[c_search]) or None if c_search is not None else None,
                     'posting_group': norm(row[c_pg]) or None if c_pg is not None else None,
                     'credit_period': num(row[c_cp]) if c_cp is not None else None,
                     'category': norm(row[c_cat]) or None if c_cat is not None else None,
                     'source_sheet_id': sid}
    return list(out.values())


# ------------------------------------------------------------------------------------------- weekly tabs
# header (lower-cased, whitespace-collapsed) -> field; a header that STARTS with the key matches
WEEKLY_FIGURES = [
    ('channel', 'channel'), ('customer group', 'customer_group'), ('party name', 'party_name'),
    ('credit terms', 'credit_terms'), ('avg. of adj. dso', 'dso'), ('o/s as on', 'opening'),
    ('consumer sales', 'sales'), ('collection during', 'collection'), ('exp jv', 'exp_jv'),
    ('receivable amount', 'receivable'), ('not due amount', 'not_due'), ('dues after', 'dues_later'),
    ('overdue receivable', 'overdue'), ('net overdue', 'net_overdue'), ('missing invoices', 'missing_invoices'),
    ('ideal collection', 'ideal_collection'), ('collection aim', 'collection_aim_l'),
    ('actual collection', 'actual_collection_l'), ('pdc/portal amount', 'pdc_l'), ('pdc on hands', 'pdc_on_hand'),
    ('pending rtv', 'pending_rtv'), ('previous year pending rtv', 'py_rtv'), ('previous year pending debit', 'py_debits'),
    ('pending debits to party', 'pending_debits'), ('net receivable', 'net_receivable'),
]
COMMENT_HINT = re.compile(r'comment|action point', re.I)


def weekly_header(row):
    idx, dues, comments = {}, [], []
    for j, hcell in enumerate(row):
        k = key(hcell)
        if not k:
            continue
        if k.startswith('dues till'):
            dues.append(j)
            continue
        if COMMENT_HINT.search(k):
            comments.append((j, norm(hcell)))
            continue
        for pref, field in WEEKLY_FIGURES:
            if k.startswith(pref) and field not in idx:
                idx[field] = j
                break
    if len(dues) >= 1:
        idx['dues_wk1'] = dues[0]
    if len(dues) >= 2:
        idx['dues_wk2'] = dues[1]
    return idx, comments


def read_weekly(g, tab, as_on, sid):
    hi = next((i for i, r in enumerate(g[:10]) if {'customer_group', 'party_name', 'receivable'} <= set(weekly_header(r)[0])), None)
    if hi is None:
        raise SystemExit(f'{tab}: no header row with Customer Group, Party Name and Receivable amount')
    idx, comments = weekly_header(g[hi])
    aim_label = norm(g[hi][idx['collection_aim_l']]) if 'collection_aim_l' in idx else None
    out, seen = [], set()
    blanks = 0
    for n, row in enumerate(g[hi + 1:], start=hi + 2):
        row = list(row) + [''] * (len(g[hi]) + 2)
        grp = norm(row[idx['customer_group']])
        if not grp:
            blanks += 1
            if blanks >= 2 and out:          # the channel summary block below the groups starts after blank rows
                break
            continue
        blanks = 0
        if key(grp) in ('channel', 'total', 'customer group'):
            break
        if key(grp) in seen:
            log.warning('%s: customer group %r listed twice; keeping the first (row %d skipped)', tab, grp, n)
            continue
        seen.add(key(grp))
        cell = lambda f: row[idx[f]] if f in idx else None                  # noqa: E731
        figures = {f: num(cell(f)) for f in ('opening', 'sales', 'collection', 'exp_jv', 'receivable', 'not_due', 'dues_wk1',
                                             'dues_wk2', 'dues_later', 'overdue', 'net_overdue', 'ideal_collection',
                                             'actual_collection_l', 'pdc_on_hand', 'pending_rtv', 'py_rtv', 'py_debits',
                                             'pending_debits', 'net_receivable') if f in idx}
        notes = [{'h': h, 't': norm(row[j]), 'c': j} for j, h in comments if norm(row[j]) and not isinstance(row[j], (int, float))]
        out.append({'as_on': as_on, 'customer_group': grp, 'tab': tab, 'sheet_row': n,
                    'channel': norm(cell('channel')) or None, 'party_name': norm(cell('party_name')) or None,
                    'credit_terms': num(cell('credit_terms')), 'dso': num(cell('dso')),
                    'missing_invoices': num(cell('missing_invoices')), 'collection_aim_l': num(cell('collection_aim_l')),
                    'pdc_l': num(cell('pdc_l')), 'aim_label': aim_label, 'comments': Json(notes) if notes else None,
                    'sheet': Json(figures), 'source_sheet_id': sid})
    return out


def tab_date(name):
    m = WEEKLY_TAB.match(name)
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    try:
        return dt.date(y + 2000 if y < 100 else y, mth, d)
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------- adjustments register
ADJ_HEADERS = {
    'tagging': {'tagging'}, 'customer_group': {'customer group'}, 'channel': {'channel'},
    'customer_code': {'customer code'}, 'customer_name': {'customer description', 'customer name'},
    'nature': {'nature of expense (in details)', 'nature of expense'}, 'invoice_no': {'invoice no.', 'invoice no'},
    'date': {'date'}, 'value_ex_gst': {'invoice value (without gst)'}, 'gst_amount': {'gst amount'},
    'value': {'invoice value (with gst)'}, 'remarks': {'remarks_mipl', 'remarks'},
}


def read_adjustments(g, sid, skipped):
    def hidx(r):
        idx = {}
        for j, c in enumerate(r):
            for f, al in ADJ_HEADERS.items():
                if f not in idx and key(c) in al:
                    idx[f] = j
        return idx
    hi = next((i for i, r in enumerate(g[:6]) if {'tagging', 'customer_group', 'value'} <= set(hidx(r))), None)
    if hi is None:
        raise SystemExit('Receivable Adj. Details: no header row with Tagging, Customer Group, Invoice Value (With GST)')
    idx = hidx(g[hi])
    out = []
    for n, row in enumerate(g[hi + 1:], start=hi + 2):
        row = list(row) + [''] * (len(g[hi]) + 2)
        cell = lambda f: row[idx[f]] if f in idx else None                  # noqa: E731
        tag, grp, val = norm(cell('tagging')), norm(cell('customer_group')), num(cell('value'))
        if not tag and not grp:
            continue
        if not grp or val is None:
            skipped['register line without a customer group or a value'] += 1
            continue
        kind = KINDS.get(key(tag), 'other')
        if kind == 'other':
            skipped[f'register tagging not used by the weekly tab: {tag or "(blank)"}'] += 1
        dv = cell('date')
        out.append({'sheet_row': n, 'tagging': tag or None, 'kind': kind, 'customer_group': grp,
                    'channel': norm(cell('channel')) or None, 'customer_code': text_id(cell('customer_code')),
                    'customer_name': norm(cell('customer_name')) or None, 'nature': norm(cell('nature')) or None,
                    'invoice_no': text_id(cell('invoice_no')), 'doc_date': as_date(dv),
                    'doc_date_text': None if isinstance(dv, (int, float)) else (norm(dv) or None),
                    'value_ex_gst': num(cell('value_ex_gst')), 'gst_amount': num(cell('gst_amount')), 'value': val,
                    'remarks': norm(cell('remarks')) or None, 'source_sheet_id': sid})
    return out


# ------------------------------------------------------------------------------------------- write
M_COLS = ['customer_no', 'customer_name', 'customer_group', 'search_name', 'posting_group', 'credit_period', 'category', 'source_sheet_id']
W_COLS = ['as_on', 'customer_group', 'tab', 'sheet_row', 'channel', 'party_name', 'credit_terms', 'dso', 'missing_invoices',
          'collection_aim_l', 'pdc_l', 'aim_label', 'comments', 'sheet', 'source_sheet_id']
A_COLS = ['sheet_row', 'tagging', 'kind', 'customer_group', 'channel', 'customer_code', 'customer_name', 'nature', 'invoice_no',
          'doc_date', 'doc_date_text', 'value_ex_gst', 'gst_amount', 'value', 'remarks', 'source_sheet_id']


def connect():
    dsn = os.environ['SUPABASE_DB_URL']
    kw = dict(sslmode=os.environ.get('SUPABASE_DB_SSLMODE', 'require'))
    if os.environ.get('SUPABASE_DB_SSLROOTCERT'):
        kw['sslrootcert'] = os.environ['SUPABASE_DB_SSLROOTCERT']
    return psycopg2.connect(dsn, **kw)


def write(conn, title, sid, master, weekly, adj, skipped):
    with conn.cursor() as cur:
        cur.execute("set statement_timeout = '300000'")
        cur.execute('select count(*) from public.ar_sheet_adjustments')
        before = cur.fetchone()[0]
        if adj is not None and before > 20 and len(adj) < before / 2:
            raise SystemExit(f'read {len(adj)} register lines against {before} loaded: refusing (a half-filled sheet?)')
        if master:
            upd = ', '.join(f'{c} = excluded.{c}' for c in M_COLS if c != 'customer_no')
            execute_values(cur, f'insert into public.ar_sheet_customer_master ({", ".join(M_COLS)}) values %s '
                                f'on conflict (customer_no) do update set {upd}, loaded_at = now()',
                           [tuple(r[c] for c in M_COLS) for r in master])
        for as_on, rows in weekly.items():
            cur.execute('delete from public.ar_sheet_weekly where as_on = %s', (as_on,))
            execute_values(cur, f'insert into public.ar_sheet_weekly ({", ".join(W_COLS)}) values %s',
                           [tuple(r[c] for c in W_COLS) for r in rows])
        if adj is not None:                          # None = the tab was missing: keep the register as loaded
            cur.execute('delete from public.ar_sheet_adjustments')
        if adj:
            execute_values(cur, f'insert into public.ar_sheet_adjustments ({", ".join(A_COLS)}) values %s',
                           [tuple(r[c] for c in A_COLS) for r in adj])
        try:
            cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) "
                        "values (%s, %s, now(), now(), 'success', %s::jsonb, %s, %s, now())",
                        ('sheet_grn_loader:ar_sheet', 'ar_sheet',
                         json.dumps({'sheet': title, 'sheet_id': sid, 'customers': len(master),
                                     'weekly_tabs': {k.isoformat(): len(v) for k, v in weekly.items()},
                                     'register_lines': None if adj is None else len(adj), 'register_replaced': before, 'skipped': skipped}),
                         len(adj or []) + sum(len(v) for v in weekly.values()), len(master)))
        except Exception as e:                                # noqa: BLE001
            log.warning('workflow_logs not written: %s', str(e)[:100])
    conn.commit()
    return before


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    c = creds()
    sid = find_sheet(c)
    api = build('sheets', 'v4', credentials=c, cache_discovery=False)
    meta = api.spreadsheets().get(spreadsheetId=sid, fields='properties(title),sheets(properties(title))').execute()
    title, names = meta['properties']['title'], [s['properties']['title'] for s in meta['sheets']]
    log.info('sheet: %s (%s); tabs: %s', title, sid, ', '.join(names))
    skipped = defaultdict(int)

    mt = find_tab(names, 'Customer Master')
    if not mt:
        raise SystemExit(f'no Customer Master tab in {title}')
    master = read_master(grid(api, sid, mt), sid)

    weekly = {}
    for n in names:
        d = tab_date(n)
        if d:
            weekly[d] = read_weekly(grid(api, sid, n), n, d, sid)
    if not weekly:
        raise SystemExit(f'no weekly tab (named dd.mm.yy) in {title}')

    at = find_tab(names, 'Receivable Adj. Details')
    adj = read_adjustments(grid(api, sid, at), sid, skipped) if at else []
    if not at:
        log.warning('no Receivable Adj. Details tab: the register is left as loaded')

    log.info('customer master: %d customers, %d groups', len(master), len({r['customer_group'] for r in master}))
    for d, rows in sorted(weekly.items()):
        rec = sum((r['sheet'].adapted or {}).get('receivable') or 0 for r in rows)
        log.info('weekly %s: %d groups, receivable %.2f, %d with comments', d, len(rows), rec, sum(1 for r in rows if r['comments']))
    per = Counter()
    for r in adj:
        per[r['kind']] += r['value']
    log.info('register: %d lines -- %s', len(adj), ', '.join(f'{k} {v:,.0f}' for k, v in per.items()))
    for k, v in skipped.items():
        log.info('skipped: %s x %d', k, v)
    if a.dry_run:
        log.info('dry run: nothing written')
        return
    if not master:
        raise SystemExit('no customers read from the Customer Master: refusing')
    conn = connect()
    try:
        write(conn, title, sid, master, weekly, adj if at else None, dict(skipped))
    finally:
        conn.close()
    log.info('done')


if __name__ == '__main__':
    main()
