#!/usr/bin/env python3
"""Sales targets (Marketplace and Trade) from the business's own Google Sheets -> warehouse.sales_targets.

    python targets_loader.py                     # both sources
    python targets_loader.py --source marketplace
    python targets_loader.py --source trade --trade-token "D:/.../token.json"
    python targets_loader.py --dry-run           # parse and print, write nothing

Two sources, two owners, two grains (2026-09-23, "Changes in Birbal_2" item 16):

* MARKETPLACE -- the marketing team's monthly "Target Planning <Month> - <Year>" sheets (one per
  month, found in Drive by name). Tab `Target`, header row 3, one row per customer location x SKU
  with `Platform`, `CATEGORY`, `SKU Name`, `QTY TGT  - <Month> <Year>` and `T - Net Sales` (the
  month's net-sales target for that line, ex-GST after returns). Loaded at two grains:
    source='target'      month x party x category           (units + value)  <- the boards read this
    source='target_sku'  month x party x category x SKU     (units + value)  <- for Ask / SQL
* DETAIL -- the same Target tab line by line (customer location x SKU) with the whole target ladder
  (qty, MRP billing ... net sales, COGS, GM, CM1, ads, CM2, CM3) -> warehouse.sales_targets_detail, then
  app.refresh_target_vs_actual() rebuilds warehouse.target_vs_actual (Birbal migration 107).
* TRADE -- finance's "Management MIS FY27" sheet, tab `YTD Channelwise`: P&L lines x channel in
  AOP / Target / Actual blocks per month. Only `Net Sales` is taken, from the `Target` block as
  source='target' (channel_group Trade; also Marketplace as source='mis', finance's own number for
  the channel) and from the `AOP Target` block as source='aop'. Trade sub-channels (MT Premium, MT
  Mass, Distributor, GT) are kept as source='mis_sub' with the sub-channel in `party`.

The boards read source='target' only and never mix levels: Marketplace rows are party x category,
Trade rows are channel-level. Each run replaces the months it read for that source.

Environment: SUPABASE_DB_URL (+ SUPABASE_DB_SSLMODE / SUPABASE_DB_SSLROOTCERT), GOOGLE_TOKEN_JSON
(the instamart@ token: it can open the Target Planning sheets), GOOGLE_TOKEN_JSON_TRADE (a token
that can open the MIS sheet -- marketing@'s; falls back to GOOGLE_TOKEN_JSON, and if that gets a
403 the run says whom to share the sheet with).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys

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

log = logging.getLogger('targets')

MIS_SHEET_ID = os.environ.get('TARGETS_MIS_SHEET_ID', '1qT4atLhT0YRRtT6S-WsMmHspOiVnn22DBPkEoYUUwL4')
MIS_TAB = 'YTD Channelwise'
TARGET_PLANNING_NAME = re.compile(r'^Target Planning\s+([A-Za-z]+)\s*-\s*(\d{4})$', re.I)
FY_START = dt.date(2026, 4, 1)          # FY27: the boards' invoices are complete only from here

# the sheet's platform spellings -> the boards' party names (channels.js PLATFORM_PARTY)
PARTY = {
    'AMAZON FRESH': 'Amazon Fresh', 'AMAZON': 'Amazon Fresh',
    'BIG BASKET': 'Big Basket', 'BIGBASKET': 'Big Basket',
    'BLINKIT': 'Blinkit', 'ZEPTO': 'Zepto', 'FIRST CLUB': 'First Club',
    'ARIPL': 'DMart', 'D MART': 'DMart', 'DMART': 'DMart', 'DMART READY': 'DMart',
    'MILK BASKET': 'Milk Basket', 'MILKBASKET': 'Milk Basket',
    'SCOOTSY': 'Instamart', 'INSTAMART': 'Instamart', 'SWIGGY INSTAMART': 'Instamart',
    'FLIPKART MINUTES': 'Flipkart Quick', 'FLIPKART QUICK': 'Flipkart Quick', 'FLIPKART': 'Flipkart Quick',
    'FLIPKART SUPERMART': 'Flipkart Supermart', 'RELIANCE': 'Reliance Smart', 'NATURES BASKET': 'NB', "NATURE'S BASKET": 'NB',
}
# The Target tab's "T - <line>" columns: the month's target P&L per location x SKU, the MIS's own ladder.
LADDER = {
    'T-MRPBILLING': 'mrp_billing', 'T-PARTNERMARGIN': 'partner_margin', 'T-GROSSBILLING': 'gross_billing',
    'T-RTVAMT': 'rtv', 'T-CONSUMERSALES': 'consumer_sales', 'T-TAXAMOUNT': 'tax', 'T-NETSALES': 'net_sales',
    'T-COGS': 'cogs', 'T-YIELDLOSS': 'yield_loss', 'T-GM': 'gm', 'T-LOGISTICS': 'logistics', 'T-CM1': 'cm1',
    'T-ADSSPENDS': 'ads', 'T-DISCOUNTS': 'discounts', 'T-OFFINVOICE': 'off_invoice', 'T-CM2': 'cm2',
    'T-BRANDBUILDING': 'brand_building', 'T-CM3': 'cm3',
}
DETAIL_COLS = ['month', 'channel_group', 'party', 'platform_raw', 'location', 'city_raw', 'sku_name', 'parent_sku',
               'category', 'item_no', 'qty'] + list(dict.fromkeys(LADDER.values())) + ['source']
# parties the business counts as Trade even where the marketing plan lists them (Birbal migration 108)
TRADE_PARTIES = {'DMart', 'Jio BP', 'GT'}
MONTHS = {m.lower(): i for i, m in enumerate(['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'], 1)}

DDL = """
create schema if not exists warehouse;
create table if not exists warehouse.sales_targets (
  month         date        not null,
  channel_group text        not null,
  party         text,
  category      text,
  item_no       text,
  sku           text,
  measure       text        not null,
  target        numeric     not null,
  source        text        not null default 'target',
  loaded_at     timestamptz not null default now()
);
create unique index if not exists sales_targets_key_uidx on warehouse.sales_targets
  (month, channel_group, coalesce(party,''), coalesce(category,''), coalesce(item_no,''), coalesce(sku,''), measure, source);
comment on table warehouse.sales_targets is 'Sales targets by month. source=target: Marketplace rows are month x party x category (units + value = net sales ex-GST after returns) from the "Target Planning <Month> - <Year>" sheets; Trade rows are channel-level (value only) from the Management MIS FY27 sheet, tab YTD Channelwise. Other sources (target_sku, mis, mis_sub, aop) are reference grains the boards do not read. Never add rows of different levels together. Loaded by sheet_grn_loader/targets_loader.py.';
"""


def title(s: str) -> str:
    s = str(s or '').strip()
    return s.title() if s and s == s.upper() else s


def party_of(platform: str) -> str:
    key = re.sub(r'\s+', ' ', str(platform or '').strip().upper())
    return PARTY.get(key, title(key))


def num(v):
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(',', '').strip())
    except ValueError:
        return None


def serial_to_date(v):
    if isinstance(v, (int, float)) and 20000 < v < 80000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(v))
    if isinstance(v, str):
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%b-%y', '%b %Y', '%B %Y'):
            try:
                return dt.datetime.strptime(v.strip(), fmt).date()
            except ValueError:
                pass
    return None


# ------------------------------------------------------------------ google
def creds(path):
    c = Credentials.from_authorized_user_file(path)
    if not c.valid:
        c.refresh(Request())
    return c


def account_of(c):
    try:
        return build('drive', 'v3', credentials=c, cache_discovery=False).about().get(fields='user(emailAddress)').execute()['user']['emailAddress']
    except Exception:                                    # noqa: BLE001
        return 'the loader account'


def values(svc, sheet_id, rng):
    return svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng, valueRenderOption='UNFORMATTED_VALUE').execute().get('values', [])


# ------------------------------------------------------------------ marketplace
def find_target_planning_sheets(c):
    drive = build('drive', 'v3', credentials=c, cache_discovery=False)
    q = "name contains 'Target Planning' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
    found, page = [], None
    while True:
        r = drive.files().list(q=q, fields='nextPageToken, files(id, name, modifiedTime)', pageSize=200, pageToken=page,
                               includeItemsFromAllDrives=True, supportsAllDrives=True, corpora='allDrives').execute()
        found += r.get('files', [])
        page = r.get('nextPageToken')
        if not page:
            break
    out = []
    for f in found:
        m = TARGET_PLANNING_NAME.match(f['name'].strip())
        if not m or m.group(1).lower() not in MONTHS:
            continue
        month = dt.date(int(m.group(2)), MONTHS[m.group(1).lower()], 1)
        if month >= FY_START:
            out.append((month, f['id'], f['name']))
    return sorted(out)


def parse_marketplace(svc, sheet_id, month, detail=None):
    """Rows of (party, category, sku, units, value) from the Target tab; `detail` (a list) also collects
    one dict per location x SKU line with the whole target ladder."""
    head = values(svc, sheet_id, "'Target'!3:3")
    headers = [str(h).strip() for h in (head[0] if head else [])]
    def col(pred, what):
        for i, h in enumerate(headers):
            if pred(h):
                return i
        raise SystemExit(f'{sheet_id}: Target tab has no {what} column (headers: {headers[:12]}...)')
    c_platform = col(lambda h: h.upper() == 'PLATFORM', 'Platform')
    c_cat = col(lambda h: h.upper() == 'CATEGORY', 'CATEGORY')
    c_sku = col(lambda h: h.upper() == 'SKU NAME', 'SKU Name')
    c_qty = col(lambda h: re.match(r'^QTY\s*TGT\s*-', h, re.I), 'QTY TGT')
    c_val = col(lambda h: re.sub(r'\s+', ' ', h).upper() == 'T - NET SALES', 'T - Net Sales')
    c_loc = col(lambda h: h.upper() == 'CUSTOMER LOCATION', 'Customer Location')
    c_city = col(lambda h: h.upper() == 'CITY', 'City')
    c_parent = next((i for i, h in enumerate(headers) if h.upper() == 'PARENT SKU NAME'), None)
    ladder = {}
    for i, h in enumerate(headers):
        k = re.sub(r'\s+', '', h).upper()
        if k in LADDER and LADDER[k] not in ladder.values():
            ladder[i] = LADDER[k]
    m = re.search(r'-\s*([A-Za-z]+)\s+(\d{4})', headers[c_qty])
    if m and m.group(1).lower() in MONTHS:
        hdr_month = dt.date(int(m.group(2)), MONTHS[m.group(1).lower()], 1)
        if hdr_month != month:
            log.warning('%s: QTY TGT header says %s, sheet name says %s; using the header', sheet_id, hdr_month, month)
            month = hdr_month
    rows, start, page = [], 4, 2000
    while True:
        vals = values(svc, sheet_id, f"'Target'!{start}:{start + page - 1}")
        if not vals:
            break
        for r in vals:
            r = list(r) + [''] * (len(headers) - len(r))
            platform, cat, sku = str(r[c_platform]).strip(), str(r[c_cat]).strip(), str(r[c_sku]).strip()
            if not platform or not sku:
                continue
            q, v = num(r[c_qty]), num(r[c_val])
            if not q and not v:
                continue
            rows.append((party_of(platform), title(cat) or '(none)', sku.strip().upper(), q or 0.0, v or 0.0))
            if detail is not None:
                d = {'party': party_of(platform), 'platform_raw': platform, 'location': str(r[c_loc]).strip(),
                     'city_raw': str(r[c_city]).strip(), 'sku_name': sku.strip().upper(),
                     'parent_sku': str(r[c_parent]).strip().upper() if c_parent is not None else None,
                     'category': title(cat) or '(none)', 'qty': q or 0.0}
                for i, name in ladder.items():
                    d[name] = num(r[i]) or 0.0
                detail.append(d)
        if len(vals) < page:
            break
        start += page
    if detail is not None:
        for d in detail:
            d.setdefault('month', month)
    return month, rows


def marketplace_rows(c, detail_out=None):
    svc = build('sheets', 'v4', credentials=c, cache_discovery=False)
    sheets = find_target_planning_sheets(c)
    if not sheets:
        log.warning('no "Target Planning <Month> - <Year>" sheet visible to %s', account_of(c))
    out, months = [], set()
    for month, sid, name in sheets:
        try:
            det = [] if detail_out is not None else None
            month, lines = parse_marketplace(svc, sid, month, det)
            if det is not None:
                for d in det:
                    d['month'] = month
                detail_out.extend(det)
        except HttpError as e:
            log.error('%s (%s): HTTP %s -- share it with %s', name, sid, e.resp.status, account_of(c))
            continue
        agg, agg_sku = {}, {}
        for party, cat, sku, q, v in lines:
            a = agg.setdefault((party, cat), [0.0, 0.0]); a[0] += q; a[1] += v
            b = agg_sku.setdefault((party, cat, sku), [0.0, 0.0]); b[0] += q; b[1] += v
        for (party, cat), (q, v) in agg.items():
            grp = 'Trade' if party in TRADE_PARTIES else 'Marketplace'
            out.append((month, grp, party, cat, None, None, 'units', round(q, 3), 'target'))
            out.append((month, grp, party, cat, None, None, 'value', round(v, 2), 'target'))
        for (party, cat, sku), (q, v) in agg_sku.items():
            grp = 'Trade' if party in TRADE_PARTIES else 'Marketplace'
            out.append((month, grp, party, cat, None, sku, 'units', round(q, 3), 'target_sku'))
            out.append((month, grp, party, cat, None, sku, 'value', round(v, 2), 'target_sku'))
        months.add(month)
        tot_v = sum(v for (_, _), (_, v) in agg.items())
        log.info('%s: %d lines -> %d party x category rows, net sales target Rs %.2f cr', name, len(lines), len(agg), tot_v / 1e7)
    return out, months


# ------------------------------------------------------------------ trade (finance MIS)
def trade_rows(c):
    svc = build('sheets', 'v4', credentials=c, cache_discovery=False)
    try:
        grid = values(svc, MIS_SHEET_ID, f"'{MIS_TAB}'!A1:ZZ120")
    except HttpError as e:
        log.error('Management MIS FY27 sheet: HTTP %s -- share https://docs.google.com/spreadsheets/d/%s with %s (Viewer)', e.resp.status, MIS_SHEET_ID, account_of(c))
        return [], set()
    width = max(len(r) for r in grid)
    grid = [list(r) + [''] * (width - len(r)) for r in grid]
    row2, row3, row4 = grid[1], grid[2], grid[3]
    labels = {re.sub(r'\s+', ' ', str(r[0]).strip().lower()): i for i, r in enumerate(grid) if r and str(r[0]).strip()}
    r_net = labels.get('net sales')
    if r_net is None:
        raise SystemExit('YTD Channelwise: no "Net Sales" row')
    out, months, block, month = [], set(), None, None
    for ci in range(1, width):
        if str(row2[ci]).strip():
            block = str(row2[ci]).strip().lower()
            month = None
        if row3[ci] != '' and row3[ci] is not None:
            month = serial_to_date(row3[ci])          # YTD blocks carry text here -> None
        ch = re.sub(r'\s+', ' ', str(row4[ci]).strip())
        if not ch or not block or not month or ch.lower() == 'total':
            continue
        v = num(grid[r_net][ci])
        if v is None:
            continue
        month = month.replace(day=1)
        chl = ch.lower()
        if block == 'target':
            if chl in ('marketplace', 'marketplaces'):
                out.append((month, 'Marketplace', None, None, None, None, 'value', round(v, 2), 'mis'))
            elif chl == 'trade':
                out.append((month, 'Trade', None, None, None, None, 'value', round(v, 2), 'target'))
            elif chl != 'ac':
                out.append((month, 'Trade', ch, None, None, None, 'value', round(v, 2), 'mis_sub'))
        elif block == 'aop target':
            if chl in ('marketplace', 'marketplaces'):
                out.append((month, 'Marketplace', None, None, None, None, 'value', round(v, 2), 'aop'))
            elif chl == 'trade':
                out.append((month, 'Trade', None, None, None, None, 'value', round(v, 2), 'aop'))
        else:
            continue
        months.add(month)
    # the tab repeats some blocks (the same AOP column twice): keep one row per key. A zero is a month
    # finance has not set yet (Trade reads 0 from Sep-26 on), not a target of nothing, so it is left out.
    seen, dedup = set(), []
    for r in out:
        key = (r[0], r[1], r[2], r[6], r[8])
        if key in seen or not r[7]:
            continue
        seen.add(key)
        dedup.append(r)
    out = dedup
    months = {r[0] for r in out}
    log.info('MIS FY27: %d rows over %d months (%s)', len(out), len(months), ', '.join(m.strftime('%b-%y') for m in sorted(months)))
    return out, months


# ------------------------------------------------------------------ database
def connect():
    dsn = os.environ['SUPABASE_DB_URL']
    kw = dict(sslmode=os.environ.get('SUPABASE_DB_SSLMODE', 'require'))
    if os.environ.get('SUPABASE_DB_SSLROOTCERT'):
        kw['sslrootcert'] = os.environ['SUPABASE_DB_SSLROOTCERT']
    return psycopg2.connect(dsn, **kw)


def write(conn, rows, months, sources, label):
    if not rows:
        log.warning('%s: nothing to write', label)
        return 0
    with conn.cursor() as cur:
        cur.execute(DDL)
        # replace exactly the (source, channel, month) slices this run read -- the Marketplace and Trade
        # runs both write source='target', for different channels, and must not wipe each other
        keys = sorted({(r[8], r[1], r[0]) for r in rows})
        for src, chan, month in keys:
            cur.execute('delete from warehouse.sales_targets where source = %s and channel_group = %s and month = %s', (src, chan, month))
        execute_values(cur, 'insert into warehouse.sales_targets (month, channel_group, party, category, item_no, sku, measure, target, source) values %s', rows, page_size=1000)
        # the boards read as per-scope roles (birbal_scope_<hash>); the Birbal migration grants too, this is the belt
        cur.execute("select rolname from pg_roles where rolname like 'birbal_scope_%' or rolname = 'birbal_engine'")
        for (role,) in cur.fetchall():
            cur.execute(f'grant select on warehouse.sales_targets to "{role}"')
        try:
            cur.execute("select 1 from information_schema.tables where table_schema='public' and table_name='workflow_logs'")
            if cur.fetchone():
                cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) values (%s, %s, now(), now(), 'success', %s::jsonb, %s, %s, now())",
                            (f'sheet_grn_loader:targets_{label}', f'targets:{label}', json.dumps({'months': [m.isoformat() for m in sorted(months)]}), len(rows), len(rows)))
        except Exception as e:                            # noqa: BLE001
            log.warning('workflow_logs not written: %s', str(e)[:100])
    conn.commit()
    log.info('%s: wrote %d rows for %s', label, len(rows), ', '.join(m.strftime('%b-%y') for m in sorted(months)))
    return len(rows)


DETAIL_DDL = """
create table if not exists warehouse.sales_targets_detail (
  month date not null, channel_group text not null, party text, platform_raw text, location text, city_raw text,
  sku_name text, parent_sku text, category text, item_no text, qty numeric,
""" + ",\n".join(f"  {c} numeric" for c in dict.fromkeys(LADDER.values())) + """,
  source text not null default 'target', loaded_at timestamptz not null default now()
);
create index if not exists sales_targets_detail_month_idx on warehouse.sales_targets_detail (month, party);
"""


def write_detail(conn, detail):
    if not detail:
        return 0
    months = sorted({d['month'] for d in detail})
    cols = DETAIL_COLS
    rows = [tuple(d.get(c) if c not in ('channel_group', 'source', 'item_no') else
                  {'channel_group': 'Trade' if d.get('party') in TRADE_PARTIES else 'Marketplace', 'source': 'target', 'item_no': None}[c]
                  for c in cols) for d in detail]
    with conn.cursor() as cur:
        cur.execute(DETAIL_DDL)
        cur.execute('delete from warehouse.sales_targets_detail where source = %s and month = any(%s)', ('target', months))
        execute_values(cur, f'insert into warehouse.sales_targets_detail ({", ".join(cols)}) values %s', rows, page_size=1000)
        # the sheet names SKUs the way the MIS's register names products: map them the same two ways
        cur.execute("""
            update warehouse.sales_targets_detail t
               set item_no = coalesce(d.item_no, n.fg_no, n.tg_no)
              from (select distinct sku_name from warehouse.sales_targets_detail where item_no is null) s
              left join lateral (select item_no from warehouse.sales_item_dim
                                  where regexp_replace(upper(product_name), '[^A-Z0-9]', '', 'g') = regexp_replace(s.sku_name, '[^A-Z0-9]', '', 'g')
                                  order by item_no limit 1) d on true
              left join public.v_cogs_item_by_name n on n.k = regexp_replace(s.sku_name, '[^A-Z0-9]', '', 'g')
             where t.sku_name = s.sku_name and t.item_no is null""")
        cur.execute("select rolname from pg_roles where rolname like 'birbal_scope_%' or rolname = 'birbal_engine'")
        for (role,) in cur.fetchall():
            cur.execute(f'grant select on warehouse.sales_targets_detail to "{role}"')
    conn.commit()
    log.info('detail: wrote %d location x SKU lines for %s', len(rows), ', '.join(m.strftime('%b-%y') for m in months))
    # the target-vs-actual snapshot the boards read (Birbal migration 107) follows every load
    try:
        with conn.cursor() as cur:
            cur.execute("select to_regprocedure('app.refresh_target_vs_actual()') is not null")
            if cur.fetchone()[0]:
                cur.execute('select app.refresh_target_vs_actual()')
                log.info('target_vs_actual rebuilt: %s', cur.fetchone()[0])
        conn.commit()
    except Exception as e:                                # noqa: BLE001 -- the 3-hourly pg_cron beat will catch up
        conn.rollback()
        log.warning('target_vs_actual not rebuilt now (%s); pg_cron rebuilds it within 3 hours', str(e)[:120])
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=['marketplace', 'trade', 'all'], default='all')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--trade-token', default=os.environ.get('GOOGLE_TOKEN_JSON_TRADE') or os.environ.get('GOOGLE_TOKEN_JSON', 'token.json'))
    ap.add_argument('--token', default=os.environ.get('GOOGLE_TOKEN_JSON', 'token.json'))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    conn = None if a.dry_run else connect()
    failed = False
    if a.source in ('marketplace', 'all'):
        detail = []
        rows, months = marketplace_rows(creds(a.token), detail)
        if a.dry_run:
            print(f'marketplace: {len(rows)} rows; sample: {rows[:4]}')
            print(f'detail: {len(detail)} location x SKU lines; sample: {detail[:1]}')
        elif rows:
            write(conn, rows, months, ('target', 'target_sku'), 'marketplace')
            write_detail(conn, detail)
        else:
            failed = True
    if a.source in ('trade', 'all'):
        rows, months = trade_rows(creds(a.trade_token))
        if a.dry_run:
            print(f'trade: {len(rows)} rows; sample: {[r for r in rows if r[8] == "target"][:4]}')
        elif rows:
            write(conn, rows, months, ('target', 'mis', 'mis_sub', 'aop'), 'trade')
        else:
            failed = True
    if conn:
        conn.close()
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
