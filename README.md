# sheet_grn_loader

Loads GRN reports that are downloaded from a partner portal and pasted into a Google Sheet into
Supabase, one table per partner, one row per sheet row, idempotent on `row_hash`.

| source | sheet | table |
|---|---|---|
| zepto  | https://docs.google.com/spreadsheets/d/1Txws1Qan9QVyR3qJTlup5BadQ9KwI6qk84KQb8bMbEQ | `public.zepto_grn` |
| amazon | https://docs.google.com/spreadsheets/d/1IjwfjZg_l9JTiRlosZaNId-QPpfHi48JMH1_Tz8trPo | `public.amazon_grn` |

## One-time setup

1. **Share both sheets (Viewer is enough) with the Google account whose `token.json` the loader uses.**
   Any of the existing scheduler accounts works; the token in `GRN Github Repos/instamart_grn_scheduler-main`
   is `instamart@thebakersdozen.in`. Until this is done every call returns 403.
2. `pip install -r requirements.txt`, copy `.env.example` to `.env`, point `GOOGLE_TOKEN_JSON` at that token.
3. `python sheet_grn_loader.py --source zepto --discover` — prints the tabs, every header with its inferred type
   and the proposed `create table`. If a header needs a different name or type, add it to `SOURCES[...]['columns']`
   as `'normalisedheader': ('column_name', 'numeric'|'date'|'text')`.
4. `python sheet_grn_loader.py --source zepto --apply-schema`, then `python sheet_grn_loader.py --source zepto`.
   Same for `amazon`.
5. Add the new feeds to the order-to-cash bridge (`o2c_bridge.sql`, the `raw` union in `o2c_grn_lines`) and to
   `ref_customer_invoicing.grn_feed`, then `select public.o2c_refresh()`.

## Scheduling

`.github/workflows/sheet_grn.yml` runs both sources every 3 hours (`17 */3 * * *` UTC). It does not refresh the
order-to-cash bridge: `o2c_refresh` runs on pg_cron at 09:00 and 14:00 IST and picks the new rows up then (a second
concurrent refresh deadlocks).
Secrets: `SUPABASE_DB_URL` (session pooler host, runners are IPv4 only) and `GOOGLE_TOKEN_JSON_B64`
(`base64 -w0 token.json`).

## Behaviour

- Cells are read with `valueRenderOption=UNFORMATTED_VALUE`, so long identifiers never arrive as `3.23E+12`
  (the sheet's CSV export is lossy — do not backfill from a CSV).
- **Upsert on the line key** (`line_key`: Amazon PO x ASIN, since the sheet's Invoice Number is always blank;
  Zepto PO x SKU), backed by a unique index `<table>_line_key_uidx` the loader creates if missing:
  - a line in the sheet and in Supabase is replaced by the sheet's version (same `id` and `created_at`, new
    `processed_at`); an unchanged line (same `row_hash`) is not touched;
  - a line only in the sheet is inserted;
  - a line only in Supabase (history the sheet no longer carries) is kept as it is.
- If a key appears on two sheet rows the load stops with an error rather than silently keep one of them.
- `row_hash` = sha256(sheet id | tab | canonical row JSON), used to skip unchanged lines.
- `raw_data` keeps the untouched row; `source_file` = "<sheet title> / <tab>", `drive_file_id` = sheet id,
  `sheet_row` = row number at load time.
- Runs are logged to `public.workflow_logs` with `source = sheet_grn:<source>` when that table exists.

## Never-billed POs sheet (`never_billed_sheet.py`, 2026-09-23)

The Birbal SCM Tracker's pending-PO list as a Google Sheet for the SCM team: "Birbal - Never billed POs"
(`18oXNb4bHhQ-ealzgcLysEd0EAyzGKBjOHxUDhSvcY7I`, tabs *Never billed* and *Reversed by credit memo*), one row per
FY27 PO with nothing billed against it, from `warehouse.fill_rate_lines` (the tracker's own snapshot). The
`never_billed` job rewrites both tabs after every load (3-hourly); row 1 says when, row 2 is the totals.
`--create` made the sheet once (owner: the loader's Google account, birbal@ as writer); share it wider from
the sheet's own Share dialog. Runs log to `workflow_logs` as `sheet_grn:never_billed`.

## Sales targets (`targets_loader.py`, 2026-09-23)

Loads `warehouse.sales_targets` for Birbal's Primary Sales board (migration 102) on the same 3-hourly
beat (`targets` job):

* **Marketplace** -- every "Target Planning <Month> - <Year>" sheet from April 2026 (found in Drive by
  name; tab `Target`, header row 3): `QTY TGT` and `T - Net Sales` per location x SKU, folded to month x
  party x category (`source='target'`) and kept per SKU (`source='target_sku'`).
* **Trade** -- the Trade team's "Trade Target Planning <Mon>'<yy>" sheets (from Jun'26, shared with instamart@),
  tab `<Mon> TGT`: one line per store x item (store = invoice ship-to name) with the ladder up to gross
  margin -> `warehouse.sales_targets_detail` (`plan='trade'`), folded to party x category (`source='target'`)
  and SKU (`target_sku`). Checked against the `Target P&L` tab's Net Sales. In a month with a Trade sheet the
  Marketplace sheets' DMart / Jio BP / GT lines are dropped. Not finance's "Management MIS FY27" sheet.

`python targets_loader.py --dry-run` parses the sheets and writes nothing.

## Field staff cost by store (`staff_salary_loader.py`, 2026-10-02)

Replaces `public.pnl_store_salaries` from the confidential "Salary Detail" sheet
(`1h9kKr91dLNruatRleBAIUmndTQk8rewhobzvS4Uj7cU`, tab "Salaries promoters Working"), shared with
**birbal@ only**, so it has its own token: secret `STAFF_SALARY_TOKEN_JSON_B64` (locally
`D:\Python\Birbal\salary_loader\token_birbal.json`, spreadsheets.readonly). Store grain WITHOUT NAMES:
the name becomes an HMAC under a key drawn fresh per run (head counts only), whole pay is not stored,
logs carry counts only. Refuses a read under half the rows already loaded. Then refreshes Birbal's
`warehouse.store_staff_cost` / `trade_store_month` snapshots (`app.refresh_store_staff()`, migration 133).
The Feeder File's copy of this tab is no longer synced by marketplace-ads-pipeline (its key folded a
person's stores into one row).

## RTV support + fixed TOT % (`rtv_support_loader.py`, 2026-10-03)

For Birbal's RTV Analysis board (`/rtv`, migration 141). Reads the Drive folder "Platform wise RTV Data"
(`1vlVTGF3biSMxlJox7_OpkhZVuMRkxNRA`, owned by zepto@, shared with birbal@) with birbal@'s **Drive** token:
secret `RTV_SUPPORT_TOKEN_JSON_B64` (locally `D:\Python\Birbal\salary_loader\token_birbal_drive.json`,
drive.readonly -- the salary token is Sheets-only and cannot list a folder). The job skips until the secret exists.

- `<Platform>/` sub-folders (Blinkit, Instamart, Zepto, Flipkart): every Google Sheet / .xlsx / .csv, every tab
  with a Month | Item ID | SKU Name | City | Alignment header -> `public.rtv_support_lines` (product x city x
  month the partner takes back in full; "100% RTV" = 1.0). Months without a year are dated from the sheet's
  order (last row = latest such month no later than next month).
- `Overall Fixed RTV TOT - Platform wise/`: Platform | Category | RTV % (blank platform = the one above)
  -> `public.rtv_fixed_tot`.

Both tables are replaced on every run, then `app.refresh_rtv_analysis()` rebuilds the board's snapshot
(migration 142); a read under half the support rows already loaded is refused.
`python rtv_support_loader.py --dry-run` reads and writes nothing.

## Expense tagging (`expense_sheet_loader.py`, 2026-10-05)

For Birbal's Expense Analysis board (`/expenses`, migration 151). Reads the finance team's Google Sheet
"Expenses Analysis Report FY27" (`1siBXpxc4R1REY0Oi89BRvwetGrlJ2aSWcFhmM-6iyIg`, shared with birbal@; override
with `EXPENSE_SHEET_ID` when the team starts a new monthly file) with birbal@'s Drive token (same secret as
rtv_support). Birbal reads the G/L itself; from the sheet it takes the team's judgement only:
- `ERP_Raw_Data` -> `public.expense_sheet_lines`: Department, Type of Exp and Expense Month per G/L line
  (document no. x account x posting date x amount; labels such as "Previous Year" kept as written);
- `Names & Codes` + `MIS Mapping` -> `public.expense_gl_map` (account -> department, type, MIS heads, provision
  account) and `public.expense_party_map` (party -> short name, group).
Every run replaces the three tables; a read with under half the tagged lines already loaded is refused.
`python expense_sheet_loader.py --dry-run` reads and writes nothing.
