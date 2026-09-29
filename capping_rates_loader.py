#!/usr/bin/env python3
"""Returns-capping RTV% rates from the monthly Target Planning sheets -> public.rtv_capping_pct.

    python capping_rates_loader.py              # load every month whose rates changed
    python capping_rates_loader.py --dry-run    # parse and compare, write nothing

Instamart, Blinkit, Zepto and Flipkart cap the returns they credit at an agreed % per product per
month; Birbal accrues the uncredited remainder as the capping provision (migrations 036/044). The
rates live in the `MRO%` tab of the marketing team's "Target Planning <Month> - <Year>" sheet
(Platform | SKU | Category | Margin | Offinvoice | RTV%), the same sheets targets_loader.py reads.
Until 2026-09-29 they reached Supabase as "<Month> RTV%.xlsx" exports of that tab, loaded by hand
(D:\\Python\\Birbal\\capping_rtv_pipeline\\load_rtv_pct.py). Replacing Apr-Sep 2026 with the sheets
left the provision unchanged to the rupee, so the sheet is now the source.

Each month is compared with what is stored and rewritten only when a rate, SKU or platform changed;
the changes are logged (and kept in workflow_logs) because an edit to an old month moves that
month's provision. public.o2c_refresh() (pg_cron, 09:00 and 14:00 IST) rebuilds
mv_rtv_capping_ledger from here, so this does not refresh it.

Environment: as targets_loader.py (SUPABASE_DB_URL, GOOGLE_TOKEN_JSON = the instamart@ token).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from psycopg2.extras import execute_values

from targets_loader import connect, creds, find_target_planning_sheets, num, values

log = logging.getLogger('capping_rates')

# the MRO% tab's platform spelling -> ref_customer_invoicing.platform (as load_rtv_pct.py)
PLATFORM = {
    'BLINKIT': 'Blinkit', 'ZEPTO': 'Zepto',
    'SCOOTSY': 'Instamart',                  # Scootsy Logistics = Swiggy Instamart
    'FLIPKART MINUTES': 'Flipkart Quick', 'FLIPKART SUPERMART': 'Flipkart',
    'BIG BASKET': 'Big Basket', 'AMAZON FRESH': 'Amazon', 'ARIPL': 'Amazon',
    'MILK BASKET': 'Reliance',               # Milkbasket invoices under Reliance Retail
    'FIRST CLUB': 'First Club', 'D MART': 'DMart',
}
MIN_ROWS = 200                               # a month read shorter than this is a broken read, not a rate card


def norm(s) -> str:
    return re.sub(r'\s+', ' ', str(s or '')).strip().upper()


def parse_mro(svc, sheet_id):
    """{(platform_raw, sku, category): rtv_pct} from the MRO% tab (header on row 2)."""
    grid = values(svc, sheet_id, "'MRO%'!A2:P5000")
    if not grid:
        raise ValueError('MRO% tab is empty')
    hdr = [norm(h) for h in grid[0]]
    def col(pred, what):
        i = next((i for i, h in enumerate(hdr) if pred(h)), None)
        if i is None:
            raise ValueError(f'MRO% tab has no {what} column (headers: {hdr[:10]})')
        return i
    c_plat = col(lambda h: h == 'PLATFORM', 'Platform')
    c_sku = col(lambda h: h.startswith('SKU'), 'SKU')
    c_cat = col(lambda h: h.startswith('CATEG'), 'Category')
    c_rtv = col(lambda h: h.startswith('RTV'), 'RTV%')
    rates, clashes = {}, []
    for r in grid[1:]:
        r = list(r) + [''] * (len(hdr) - len(r))
        plat, sku = norm(r[c_plat]), norm(r[c_sku])
        if not plat or not sku:
            continue
        v = r[c_rtv]
        pct = num(v) if v not in (None, '') else 0.0
        if pct is None:
            raise ValueError(f'non-numeric RTV% {v!r} for {plat} / {sku}')
        k = (plat, sku, norm(r[c_cat]))
        if k in rates and rates[k] != pct:
            # Big Basket lists PITA BREAD / PIZZA BASE twice at 0.30 and 0.42: the larger ceiling is the prudent accrual
            clashes.append(f'{plat} / {sku}: {rates[k]} vs {pct}')
        rates[k] = max(pct, rates.get(k, pct))
    return rates, clashes


def stored(cur, month):
    cur.execute('select platform_raw, sku, category, rtv_pct from public.rtv_capping_pct where month = %s', (month,))
    return {(p, s, c): float(v) for p, s, c, v in cur.fetchall()}


def diff(old, new):
    changed = [f'{k[0]} / {k[1]}: {old[k]:g} -> {new[k]:g}' for k in new if k in old and abs(old[k] - new[k]) > 1e-9]
    added = [k for k in new if k not in old]
    dropped = [k for k in old if k not in new]
    return changed, added, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--token', default=os.environ.get('GOOGLE_TOKEN_JSON', 'token.json'))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    c = creds(a.token)
    svc = build('sheets', 'v4', credentials=c, cache_discovery=False)
    conn = connect()
    cur = conn.cursor()
    failed, written, summary = False, 0, {}
    for month, sid, name in find_target_planning_sheets(c):
        try:
            rates, clashes = parse_mro(svc, sid)
            unmapped = sorted({k[0] for k in rates if k[0] not in PLATFORM})
            if unmapped:
                raise ValueError(f'platforms with no mapping {unmapped}: add them to PLATFORM')
            if len(rates) < MIN_ROWS:
                raise ValueError(f'only {len(rates)} rows read; refusing to replace the month')
        except (HttpError, ValueError) as e:
            status = getattr(getattr(e, 'resp', None), 'status', None)
            log.error('%s (%s): %s', name, sid, f'HTTP {status}' if status else e)
            failed = True
            continue
        for cl in clashes:
            log.warning('%s: listed twice with two rates, kept the higher: %s', name, cl)
        old = stored(cur, month)
        changed, added, dropped = diff(old, rates)
        if not (changed or added or dropped):
            log.info('%s: %d rates, unchanged', name, len(rates))
            continue
        log.info('%s: %d rates -- %d changed, %d added, %d dropped%s', name, len(rates), len(changed), len(added),
                 len(dropped), '' if old else ' (first load of this month)')
        if old:
            for line in changed[:25]:
                log.info('   rate %s', line)
            for k in dropped[:10]:
                log.info('   dropped %s / %s / %s', *k)
        summary[month.isoformat()] = {'sheet': name, 'rates': len(rates), 'changed': len(changed),
                                      'added': len(added), 'dropped': len(dropped), 'rate_changes': changed[:50]}
        if a.dry_run:
            continue
        cur.execute('delete from public.rtv_capping_pct where month = %s', (month,))
        execute_values(cur, 'insert into public.rtv_capping_pct (month, platform_raw, platform, sku, category, rtv_pct, source_file) values %s',
                       [(month, p, PLATFORM[p], s, cat, pct, f'{name} (MRO%)') for (p, s, cat), pct in rates.items()])
        conn.commit()
        written += len(rates)
    if a.dry_run:
        conn.rollback()
    elif summary:
        try:
            cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) "
                        "values ('sheet_grn_loader:capping_rates', 'capping_rates', now(), now(), 'success', %s::jsonb, %s, %s, now())",
                        (json.dumps(summary), written, written))
            conn.commit()
        except Exception as e:                            # noqa: BLE001
            conn.rollback()
            log.warning('workflow_logs not written: %s', str(e)[:100])
    conn.close()
    log.info('%s %d months', 'would rewrite' if a.dry_run else 'rewrote', len(summary))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
