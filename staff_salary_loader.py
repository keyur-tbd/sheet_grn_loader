#!/usr/bin/env python3
"""Load field staff (TSI / promoter) cost by store into public.pnl_store_salaries -- WITHOUT NAMES.

Source: the "Salary Detail" Google Sheet (1h9kKr91dLNruatRleBAIUmndTQk8rewhobzvS4Uj7cU), tab
"Salaries promoters Working", shared with birbal@thebakersdozen.in only. One row per month x store
(Erp Name = the ship-to) x person. A person's pay is split over the stores they cover by each store's
adjusted gross billing (Salary Split), so the month's splits add up to the month's pay.

Birbal migrations 132-133 own the table: warehouse.store_staff_cost (month x store snapshot, refreshed
after every load) is what the boards and Ask read, and v_pnl_lines allocates the same rows to trade lines by party (CM2 "store salaries").

CONFIDENTIAL. Nothing that identifies a person is written or logged:
  * the name becomes person_key = HMAC-SHA256(name, a key drawn fresh on every run)[:16] -- distinct
    within one load (head counts), linkable to no one, different on the next load;
  * the "Promoter / TSI" short-name column and the person's whole Salary are not stored;
  * logs carry row counts and months only, never a name or a rupee figure.

Every run REPLACES the table in one transaction (the sheet is the whole history). A read that comes
back with less than half the rows already loaded is refused rather than written.

Environment: SUPABASE_DB_URL (+ SUPABASE_DB_SSLMODE / SUPABASE_DB_SSLROOTCERT),
STAFF_SALARY_TOKEN_JSON (path to birbal@'s token; default D:\\Python\\Birbal\\salary_loader\\token_birbal.json).

    python staff_salary_loader.py --dry-run     # read + check, write nothing
    python staff_salary_loader.py               # replace public.pnl_store_salaries
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
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

SHEET_ID = '1h9kKr91dLNruatRleBAIUmndTQk8rewhobzvS4Uj7cU'
TAB = 'Salaries promoters Working'
DEFAULT_TOKEN = r'D:\Python\Birbal\salary_loader\token_birbal.json'
SHEETS_EPOCH = dt.date(1899, 12, 30)
MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}

# sheet header (lower-cased, spaces collapsed) -> field. Matched by NAME: a moved column is fine, a
# missing one stops the load. The tab carries a party x month pivot to the right of the data
# (a second "Party Name" header), so only the FIRST occurrence of a header counts.
HEADERS = {
    'party name': 'party_name',
    'erp name': 'erp_name',
    'city': 'city',
    'designation': 'designation',
    'salary name': 'name',                 # hashed, never stored
    'firm': 'firm',
    'adj gross billing': 'adj_gross_billing',
    'salary split': 'salary_split',
    'month': 'month',
}

log = logging.getLogger('staff_salary')


def sheets_client():
    path = os.environ.get('STAFF_SALARY_TOKEN_JSON') or DEFAULT_TOKEN
    creds = Credentials.from_authorized_user_file(path)
    if not creds.valid:
        creds.refresh(Request())
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def norm(s) -> str:
    return re.sub(r'\s+', ' ', str(s if s is not None else '')).strip()


def parse_month(v) -> dt.date:
    """'1-April-26', '01-Apr-2026', a Sheets date serial, or an ISO date -> first of the month."""
    if isinstance(v, (int, float)):
        d = SHEETS_EPOCH + dt.timedelta(days=int(v))
        return d.replace(day=1)
    s = norm(v)
    m = re.fullmatch(r'(\d{1,2})[-/ ]([A-Za-z]+)[-/ ](\d{2,4})', s)
    if m and m.group(2)[:3].lower() in MONTHS:
        y = int(m.group(3))
        return dt.date(y + 2000 if y < 100 else y, MONTHS[m.group(2)[:3].lower()], 1)
    m = re.fullmatch(r'(\d{4})-(\d{2})(?:-\d{2})?', s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), 1)
    raise ValueError(f'month cell {s!r} is not a month')


def num(v):
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(',', '').replace('₹', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def read_rows(svc):
    grid = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{TAB}'!A1:Z",
        valueRenderOption='UNFORMATTED_VALUE', dateTimeRenderOption='FORMATTED_STRING',
    ).execute().get('values', [])
    if not grid:
        raise SystemExit(f'tab {TAB!r} is empty')
    idx = {}
    for i, h in enumerate(grid[0]):
        key = norm(h).lower()
        if key in HEADERS and HEADERS[key] not in idx:
            idx[HEADERS[key]] = i
    missing = sorted(set(HEADERS.values()) - set(idx))
    if missing:
        raise SystemExit(f'tab {TAB!r}: header(s) not found: {", ".join(missing)}. Refusing to load.')

    key = secrets.token_bytes(32)        # per-run: person_key links to no one across loads
    agg = {}
    skipped = defaultdict(int)
    for n, row in enumerate(grid[1:], start=2):
        cell = lambda f: row[idx[f]] if idx[f] < len(row) else None   # noqa: E731
        party, erp, name, month = norm(cell('party_name')), norm(cell('erp_name')), norm(cell('name')), cell('month')
        if not (party or erp or name):
            continue                                  # blank line
        if not (party and erp and name and month not in (None, '')):
            skipped['incomplete'] += 1
            continue
        try:
            m = parse_month(month)
        except ValueError:
            skipped['bad month'] += 1
            continue
        split = num(cell('salary_split'))
        if split is None:
            skipped['no split'] += 1
            continue
        pk = hmac.new(key, name.upper().encode('utf-8'), hashlib.sha256).hexdigest()[:16]
        k = (m, party, erp, pk)
        rec = agg.get(k)
        if rec is None:
            agg[k] = rec = {
                'month': m, 'party_name': party, 'erp_name': erp, 'person_key': pk,
                'city': norm(cell('city')) or None, 'designation': norm(cell('designation')).upper() or None,
                'firm': norm(cell('firm')) or None, 'adj_gross_billing': num(cell('adj_gross_billing')),
                'salary_split': 0.0,
            }
        else:
            skipped['same person twice at a store (summed)'] += 1
        rec['salary_split'] += split
    return list(agg.values()), dict(skipped)


COLS = ['month', 'party_name', 'erp_name', 'person_key', 'city', 'designation', 'firm',
        'adj_gross_billing', 'salary_split']


def connect():
    dsn = os.environ['SUPABASE_DB_URL']
    kw = dict(sslmode=os.environ.get('SUPABASE_DB_SSLMODE', 'require'))
    if os.environ.get('SUPABASE_DB_SSLROOTCERT'):
        kw['sslrootcert'] = os.environ['SUPABASE_DB_SSLROOTCERT']
    return psycopg2.connect(dsn, **kw)


def write(conn, rows, months):
    with conn.cursor() as cur:
        cur.execute("set statement_timeout = '600000'")
        cur.execute('select count(*) from public.pnl_store_salaries')
        before = cur.fetchone()[0]
        if before and len(rows) < before / 2:
            raise SystemExit(f'read {len(rows)} rows against {before} loaded: refusing (half-read sheet?)')
        cur.execute('delete from public.pnl_store_salaries')
        execute_values(cur, f'insert into public.pnl_store_salaries ({", ".join(COLS)}) values %s',
                       [tuple(r[c] for c in COLS) for r in rows], page_size=1000)
        try:
            cur.execute("select 1 from information_schema.tables where table_schema='public' and table_name='workflow_logs'")
            if cur.fetchone():
                cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) "
                            "values (%s, %s, now(), now(), 'success', %s::jsonb, %s, %s, now())",
                            ('sheet_grn_loader:staff_salary', 'staff_salary',
                             json.dumps({'months': [m.isoformat() for m in months], 'replaced': before}), len(rows), len(rows)))
        except Exception as e:                                # noqa: BLE001
            log.warning('workflow_logs not written: %s', str(e)[:100])
    conn.commit()
    # the boards read store_staff_cost as a snapshot (Birbal migration 133); refresh it now, not in 3 hours
    try:
        with conn.cursor() as cur:
            cur.execute('select app.refresh_store_staff()')
            log.info('store snapshots: %s', cur.fetchone()[0])
        conn.commit()
    except Exception as e:                                    # noqa: BLE001
        conn.rollback()
        log.warning('store snapshot refresh failed (the 3-hourly beat will catch up): %s', str(e)[:120])
    return before


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    rows, skipped = read_rows(sheets_client())
    months = sorted({r['month'] for r in rows})
    per = defaultdict(int)
    for r in rows:
        per[r['month']] += 1
    log.info('read %d store x person rows, %d stores, months %s', len(rows),
             len({r['erp_name'] for r in rows}), ', '.join(f'{m:%b-%y} ({per[m]})' for m in months))
    if skipped:
        log.info('skipped / folded: %s', ', '.join(f'{k} {v}' for k, v in skipped.items()))
    if not rows:
        raise SystemExit('nothing read')
    if a.dry_run:
        log.info('dry run: nothing written')
        return
    conn = connect()
    try:
        before = write(conn, rows, months)
    finally:
        conn.close()
    log.info('replaced %d rows with %d', before, len(rows))


if __name__ == '__main__':
    sys.exit(main())
