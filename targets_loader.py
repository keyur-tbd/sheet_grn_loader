#!/usr/bin/env python3
"""Sales targets from the business's planning sheets -> warehouse.sales_targets.

    python targets_loader.py                     # both
    python targets_loader.py --source marketplace
    python targets_loader.py --source trade
    python targets_loader.py --dry-run           # parse and print, write nothing

"Changes in Birbal_2" item 16 (2026-09-23). Trade targets are NOT taken from finance's Management MIS
FY27 sheet (owner's call, 2026-09-28) but from the Trade team's own planning sheets (TRADE below).

* MARKETPLACE -- the marketing team's monthly "Target Planning <Month> - <Year>" sheets (one per
  month, found in Drive by name). Tab `Target`, header row 3, one row per customer location x SKU
  with `Platform`, `CATEGORY`, `SKU Name`, `QTY TGT  - <Month> <Year>` and `T - Net Sales` (the
  month's net-sales target for that line, ex-GST after returns). Loaded at two grains:
    source='target'      month x party x category           (units + value)  <- the boards read this
    source='target_sku'  month x party x category x SKU     (units + value)  <- for Ask / SQL
* DETAIL -- the same Target tab line by line (customer location x SKU) with the whole target ladder
  (qty, MRP billing ... net sales, COGS, GM, CM1, ads, CM2, CM3) -> warehouse.sales_targets_detail, then
  app.refresh_target_vs_actual() rebuilds warehouse.target_vs_actual (Birbal migration 107).
* TRADE -- the Trade team's monthly "Trade Target Planning <Mon>'<yy>" sheets (from Jun'26; shared with
  instamart@ 2026-09-25). Tab `<Mon> TGT`, header row 2, one row per store x item with PARTY, NAME (the
  store = the invoice ship-to name), CITY, CATEGORY, ITEM NAME, `<Mon> Net Sales Qty` and the ladder
  (MRP SUPPLY TGT ... Net Sales TGT, COGS TGT, Gross Margin TGT). Loaded like the Marketplace lines:
  store x SKU into warehouse.sales_targets_detail (plan='trade'; party = the ship-to's party_master),
  then folded to party x category (source='target') and SKU (source='target_sku') in sales_targets.
  Each month's lines are checked against the `Target P&L` tab's Net Sales total.
  In a month that has a Trade sheet, the Marketplace sheets' DMart / Jio BP / GT lines are dropped: the
  Trade sheet is the Trade target.

The boards read source='target' only and never mix levels. Each run replaces the months it read.

Environment: SUPABASE_DB_URL (+ SUPABASE_DB_SSLMODE / SUPABASE_DB_SSLROOTCERT), GOOGLE_TOKEN_JSON
(the instamart@ token: it can open both sets of planning sheets).
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

TARGET_PLANNING_NAME = re.compile(r'^Target Planning\s+([A-Za-z]+)\s*-\s*(\d{4})$', re.I)
TRADE_PLANNING_NAME = re.compile(r"^Trade Target Planning\s+([A-Za-z]+)\s*['’]?\s*(\d{2}|\d{4})$", re.I)
FY_START = dt.date(2026, 4, 1)          # FY27: the boards' invoices are complete only from here

# the sheet's platform spellings -> the boards' party names (channels.js PLATFORM_PARTY)
PARTY = {
    'AMAZON FRESH': 'Amazon Fresh', 'AMAZON': 'Amazon Fresh',
    'BIG BASKET': 'Big Basket', 'BIGBASKET': 'Big Basket',
    'BLINKIT': 'Blinkit', 'ZEPTO': 'Zepto', 'FIRST CLUB': 'First Club',
    'ARIPL': 'ARIPL', 'D MART': 'DMart', 'DMART': 'DMart', 'DMART READY': 'DMart Ready',
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
               'category', 'item_no', 'qty'] + list(dict.fromkeys(LADDER.values())) + ['source', 'plan']
# parties the business counts as Trade even where the marketing plan lists them (Birbal migration 108)
TRADE_PARTIES = {'DMart', 'Jio BP', 'GT'}
# the Trade sheets' spellings party_of() would title-case wrongly
TRADE_PARTY = {'JIO BP': 'Jio BP', 'GT': 'GT', 'NB': 'NB', 'METRO C&C': 'Metro C&C', 'M.K. RETAIL': 'M.K. Retail'}
# the `<Mon> TGT` tab's ladder columns -> sales_targets_detail columns (first occurrence of each header)
TRADE_LADDER = {
    'MRP SUPPLY TGT': 'mrp_billing', 'COMMSISON TGT': 'partner_margin', 'COMMISSION TGT': 'partner_margin',
    'GROSS BILLING TGT': 'gross_billing', 'RTV VALUE TGT': 'rtv', 'CONSUMER SALES TGT': 'consumer_sales',
    'GST VALUE TGT': 'tax', 'NET SALES TGT': 'net_sales', 'COGS TGT': 'cogs', 'GROSS MARGIN TGT': 'gm',
    'OFFER TGT': 'discounts', 'OFF-INVOICE TGT': 'off_invoice',
}
# Trade sheet item names the register spells differently
SKU_ALIAS = {'RAGI FULL LOAF': 'RAGI LOAF'}
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
comment on table warehouse.sales_targets is 'Sales targets by month. source=target: Marketplace rows are month x party x category (units + value = net sales ex-GST after returns) from the "Target Planning <Month> - <Year>" sheets; Trade rows are month x party x category the same way, from the store x SKU lines of the "Trade Target Planning <Mon>''<yy>" sheets (plus, only in months with no Trade sheet, the DMart / Jio BP / GT rows the Marketplace sheets list). source=target_sku is the SKU grain of both, a reference the boards do not read. The Management MIS is not a source. Never add rows of different levels together. Loaded by sheet_grn_loader/targets_loader.py.';
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


def marketplace_rows(c, detail_out=None, trade_months=frozenset()):
    svc = build('sheets', 'v4', credentials=c, cache_discovery=False)
    sheets = find_target_planning_sheets(c)
    if not sheets:
        log.warning('no "Target Planning <Month> - <Year>" sheet visible to %s', account_of(c))
    out, months = [], set()
    for month, sid, name in sheets:
        try:
            det = [] if detail_out is not None else None
            month, lines = parse_marketplace(svc, sid, month, det)
            if month in trade_months:
                # the Trade sheet is this month's Trade target; the marketing plan's DMart / Jio BP / GT go
                n = len(lines)
                lines = [ln for ln in lines if ln[0] not in TRADE_PARTIES]
                if det is not None:
                    det = [d for d in det if d['party'] not in TRADE_PARTIES]
                if n != len(lines):
                    log.info('%s: %d Trade-party lines left to the Trade sheet', name, n - len(lines))
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


# ------------------------------------------------------------------ trade
def find_trade_planning_sheets(c):
    drive = build('drive', 'v3', credentials=c, cache_discovery=False)
    q = "name contains 'Trade Target Planning' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
    r = drive.files().list(q=q, fields='files(id, name)', pageSize=200,
                           includeItemsFromAllDrives=True, supportsAllDrives=True, corpora='allDrives').execute()
    by3 = {k[:3]: v for k, v in MONTHS.items()}
    out = []
    for f in r.get('files', []):
        m = TRADE_PLANNING_NAME.match(f['name'].strip())
        if not m or m.group(1).lower()[:3] not in by3:
            continue
        year = int(m.group(2))
        month = dt.date(year + 2000 if year < 100 else year, by3[m.group(1).lower()[:3]], 1)
        if month >= FY_START:
            out.append((month, f['id'], f['name']))
    return sorted(out)


def pl_net_sales(svc, sheet_id):
    """The `Target P&L` tab's Net Sales total (the check the lines must add up to), or None."""
    grid = values(svc, sheet_id, "'Target P&L'!A1:BZ80")
    hdr = next((r for r in grid if r and str(r[0]).strip().upper().startswith('CHANNEL P&L')), None)
    net = next((r for r in grid if r and str(r[0]).strip().lower() == 'net sales'), None)
    if hdr is None or net is None:
        return None
    c_total = next((i for i, h in enumerate(hdr) if i and str(h).strip().upper().startswith('TOTAL')), None)
    return num(net[c_total]) if c_total is not None and c_total < len(net) else None


def parse_trade(svc, sheet_id, month):
    """One dict per store x item line of the `<Mon> TGT` tab (lines with no quantity and no value are left out)."""
    tabs = [x['properties']['title'] for x in svc.spreadsheets().get(spreadsheetId=sheet_id, fields='sheets(properties(title))').execute()['sheets']]
    tab = next((x for x in tabs if re.fullmatch(r'[A-Za-z]+ TGT', x.strip()) and x.strip().upper() != 'PARTY TGT'), None)
    if tab is None:
        raise ValueError(f'no "<Mon> TGT" tab (tabs: {tabs})')
    grid = values(svc, sheet_id, f"'{tab}'!A1:BZ20000")
    hi = next((i for i, r in enumerate(grid[:6]) if r and str(r[0]).strip().upper() == 'PARTY'), None)
    if hi is None:
        raise ValueError(f'{tab}: no PARTY header row in the first 6 rows')
    headers = [re.sub(r'\s+', ' ', str(h)).strip().upper() for h in grid[hi]]
    first = {}
    for i, h in enumerate(headers):
        first.setdefault(h, i)
    need = {k: first.get(k) for k in ('PARTY', 'NAME', 'CITY', 'CATEGORY', 'ITEM NAME', 'NET SALES TGT')}
    c_qty = next((i for i, h in enumerate(headers) if h.endswith('NET SALES QTY')), None)
    missing = [k for k, v in need.items() if v is None] + (['<Mon> Net Sales Qty'] if c_qty is None else [])
    if missing:
        raise ValueError(f'{tab}: no {", ".join(missing)} column (headers: {headers[:12]}...)')
    ladder = {first[h]: col for h, col in TRADE_LADDER.items() if h in first}
    out = []
    for r in grid[hi + 1:]:
        r = list(r) + [''] * (len(headers) - len(r))
        party = str(r[need['PARTY']]).strip()
        store = str(r[need['NAME']]).strip()
        item = re.sub(r'\s+', ' ', str(r[need['ITEM NAME']]).strip().upper())
        if not party or not store or not item:
            continue
        q = num(r[c_qty]) or 0.0
        d = {'month': month, 'party': TRADE_PARTY.get(party.upper(), party_of(party)), 'platform_raw': party,
             'location': store, 'city_raw': str(r[need['CITY']]).strip(), 'sku_name': SKU_ALIAS.get(item, item),
             'parent_sku': None, 'category': title(str(r[need['CATEGORY']]).strip()) or '(none)', 'qty': q}
        for i, col in ladder.items():
            d[col] = num(r[i]) or 0.0
        if not q and not d.get('net_sales'):
            continue
        out.append(d)
    return tab, out


def trade_detail(c, sheets):
    svc = build('sheets', 'v4', credentials=c, cache_discovery=False)
    if not sheets:
        log.warning('no "Trade Target Planning <Mon>\'<yy>" sheet visible to %s', account_of(c))
    detail, months, bad = [], set(), []
    per_month = {}
    for month, sid, name in sheets:
        per_month.setdefault(month, []).append(f'{name} ({sid})')
    for month, sid, name in sheets:
        if len(per_month[month]) > 1:
            # two sheets for one month would load the target twice: keep the month's last good load until one goes
            if f'{name} ({sid})' == per_month[month][0]:
                log.error('%d Trade sheets for %s, month skipped (previous load kept) -- delete or rename all but one: %s',
                          len(per_month[month]), month.strftime('%b-%y'), '; '.join(per_month[month]))
            bad.append(name)
            continue
        try:
            tab, lines = parse_trade(svc, sid, month)
            check = pl_net_sales(svc, sid)
        except (HttpError, ValueError) as e:
            status = getattr(getattr(e, 'resp', None), 'status', None)
            log.error('%s (%s): %s', name, sid, f'HTTP {status} -- share it with {account_of(c)}' if status else e)
            bad.append(name)
            continue
        if not lines:
            log.warning('%s: no target lines, skipped', name)
            continue
        tot = sum(d.get('net_sales') or 0.0 for d in lines)
        if check and abs(tot - check) > 0.01 * check:
            log.warning('%s: store x SKU lines add up to Rs %.2f L, the Target P&L says Rs %.2f L', name, tot / 1e5, check / 1e5)
        detail += lines
        months.add(month)
        log.info('%s: %d store x SKU lines (%s), %d stores, net sales target Rs %.2f L', name, len(lines), tab,
                 len({d['location'] for d in lines}), tot / 1e5)
    return detail, months, bad


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
        # replace exactly the (source, channel, month, party-level?) slices this run read -- both runs write
        # source='target' Trade rows (Marketplace: DMart / Jio BP / GT per party; Trade: the channel total,
        # party NULL) and must not wipe each other
        keys = sorted({(r[8], r[1], r[0], r[2] is None) for r in rows})
        for src, chan, month, channel_level in keys:
            cur.execute('delete from warehouse.sales_targets where source = %s and channel_group = %s and month = %s and (party is null) = %s',
                        (src, chan, month, channel_level))
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
-- which sheet a line came from, so the Marketplace and Trade loads replace only their own lines
alter table warehouse.sales_targets_detail add column if not exists plan text not null default 'marketplace';
"""


def write_detail(conn, detail, plan):
    """Replace `plan`'s lines for the months in `detail` ('marketplace' or 'trade')."""
    if not detail:
        return 0
    months = sorted({d['month'] for d in detail})
    cols = DETAIL_COLS
    def grp(d):
        return 'Trade' if plan == 'trade' or d.get('party') in TRADE_PARTIES else 'Marketplace'
    rows = [tuple({'channel_group': grp(d), 'source': 'target', 'item_no': None, 'plan': plan}[c]
                  if c in ('channel_group', 'source', 'item_no', 'plan') else d.get(c) for c in cols) for d in detail]
    with conn.cursor() as cur:
        cur.execute(DETAIL_DDL)
        cur.execute('delete from warehouse.sales_targets_detail where source = %s and plan = %s and month = any(%s)', ('target', plan, months))
        if plan == 'trade':
            # a month with a Trade sheet takes its Trade target from there only
            cur.execute("delete from warehouse.sales_targets_detail where source = 'target' and plan = 'marketplace' and channel_group = 'Trade' and month = any(%s)", (months,))
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
        if plan == 'trade':
            # the Trade sheet's store is the invoice ship-to: take the party the register gives that ship-to
            # (COMPASS INDIA's stores are GT there), so target and actual meet on the same party
            cur.execute("""
                update warehouse.sales_targets_detail t
                   set party = m.party
                  from (select warehouse.location_key(ship_to_name) as k, mode() within group (order by party_master) as party
                          from warehouse.pnl_lines
                         where business = 'TRADE' and party_master is not null and party_master <> '(unmapped)'
                         group by 1) m
                 where t.plan = 'trade' and t.month = any(%s) and warehouse.location_key(t.location) = m.k
                   and t.party is distinct from m.party""", (months,))
            log.info('trade: %d lines took the register\'s party for their ship-to', cur.rowcount)
        cur.execute("select rolname from pg_roles where rolname like 'birbal_scope_%' or rolname = 'birbal_engine'")
        for (role,) in cur.fetchall():
            cur.execute(f'grant select on warehouse.sales_targets_detail to "{role}"')
    conn.commit()
    log.info('detail (%s): wrote %d lines for %s', plan, len(rows), ', '.join(m.strftime('%b-%y') for m in months))
    return len(rows)


def write_trade_targets(conn, months):
    """sales_targets' Trade rows for the Trade-sheet months, folded from the detail just written: party x
    category (source='target', what the boards read) and SKU (source='target_sku'), units and value."""
    months = sorted(months)
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("delete from warehouse.sales_targets where channel_group = 'Trade' and month = any(%s) and source in ('target', 'target_sku', 'target_party')", (months,))
        cur.execute("""
            with d as (select month, party, category, sku_name, sum(qty) as q, sum(net_sales) as v
                         from warehouse.sales_targets_detail
                        where source = 'target' and plan = 'trade' and month = any(%s)
                        group by 1, 2, 3, 4)
            insert into warehouse.sales_targets (month, channel_group, party, category, item_no, sku, measure, target, source)
            select month, 'Trade', party, category, null, null, 'units', round(sum(q), 3), 'target' from d group by month, party, category
            union all
            select month, 'Trade', party, category, null, null, 'value', round(sum(v), 2), 'target' from d group by month, party, category
            union all
            select month, 'Trade', party, category, null, sku_name, 'units', round(q, 3), 'target_sku' from d
            union all
            select month, 'Trade', party, category, null, sku_name, 'value', round(v, 2), 'target_sku' from d""", (months,))
        n = cur.rowcount
        try:
            cur.execute("insert into public.workflow_logs (workflow, source, started_at, ended_at, status, details, rows_written, processed, created_at) values (%s, %s, now(), now(), 'success', %s::jsonb, %s, %s, now())",
                        ('sheet_grn_loader:targets_trade', 'targets:trade', json.dumps({'months': [m.isoformat() for m in months]}), n, n))
        except Exception as e:                            # noqa: BLE001
            log.warning('workflow_logs not written: %s', str(e)[:100])
    conn.commit()
    log.info('trade: wrote %d target rows for %s', n, ', '.join(m.strftime('%b-%y') for m in months))
    return n


def refresh_target_vs_actual(conn):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=['marketplace', 'trade', 'all'], default='all')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--token', default=os.environ.get('GOOGLE_TOKEN_JSON', 'token.json'))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    conn = None if a.dry_run else connect()
    c = creds(a.token)
    trade_sheets = find_trade_planning_sheets(c)
    trade_months = frozenset(m for m, _, _ in trade_sheets)
    failed, wrote = False, False
    if a.source in ('marketplace', 'all'):
        detail = []
        rows, months = marketplace_rows(c, detail, trade_months)
        if a.dry_run:
            print(f'marketplace: {len(rows)} rows; sample: {rows[:4]}')
            print(f'detail: {len(detail)} location x SKU lines; sample: {detail[:1]}')
        elif rows:
            write(conn, rows, months, ('target', 'target_sku'), 'marketplace')
            write_detail(conn, detail, 'marketplace')
            wrote = True
        else:
            failed = True
    if a.source in ('trade', 'all'):
        detail, months, bad = trade_detail(c, trade_sheets)
        if a.dry_run:
            print(f'trade: {len(detail)} store x SKU lines; sample: {detail[:1]}')
        elif detail:
            write_detail(conn, detail, 'trade')
            write_trade_targets(conn, months)
            wrote = True
        failed = failed or not detail or bool(bad)
    if wrote:
        refresh_target_vs_actual(conn)
    if conn:
        conn.close()
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
