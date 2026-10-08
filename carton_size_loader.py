#!/usr/bin/env python3
"""Shipper carton sizes from the logistics team's "Pending POs" sheet -> wip.ref_carton_size (Birbal migration 163).

    python carton_size_loader.py              # rewrite the table when the sheet changed, then rebuild intercity freight
    python carton_size_loader.py --dry-run    # parse and compare, write nothing

The "Carton & MRP" tab (Item Code | Item Name | Current MRP | CATEGORY | CARTON SIZE) holds the box that goes on
the truck: ERP transfer units divided by it give back the boxes the warehouses log, dispatch by dispatch. Birbal
splits each warehouse-month's intercity freight over SKUs by those cases, so an edited carton size moves the
per-SKU figures; app.refresh_intercity_freight() rebuilds them (under a second) only when something changed.
A size of 0 or blank means "not filled in"; Birbal then falls back to Uniware's carton_size and says so.

Columns are matched on their header names and a read shorter than MIN_ROWS is treated as broken, not as a
deletion. Replaces cogs_pipeline/load_carton_sizes.py (the manual first load, 2026-10-08).

Environment: as targets_loader.py (SUPABASE_DB_URL, GOOGLE_TOKEN_JSON = the instamart@ token, which can read
the sheet).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from googleapiclient.discovery import build
from psycopg2.extras import execute_values

from targets_loader import connect, creds, num, values

log = logging.getLogger('carton_size')

SHEET_ID = os.environ.get('CARTON_SHEET_ID', '1i1VnYly-QEExfqwYldnxFh8ZRSkJqm4rpfsw1V8-pAU')
TAB = 'Carton & MRP'
COLS = {'item code': 'item_no', 'item name': 'item_name', 'current mrp': 'mrp', 'category': 'category',
        'carton size': 'carton_size'}
MIN_ROWS = 50                                # the tab had 105 items on 2026-10-08


def read_sheet(svc):
    grid = values(svc, SHEET_ID, f"'{TAB}'!A1:Z2000")
    if not grid:
        raise ValueError(f'{TAB} tab is empty')
    hdr = [str(h).strip().lower() for h in grid[0]]
    idx = {COLS[h]: i for i, h in enumerate(hdr) if h in COLS}
    missing = sorted(set(COLS.values()) - set(idx))
    if missing:
        raise ValueError(f'{TAB}: headers changed, cannot find {missing} (saw {hdr})')
    rows = {}
    for r in grid[1:]:
        r = list(r) + [''] * (len(hdr) - len(r))
        code = str(r[idx['item_no']]).strip().upper()
        if not code:
            continue
        name, cat = str(r[idx['item_name']]).strip(), str(r[idx['category']]).strip()
        rows[code] = (code, name or None, num(r[idx['mrp']]), cat or None, num(r[idx['carton_size']]))
    if len(rows) < MIN_ROWS:
        raise ValueError(f'{TAB}: only {len(rows)} items read (expected at least {MIN_ROWS}); refusing to replace the table')
    return rows


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    svc = build('sheets', 'v4', credentials=creds(os.environ.get('GOOGLE_TOKEN_JSON', 'token.json')), cache_discovery=False)
    sheet = read_sheet(svc)
    log.info('%d items in the sheet, %d with a carton size', len(sheet), sum(1 for r in sheet.values() if (r[4] or 0) > 0))

    conn = connect()
    with conn.cursor() as cur:
        cur.execute('select item_no, item_name, mrp, category, carton_size from wip.ref_carton_size')
        stored = {r[0]: (r[0], r[1], None if r[2] is None else float(r[2]), r[3], None if r[4] is None else float(r[4]))
                  for r in cur.fetchall()}
    added = sorted(set(sheet) - set(stored))
    removed = sorted(set(stored) - set(sheet))
    resized = sorted(k for k in set(sheet) & set(stored) if sheet[k][4] != stored[k][4])
    other = sorted(k for k in set(sheet) & set(stored) if sheet[k] != stored[k] and k not in resized)
    if not (added or removed or resized or other):
        log.info('no change since the last load')
        return 0
    for k in resized:
        log.info('carton size %s: %s -> %s', k, stored[k][4], sheet[k][4])
    log.info('added %s | removed %s | other edits %d', added or '-', removed or '-', len(other))
    if a.dry_run:
        log.info('dry run: nothing written')
        return 0
    with conn.cursor() as cur:
        cur.execute("set statement_timeout = '600000'")
        cur.execute('delete from wip.ref_carton_size where true')
        execute_values(cur, 'insert into wip.ref_carton_size (item_no, item_name, mrp, category, carton_size) values %s',
                       list(sheet.values()))
        cur.execute('select app.refresh_intercity_freight()')
        log.info('loaded %d items; intercity freight rebuilt: %s', len(sheet), cur.fetchone()[0])
    conn.commit()
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
