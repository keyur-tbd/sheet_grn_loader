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
- `row_hash` = sha256(sheet id | tab | canonical row JSON). Re-reading the sheet writes nothing new;
  a row edited in the sheet lands as a new row. After each full load, older stored versions of a line the sheet still
  carries are deleted (`line_key`: Amazon invoice x PO x ASIN, Zepto PO x SKU), so the table holds the sheet's current
  version of every line. Lines that have left the sheet are kept.
- `raw_data` keeps the untouched row; `source_file` = "<sheet title> / <tab>", `drive_file_id` = sheet id,
  `sheet_row` = row number at load time.
- Runs are logged to `public.workflow_logs` with `source = sheet_grn:<source>` when that table exists.
