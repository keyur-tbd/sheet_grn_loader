#!/usr/bin/env python3
"""Load the partners' RTV support lists and the fixed RTV % in the terms of trade for Birbal's RTV
Analysis board (Birbal migration 141).

Source: the Drive folder "Platform wise RTV Data" (1vlVTGF3biSMxlJox7_OpkhZVuMRkxNRA, owned by zepto@,
shared with birbal@thebakersdozen.in):

  <Platform>/...                         one sub-folder per partner (Blinkit, Instamart, Zepto, Flipkart).
                                         Every Google Sheet, .xlsx or .csv in it (sub-folders too) is read,
                                         every tab: a header row with Month | Item ID | SKU Name | City |
                                         Alignment, one row per product x city x month the partner takes
                                         back in full ("100% RTV").           -> public.rtv_support_lines
  Overall Fixed RTV TOT - Platform wise/  "Platform wise - RTV TOT": Platform | Category | RTV %, the fixed
                                         RTV % per platform for Breads and HSL. A blank Platform cell
                                         repeats the one above (the sheet merges it). -> public.rtv_fixed_tot

Headers are matched by NAME in the first five rows, so a moved or added column is fine; a tab without
the needed headers is skipped and logged. The sheets name months without a year ("June"): a row's year
is worked back from the sheet's order -- the last month is the latest such month no later than next
month, and walking up the sheet a month that is later in the year than the one below it belongs to the
year before. A month cell that carries a year ("Jun-26", "01-06-2026", a date) keeps it.

Every run REPLACES both tables in one transaction (the folder is the whole history), then rebuilds Birbal's
warehouse.rtv_analysis snapshot (app.refresh_rtv_analysis(), migration 142). A read with less
than half the support rows already loaded is refused rather than written (a half-shared folder).

Environment: SUPABASE_DB_URL (+ SUPABASE_DB_SSLMODE / SUPABASE_DB_SSLROOTCERT),
RTV_SUPPORT_TOKEN_JSON (path to birbal@'s Drive token, scope drive.readonly; default
D:\\Python\\Birbal\\salary_loader\\token_birbal_drive.json).

    python rtv_support_loader.py --dry-run     # read + check, write nothing
    python rtv_support_loader.py               # replace public.rtv_support_lines + public.rtv_fixed_tot
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
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
from googleapiclient.http import MediaIoBaseDownload

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:                                            # noqa: BLE001
    pass

ROOT_FOLDER = '1vlVTGF3biSMxlJox7_OpkhZVuMRkxNRA'
DEFAULT_TOKEN = r'D:\Python\Birbal\salary_loader\token_birbal_drive.json'
FOLDER_MIME = 'application/vnd.google-apps.folder'
SHEET_MIME = 'application/vnd.google-apps.spreadsheet'
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
SHEETS_EPOCH = dt.date(1899, 12, 30)
MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}

# header (lower-cased, spaces collapsed) -> field
# Instamart and Zepto write their own headers (2026-10-04: their folders were still empty), so the usual
# spellings of each are accepted; '_' counts as a space. Zepto's id may be its numeric SKU code or the EAN --
# Birbal's rtv_support view (migration 145) matches either, and falls back on the SKU name.
SUPPORT_HEADERS = {
    'month': 'month', 'month name': 'month', 'period': 'month',
    'item id': 'item_id', 'item code': 'item_id', 'sku id': 'item_id', 'sku code': 'item_id', 'product id': 'item_id',
    'product code': 'item_id', 'article code': 'item_id', 'ean': 'item_id', 'ean code': 'item_id',
    'zepto sku id': 'item_id', 'zepto sku code': 'item_id', 'swiggy item code': 'item_id', 'instamart item code': 'item_id',
    'sku name': 'sku_name', 'item name': 'sku_name', 'product name': 'sku_name', 'sku': 'sku_name',
    'product': 'sku_name', 'item': 'sku_name', 'item description': 'sku_name', 'product description': 'sku_name',
    'city': 'city', 'city name': 'city', 'location': 'city', 'region': 'city',
    'alignment': 'alignment', 'support': 'alignment', 'rtv support': 'alignment', 'rtv %': 'alignment',
    'dump support': 'alignment', 'support %': 'alignment',
}
SUPPORT_NEEDED = {'month', 'city'}            # and an item id or an SKU name (checked in header_row)
TOT_HEADERS = {'platform': 'platform', 'category': 'category', 'rtv %': 'rtv_pct', 'rtv%': 'rtv_pct', 'fixed rtv %': 'rtv_pct'}

log = logging.getLogger('rtv_support')


def google():
    path = os.environ.get('RTV_SUPPORT_TOKEN_JSON') or DEFAULT_TOKEN
    creds = Credentials.from_authorized_user_file(path)
    if not creds.valid:
        creds.refresh(Request())
    return (build('drive', 'v3', credentials=creds, cache_discovery=False),
            build('sheets', 'v4', credentials=creds, cache_discovery=False))


def norm(s) -> str:
    return re.sub(r'\s+', ' ', str(s if s is not None else '')).strip()


def platform_of(folder_name: str) -> str:
    n = folder_name.lower()
    for key, name in (('blinkit', 'Blinkit'), ('instamart', 'Instamart'), ('swiggy', 'Instamart'),
                      ('zepto', 'Zepto'), ('flipkart', 'Flipkart')):
        if key in n:
            return name
    return norm(folder_name)


def children(drive, folder_id):
    out, token = [], None
    while True:
        r = drive.files().list(q=f"'{folder_id}' in parents and trashed=false", pageSize=1000, pageToken=token,
                               fields='nextPageToken, files(id,name,mimeType,modifiedTime)',
                               supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        out += r.get('files', [])
        token = r.get('nextPageToken')
        if not token:
            return out


def files_under(drive, folder_id, path=''):
    """Every readable file below a folder, depth first, with its folder path."""
    for f in sorted(children(drive, folder_id), key=lambda x: x['name']):
        if f['mimeType'] == FOLDER_MIME:
            yield from files_under(drive, f['id'], f'{path}{f["name"]}/')
        elif f['mimeType'] == SHEET_MIME or f['mimeType'] == XLSX_MIME or f['name'].lower().endswith(('.xlsx', '.csv')):
            yield path, f


def grids(drive, sheets, f):
    """[(tab name, rows)] for a Google Sheet, an .xlsx or a .csv."""
    if f['mimeType'] == SHEET_MIME:
        meta = sheets.spreadsheets().get(spreadsheetId=f['id'], fields='sheets(properties(title))').execute()
        out = []
        for s in meta['sheets']:
            t = s['properties']['title']
            v = sheets.spreadsheets().values().get(
                spreadsheetId=f['id'], range=f"'{t}'", valueRenderOption='UNFORMATTED_VALUE',
                dateTimeRenderOption='SERIAL_NUMBER').execute().get('values', [])
            out.append((t, v))
        return out
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=f['id'], supportsAllDrives=True))
    done = False
    while not done:
        _, done = dl.next_chunk()
    if f['name'].lower().endswith('.csv'):
        return [('csv', list(csv.reader(io.StringIO(buf.getvalue().decode('utf-8-sig')))))]
    import openpyxl
    wb = openpyxl.load_workbook(buf, read_only=True, data_only=True)
    return [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]


def header_row(rows, headers, needed):
    """(row index, {field: column}) of the first of five rows that names every needed field."""
    for i, row in enumerate(rows[:5]):
        idx = {}
        for j, h in enumerate(row):
            key = norm(str(h if h is not None else '').replace('_', ' ')).lower()
            if key in headers and headers[key] not in idx:
                idx[headers[key]] = j
        if needed <= set(idx) and (headers is not SUPPORT_HEADERS or 'item_id' in idx or 'sku_name' in idx):
            return i, idx
    return None, None


def pct(v):
    """'100% RTV' -> 1.0, '15%' -> 0.15, 0.15 -> 0.15, 15 -> 0.15, blank -> None."""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v) / 100 if v > 1 else float(v)
    s = norm(v).lower()
    m = re.search(r'(\d+(?:\.\d+)?)\s*%', s)
    if m:
        return float(m.group(1)) / 100
    if s in ('full', 'full rtv', 'yes', 'y'):
        return 1.0
    try:
        x = float(s)
        return x / 100 if x > 1 else x
    except ValueError:
        return None


def month_parts(v):
    """(month number, year or None) of a month cell; None if it is not a month."""
    if isinstance(v, dt.datetime):
        return v.month, v.year
    if isinstance(v, dt.date):
        return v.month, v.year
    if isinstance(v, (int, float)):
        if 1 <= v <= 12:
            return int(v), None
        d = SHEETS_EPOCH + dt.timedelta(days=int(v))
        return d.month, d.year
    s = norm(v)
    m = re.fullmatch(r'([A-Za-z]+)[-/ \']*(\d{2,4})?', s)                     # June | Jun-26 | June 2026
    if m and m.group(1)[:3].lower() in MONTHS:
        y = m.group(2)
        return MONTHS[m.group(1)[:3].lower()], (int(y) + 2000 if y and len(y) == 2 else int(y) if y else None)
    m = re.fullmatch(r'\d{1,2}[-/ ]([A-Za-z]+)[-/ ](\d{2,4})', s)            # 01-Jun-2026
    if m and m.group(1)[:3].lower() in MONTHS:
        y = int(m.group(2))
        return MONTHS[m.group(1)[:3].lower()], y + 2000 if y < 100 else y
    m = re.fullmatch(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', s)                # 01-06-2026 (day first)
    if m:
        return int(m.group(2)), int(m.group(3))
    m = re.fullmatch(r'(\d{4})-(\d{2})(?:-\d{2})?', s)                       # 2026-06(-01)
    if m:
        return int(m.group(2)), int(m.group(1))
    return None


def assign_years(parts, today):
    """Years for month cells that carry none: the last one is the latest such month no later than next
    month, and walking up the sheet a month later in the year than the one below it is a year earlier."""
    limit = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    out = [None] * len(parts)
    year, below = None, None
    for i in range(len(parts) - 1, -1, -1):
        mo, y = parts[i]
        if y is not None:
            year, below = y, mo
        elif year is None:
            year = limit.year if mo <= limit.month else limit.year - 1
            below = mo
        else:
            if mo > below:
                year -= 1
            below = mo
            y = year
        out[i] = dt.date(y if y is not None else year, mo, 1)
    return out


def item_id(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = norm(v)
    return s[:-2] if re.fullmatch(r'\d+\.0', s) else s


def read_support(platform, path, f, tabs, today, skipped):
    rows = []
    for tab, grid in tabs:
        hi, idx = header_row(grid, SUPPORT_HEADERS, SUPPORT_NEEDED)
        if hi is None:
            skipped[f'UNREAD {path}{f["name"]} / {tab}: no Month + City + (Item ID or SKU Name) header'] += 1
            continue
        body = []
        for n, row in enumerate(grid[hi + 1:], start=hi + 2):
            cell = lambda k: row[idx[k]] if k in idx and idx[k] < len(row) else None   # noqa: E731
            iid, mon = item_id(cell('item_id')), cell('month')
            if not iid and norm(cell('sku_name')):
                iid = 'name:' + norm(cell('sku_name'))         # no id column / cell: Birbal matches it by name
            if not iid and mon in (None, ''):
                continue                                       # blank line
            mp = month_parts(mon) if mon not in (None, '') else None
            if not iid or mp is None:
                skipped['incomplete row (no item id / SKU name, or no month)'] += 1
                continue
            body.append((n, mp, iid, row, cell))
        months = assign_years([b[1] for b in body], today)
        for (n, _, iid, row, cell), m in zip(body, months):
            align = cell('alignment')
            rows.append({
                'platform': platform, 'month': m, 'platform_item_id': iid,
                'sku_name': norm(cell('sku_name')) or None,
                'city': norm(cell('city')) or 'Pan India',
                'alignment': norm(align) or None,
                'support_pct': pct(align) if align not in (None, '') else 1.0,
                'source_file': f'{path}{f["name"]} / {tab}', 'source_file_id': f['id'], 'source_row': n,
            })
    return rows


def read_tot(path, f, tabs, skipped):
    rows = []
    for tab, grid in tabs:
        hi, idx = header_row(grid, TOT_HEADERS, {'platform', 'category', 'rtv_pct'})
        if hi is None:
            skipped[f'{path}{f["name"]} / {tab}: no Platform + Category + RTV % header'] += 1
            continue
        platform = None
        for row in grid[hi + 1:]:
            cell = lambda k: row[idx[k]] if idx[k] < len(row) else None          # noqa: E731
            p, c = norm(cell('platform')), norm(cell('category'))
            if p:
                platform = platform_of(p)
            if not c:
                continue
            if not platform:
                skipped['TOT row with no platform above it'] += 1
                continue
            cat = 'Breads' if c.lower().startswith('bread') else 'HSL' if c.lower() == 'hsl' else c
            rows.append({'platform': platform, 'category': cat, 'rtv_pct': pct(cell('rtv_pct')),
                         'source_file': f'{path}{f["name"]} / {tab}', 'source_file_id': f['id']})
    return rows


def read_all(drive, sheets, today):
    support, tot = [], []
    skipped = defaultdict(int)
    for top in sorted(children(drive, ROOT_FOLDER), key=lambda x: x['name']):
        if top['mimeType'] != FOLDER_MIME:
            continue
        is_tot = 'tot' in re.split(r'[^a-z]+', top['name'].lower())     # "Overall Fixed RTV TOT - Platform wise"
        for path, f in files_under(drive, top['id'], f'{top["name"]}/'):
            tabs = grids(drive, sheets, f)
            if is_tot:
                tot += read_tot(path, f, tabs, skipped)
            else:
                support += read_support(platform_of(top['name']), path, f, tabs, today, skipped)
    # one row per key: the highest support wins (two sheets listing the same line agree or the larger stands)
    by = {}
    for r in support:
        k = (r['platform'], r['month'], r['platform_item_id'], r['city'].lower())
        if k in by:
            skipped['same product x city x month listed twice (kept the higher)'] += 1
            if (r['support_pct'] or 0) <= (by[k]['support_pct'] or 0):
                continue
        by[k] = r
    tby = {}
    for r in tot:
        k = (r['platform'], r['category'])
        if k in tby:
            skipped['TOT platform x category listed twice (kept the last)'] += 1
        tby[k] = r
    return list(by.values()), list(tby.values()), dict(skipped)


S_COLS = ['platform', 'month', 'platform_item_id', 'sku_name', 'city', 'alignment', 'support_pct',
          'source_file', 'source_file_id', 'source_row']
T_COLS = ['platform', 'category', 'rtv_pct', 'source_file', 'source_file_id']


def connect():
    dsn = os.environ['SUPABASE_DB_URL']
    kw = dict(sslmode=os.environ.get('SUPABASE_DB_SSLMODE', 'require'))
    if os.environ.get('SUPABASE_DB_SSLROOTCERT'):
        kw['sslrootcert'] = os.environ['SUPABASE_DB_SSLROOTCERT']
    return psycopg2.connect(dsn, **kw)


def write(conn, support, tot, skipped):
    with conn.cursor() as cur:
        cur.execute("set statement_timeout = '600000'")
        cur.execute('select count(*) from public.rtv_support_lines')
        before = cur.fetchone()[0]
        if before > 20 and len(support) < before / 2:
            raise SystemExit(f'read {len(support)} support rows against {before} loaded: refusing (folder half shared?)')
        cur.execute('delete from public.rtv_support_lines')
        if support:
            execute_values(cur, f'insert into public.rtv_support_lines ({", ".join(S_COLS)}) values %s',
                           [tuple(r[c] for c in S_COLS) for r in support], page_size=1000)
        if tot:                                   # an unreadable TOT sheet keeps the last good rates
            cur.execute('delete from public.rtv_fixed_tot')
            execute_values(cur, f'insert into public.rtv_fixed_tot ({", ".join(T_COLS)}) values %s',
                           [tuple(r[c] for c in T_COLS) for r in tot])
        try:
            cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) "
                        "values (%s, %s, now(), now(), 'success', %s::jsonb, %s, %s, now())",
                        ('sheet_grn_loader:rtv_support', 'rtv_support',
                         json.dumps({'support_rows': len(support), 'replaced': before, 'tot_rows': len(tot),
                                     'platforms': sorted({r['platform'] for r in support}),
                                     'months': sorted({r['month'].isoformat() for r in support}),
                                     'skipped': skipped}),
                         len(support) + len(tot), len(support) + len(tot)))
        except Exception as e:                                # noqa: BLE001
            log.warning('workflow_logs not written: %s', str(e)[:100])
    conn.commit()
    # the board reads warehouse.rtv_analysis as a snapshot (Birbal migration 142); rebuild it now, not in 3 hours
    try:
        with conn.cursor() as cur:
            cur.execute('select app.refresh_rtv_analysis()')
            log.info('rtv_analysis snapshot: %s', cur.fetchone()[0])
        conn.commit()
    except Exception as e:                                    # noqa: BLE001
        conn.rollback()
        log.warning('rtv_analysis refresh failed (the 3-hourly beat will catch up): %s', str(e)[:120])
    return before


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    drive, sheets = google()
    support, tot, skipped = read_all(drive, sheets, dt.date.today())
    per = defaultdict(int)
    for r in support:
        per[(r['platform'], r['month'])] += 1
    log.info('support: %d rows -- %s', len(support),
             ', '.join(f'{p} {m:%b-%y} ({n})' for (p, m), n in sorted(per.items())) or 'none')
    log.info('fixed TOT: %s', ', '.join(f'{r["platform"]} {r["category"]} {"-" if r["rtv_pct"] is None else format(r["rtv_pct"], ".0%")}'
                                        for r in sorted(tot, key=lambda x: (x['platform'], x['category']))) or 'none')
    for k in [k for k in skipped if k.startswith('UNREAD ')]:
        log.warning('%s -- rename its columns or tell the Birbal team the new spelling', k)   # a new platform's sheet layout
    if skipped:
        log.info('skipped / folded: %s', '; '.join(f'{k}: {v}' for k, v in skipped.items()))
    if not support and not tot:
        raise SystemExit('nothing read')
    if a.dry_run:
        log.info('dry run: nothing written')
        return
    conn = connect()
    try:
        before = write(conn, support, tot, skipped)
    finally:
        conn.close()
    log.info('replaced %d support rows with %d; %d TOT rows', before, len(support), len(tot))


if __name__ == '__main__':
    sys.exit(main())
