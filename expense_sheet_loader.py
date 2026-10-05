#!/usr/bin/env python3
"""Load the finance team's expense tagging for Birbal's Expense Analysis board (Birbal migration 151).

Source: the Google Sheet "Expenses Analysis Report FY27" (1siBXpxc4R1REY0Oi89BRvwetGrlJ2aSWcFhmM-6iyIg,
shared with birbal@thebakersdozen.in; EXPENSE_SHEET_ID overrides it when the team starts a new file):

  ERP_Raw_Data   one row per expense G/L entry with the team's Department, Type of Exp and Expense
                 Month                                                   -> public.expense_sheet_lines
  Names & Codes  section 1: G/L account -> provision account; section 2: party -> short name, group
  MIS Mapping    G/L account -> PROVISION Group / Sub Group
                 G/L account -> department, type of exp, MIS heads        -> public.expense_gl_map
                 party -> short name, group                               -> public.expense_party_map

Columns are found by their header NAME, so a moved or added column is fine. Birbal reads the G/L itself
(bc_general_ledger_entries); from the sheet it takes only the team's own judgement: which department and
head an account belongs to, and which month each line's cost belongs to (see the rules in migration 151).

Every run REPLACES the three tables in one transaction (the sheet is the year to date). A read with less
than half the tagged lines already loaded is refused rather than written (a half-filled new file).

Environment: SUPABASE_DB_URL (+ SUPABASE_DB_SSLMODE / SUPABASE_DB_SSLROOTCERT), EXPENSE_SHEET_TOKEN_JSON
(path to birbal@'s Google token, Sheets or Drive read scope; default
D:\\Python\\Birbal\\salary_loader\\token_birbal_drive.json), EXPENSE_SHEET_ID (optional).

    python expense_sheet_loader.py --dry-run     # read + check, write nothing
    python expense_sheet_loader.py               # replace the three tables
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
from psycopg2.extras import execute_values
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:                                            # noqa: BLE001
    pass

SHEET_ID = os.environ.get('EXPENSE_SHEET_ID') or '1siBXpxc4R1REY0Oi89BRvwetGrlJ2aSWcFhmM-6iyIg'
DEFAULT_TOKEN = r'D:\Python\Birbal\salary_loader\token_birbal_drive.json'
SHEETS_EPOCH = dt.date(1899, 12, 30)
MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}

# the team's MIS Mapping group -> the department its tagged lines use
GROUP_DEPARTMENT = {
    'corporate oh': 'Corporate OH', 'manufacturing exp': 'MFG', 'salary exp': 'HR', 'rent & utilities': 'Rent & Utilities',
    'logistics cost': 'SCM', 'freight': 'SCM', 'marketing': 'Marketing',
}
# ... and its sub group -> the Type of Exp its tagged lines use (MFG and Corporate OH only)
SUBGROUP_TYPE = {
    'others': 'Others', 'legal & professional exp': 'Consultancy & Legal', 'ineligible gst': 'Ineligible GST',
    'travelling exp': 'Travelling', 'software exp': 'Software', 'testing exp': 'Testing', 'housekeeping exp': 'HK',
    'repairing & maintainance': 'Repair & Miantainance', 'consumables & utility': 'EG', 'gas consumption': 'Gas',
    'labour charges': 'Manpower', 'transportation service to employees': 'Staff Transportation',
}
# accounts the Names & Codes tab lists that neither tagged lines nor the MIS Mapping place
FALLBACK = {
    '46320010': ('Corporate OH', 'Consultancy & Legal'),     # STATUTORY AUDIT FEES
    '46320020': ('Corporate OH', 'Consultancy & Legal'),     # TAX AUDIT FEES
}
# payroll: salary, stipend, director remuneration, bonus, PL encashment, notice pay, PF / ESIC / LWF, gratuity
PAYROLL_PREFIXES = ('4211', '4221', '4231')

log = logging.getLogger('expense_sheet')


def sheets_api():
    path = os.environ.get('EXPENSE_SHEET_TOKEN_JSON') or DEFAULT_TOKEN
    creds = Credentials.from_authorized_user_file(path)
    if not creds.valid:
        creds.refresh(Request())
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def norm(s) -> str:
    return re.sub(r'\s+', ' ', str(s if s is not None else '')).strip()


def key(s) -> str:
    return norm(s).lower()


def tabs(api):
    meta = api.spreadsheets().get(spreadsheetId=SHEET_ID, fields='properties(title),sheets(properties(title))').execute()
    return meta['properties']['title'], [s['properties']['title'] for s in meta['sheets']]


def grid(api, tab):
    return api.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{tab}'", valueRenderOption='UNFORMATTED_VALUE',
        dateTimeRenderOption='SERIAL_NUMBER').execute().get('values', [])


def find_tab(names, want):
    for n in names:
        if key(n) == key(want):
            return n
    for n in names:
        if key(want) in key(n):
            return n
    return None


def gl_code(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = norm(v)
    return s[:-2] if re.fullmatch(r'\d+\.0', s) else s


def as_date(v):
    """A posting-date cell: a Sheets serial number, or text like 16-Apr-2026 / 16-04-2026 / 2026-04-16."""
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        return SHEETS_EPOCH + dt.timedelta(days=int(v))
    s = norm(v)
    for f in ('%d-%b-%Y', '%d-%B-%Y', '%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d', '%d %B %Y', '%d %b %Y'):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def as_month(v):
    """(first of month, None) for a month cell; (None, label) for the team's labels ('Previous Year')."""
    if v in (None, ''):
        return None, None
    if isinstance(v, (int, float)) and v > 1000:
        d = SHEETS_EPOCH + dt.timedelta(days=int(v))
        return d.replace(day=1), None
    d = as_date(v)
    if d:
        return d.replace(day=1), None
    s = norm(v)
    m = re.fullmatch(r'([A-Za-z]+)[-/ \']*(\d{2,4})', s)                     # Apr-26 | April 2026
    if m and m.group(1)[:3].lower() in MONTHS:
        y = int(m.group(2))
        return dt.date(y + 2000 if y < 100 else y, MONTHS[m.group(1)[:3].lower()], 1), None
    return None, s


def amount(v):
    if v in (None, ''):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    try:
        return round(float(norm(v).replace(',', '')), 2)
    except ValueError:
        return None


def header_index(row, names):
    idx = {}
    for j, h in enumerate(row):
        k = key(h)
        for field, aliases in names.items():
            if field not in idx and k in aliases:
                idx[field] = j
    return idx


LINE_HEADERS = {
    'type': {'type of exp', 'type of expense'},
    'month': {'expense month'},
    'department': {'department'},
    'gl_no': {'g/l account no.', 'g/l account no', 'gl account no', 'g_l_account_no'},
    'gl_name': {'g/l account name', 'gl account name', 'g_l_account_name'},
    'party': {'party name'},
    'posting_date': {'posting date'},
    'document_no': {'document no.', 'document no'},
    'amount': {'amount (lcy)', 'amount'},
}


def read_lines(g, skipped):
    hi = next((i for i, r in enumerate(g[:5]) if {'gl_no', 'document_no', 'posting_date', 'amount', 'month'}
               <= set(header_index(r, LINE_HEADERS))), None)
    if hi is None:
        raise SystemExit('ERP_Raw_Data: no header row with G/L Account No., Document No., Posting Date, Amount (LCY), Expense Month')
    idx = header_index(g[hi], LINE_HEADERS)
    out = []
    for n, row in enumerate(g[hi + 1:], start=hi + 2):
        cell = lambda k: row[idx[k]] if k in idx and idx[k] < len(row) else None   # noqa: E731
        gl, doc = gl_code(cell('gl_no')), norm(cell('document_no'))
        if not gl and not doc:
            continue
        pdate, amt = as_date(cell('posting_date')), amount(cell('amount'))
        month, label = as_month(cell('month'))
        if not gl or not doc or pdate is None or amt is None:
            skipped['line without account, document, posting date or amount'] += 1
            continue
        if month is None and label is None:
            skipped['line without an expense month (left to the rule)'] += 1
            continue
        out.append({'sheet_row': n, 'document_no': doc, 'gl_no': gl, 'posting_date': pdate, 'amount': amt,
                    'expense_month': month, 'month_label': label,
                    'department': norm(cell('department')) or None, 'type_of_exp': norm(cell('type')) or None,
                    'party_name': norm(cell('party')) or None, 'gl_name': norm(cell('gl_name')) or None,
                    'source_sheet_id': SHEET_ID})
    return out


def read_mis_mapping(g):
    """{gl: (name, group, sub group)} from the MIS Mapping tab."""
    hi = next((i for i, r in enumerate(g[:5]) if r and key(r[0]) in ('g_l_account_no', 'g/l account no.', 'gl code')), None)
    out = {}
    if hi is None:
        return out
    for row in g[hi + 1:]:
        row = list(row) + [''] * 4
        gl = gl_code(row[0])
        if re.fullmatch(r'\d{8}', gl):
            out[gl] = (norm(row[1]) or None, norm(row[2]) or None, norm(row[3]) or None)
    return out


def read_names_codes(g):
    """({gl: (name, provision gl)}, [(party, short, group)]) from the Names & Codes tab."""
    hi = next((i for i, r in enumerate(g[:5]) if any(key(c) == 'erp gl_no' for c in r)), None)
    gls, parties = {}, []
    if hi is None:
        return gls, parties
    h = [key(c) for c in g[hi]]
    gl_c = h.index('erp gl_no')
    prov_c = h.index('provision gl') if 'provision gl' in h else None
    party_c = h.index('party name') if 'party name' in h else None
    group_c = h.index('channel', party_c) if party_c is not None and 'channel' in h[party_c:] else None
    for row in g[hi + 1:]:
        row = list(row) + [''] * (len(h) + 2)
        gl = gl_code(row[gl_c])
        if re.fullmatch(r'\d{8}', gl):
            gls[gl] = (norm(row[gl_c + 1]) or None, gl_code(row[prov_c]) if prov_c is not None else None)
        if party_c is not None and norm(row[party_c]):
            parties.append((norm(row[party_c]), norm(row[party_c + 1]) or None,
                            norm(row[group_c]) or None if group_c is not None else None))
    return gls, parties


def build_gl_map(lines, mis, nc):
    by = defaultdict(Counter)
    names = {}
    for l in lines:
        by[l['gl_no']][(l['department'], l['type_of_exp'])] += 1
        names.setdefault(l['gl_no'], l['gl_name'])
    out = []
    for gl in sorted(set(by) | set(nc)):
        m_name, group, sub = mis.get(gl, (None, None, None))
        n_name, prov = nc.get(gl, (None, None))
        if gl in by:
            (dep, typ), _ = by[gl].most_common(1)[0]
            if len(by[gl]) > 1:
                log.warning('account %s carries %d department/type pairs in the sheet; using the commonest', gl, len(by[gl]))
            source = 'lines'
        elif gl in FALLBACK:
            dep, typ = FALLBACK[gl]
            source = 'names & codes'
        else:
            dep = GROUP_DEPARTMENT.get(key(group))
            typ = SUBGROUP_TYPE.get(key(sub), sub) if dep in ('MFG', 'Corporate OH') else None
            source = 'mis mapping' if group else 'names & codes'
        out.append({'gl_no': gl, 'gl_name': names.get(gl) or n_name or m_name, 'department': dep or 'Other',
                    'type_of_exp': typ, 'mis_group': group, 'mis_sub_group': sub, 'provision_gl': prov or None,
                    'payroll': gl.startswith(PAYROLL_PREFIXES), 'source': source})
    return out


def build_party_map(parties):
    out = {}
    for name, short, group in parties:
        k = name.upper()
        if k not in out:                       # the first listing wins (the tab repeats a few names)
            out[k] = {'party_key': k, 'party_name': name, 'short_name': short, 'party_group': group}
    return list(out.values())


L_COLS = ['sheet_row', 'document_no', 'gl_no', 'posting_date', 'amount', 'expense_month', 'month_label',
          'department', 'type_of_exp', 'party_name', 'source_sheet_id']
G_COLS = ['gl_no', 'gl_name', 'department', 'type_of_exp', 'mis_group', 'mis_sub_group', 'provision_gl', 'payroll', 'source']
P_COLS = ['party_key', 'party_name', 'short_name', 'party_group']


def connect():
    dsn = os.environ['SUPABASE_DB_URL']
    kw = dict(sslmode=os.environ.get('SUPABASE_DB_SSLMODE', 'require'))
    if os.environ.get('SUPABASE_DB_SSLROOTCERT'):
        kw['sslrootcert'] = os.environ['SUPABASE_DB_SSLROOTCERT']
    return psycopg2.connect(dsn, **kw)


def write(conn, title, lines, gl_map, party_map, skipped):
    with conn.cursor() as cur:
        cur.execute("set statement_timeout = '600000'")
        cur.execute('select count(*) from public.expense_sheet_lines')
        before = cur.fetchone()[0]
        if before > 100 and len(lines) < before / 2:
            raise SystemExit(f'read {len(lines)} tagged lines against {before} loaded: refusing (a half-filled sheet?)')
        cur.execute('delete from public.expense_sheet_lines')
        execute_values(cur, f'insert into public.expense_sheet_lines ({", ".join(L_COLS)}) values %s',
                       [tuple(r[c] for c in L_COLS) for r in lines], page_size=1000)
        if gl_map:
            cur.execute('delete from public.expense_gl_map')
            execute_values(cur, f'insert into public.expense_gl_map ({", ".join(G_COLS)}) values %s',
                           [tuple(r[c] for c in G_COLS) for r in gl_map])
        if party_map:
            cur.execute('delete from public.expense_party_map')
            execute_values(cur, f'insert into public.expense_party_map ({", ".join(P_COLS)}) values %s',
                           [tuple(r[c] for c in P_COLS) for r in party_map])
        try:
            months = sorted({(r['expense_month'].isoformat() if r['expense_month'] else r['month_label']) for r in lines})
            cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) "
                        "values (%s, %s, now(), now(), 'success', %s::jsonb, %s, %s, now())",
                        ('sheet_grn_loader:expense_sheet', 'expense_sheet',
                         json.dumps({'sheet': title, 'sheet_id': SHEET_ID, 'lines': len(lines), 'replaced': before,
                                     'accounts': len(gl_map), 'parties': len(party_map), 'months': months,
                                     'skipped': skipped}),
                         len(lines), len(lines)))
        except Exception as e:                                # noqa: BLE001
            log.warning('workflow_logs not written: %s', str(e)[:100])
    conn.commit()
    return before


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    api = sheets_api()
    title, names = tabs(api)
    log.info('sheet: %s (%s)', title, SHEET_ID)
    skipped = defaultdict(int)
    raw_tab = find_tab(names, 'ERP_Raw_Data')
    if not raw_tab:
        raise SystemExit(f'no ERP_Raw_Data tab in {title}: {names}')
    lines = read_lines(grid(api, raw_tab), skipped)
    mis_tab, nc_tab = find_tab(names, 'MIS Mapping'), find_tab(names, 'Names & Codes')
    mis = read_mis_mapping(grid(api, mis_tab)) if mis_tab else {}
    nc, parties = read_names_codes(grid(api, nc_tab)) if nc_tab else ({}, [])
    gl_map, party_map = build_gl_map(lines, mis, nc), build_party_map(parties)

    per = Counter(r['expense_month'].strftime('%b-%y') if r['expense_month'] else r['month_label'] for r in lines)
    log.info('tagged lines: %d (%.2f) -- %s', len(lines), sum(r['amount'] for r in lines),
             ', '.join(f'{k} ({v})' for k, v in per.items()))
    log.info('accounts: %d (%s); parties: %d', len(gl_map),
             ', '.join(f'{k} {v}' for k, v in Counter(g['source'] for g in gl_map).items()), len(party_map))
    for k, v in skipped.items():
        log.info('skipped: %s x %d', k, v)
    if a.dry_run:
        log.info('dry run: nothing written')
        return
    if not lines:
        raise SystemExit('no tagged lines read: refusing to empty the table')
    conn = connect()
    try:
        before = write(conn, title, lines, gl_map, party_map, dict(skipped))
    finally:
        conn.close()
    log.info('expense_sheet_lines: %d replaced by %d', before, len(lines))


if __name__ == '__main__':
    main()
