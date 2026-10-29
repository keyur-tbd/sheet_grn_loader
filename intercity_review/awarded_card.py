"""The contracted rate card: FY'27 tab, block "Rate Card - July, 2026 onwards until 31st Dec, 2027" (BA:BF).

Owner, 2026-10-08: "this is the real rate card". Four vendors quote every lane for 20 / 22 / 32 ft; the cells shaded
green are the vendor each lane is awarded to (Mumbai RR, Bangalore Meta, Gurgaon Radhe Radhe, Hyderabad and Kolkata
DBRL on 8 Oct). Read with cell formats so the award comes from the sheet, not from a reading of it.
Writes work/awarded_card.json: {quotes: [...], awarded: {lane: {vendor, rates: {size: rate}}}, read_at}.
"""
import datetime as dt
import json
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SHEET = "1jMW9R_y21ytPlnCpXwcRbzjIaqzcZtH61aVQtephci8"
from icl_env import W, TOKEN
VENDOR = {"META": "Meta Road", "RADHE RADHE": "Radhe Radhe", "RR LOGISTICS": "R R Logistics", "DBRL": "Delhi Bangalore Roadlines"}
LANE = {"MUMBAI": "Mumbai", "BLR": "Bangalore", "GURGAON": "Gurgaon", "HYDERABD": "Hyderabad", "HYDERABAD": "Hyderabad",
        "KOLKATTA": "Kolkata", "KOLKATA": "Kolkata"}

c = Credentials.from_authorized_user_file(TOKEN)
if not c.valid and c.refresh_token:
    c.refresh(Request())
svc = build("sheets", "v4", credentials=c)
res = svc.spreadsheets().get(spreadsheetId=SHEET, ranges=["'FY''27'!BA1:BF40"], includeGridData=True,
                             fields="sheets.data.rowData.values(effectiveValue,effectiveFormat.backgroundColor)").execute()
grid = []
for row in res["sheets"][0]["data"][0].get("rowData", []):
    cells = []
    for v in row.get("values", []):
        ev = v.get("effectiveValue", {})
        val = ev.get("numberValue", ev.get("stringValue"))
        bg = v.get("effectiveFormat", {}).get("backgroundColor", {})
        green = bg.get("green", 0) > .8 and bg.get("red", 0) < .6 and bg.get("blue", 0) < .6
        cells.append((val, green))
    grid.append(cells)

quotes, awarded = [], {}
for r, row in enumerate(grid):
    head = str(row[0][0]).strip().upper() if row and row[0][0] is not None else ""
    if head not in VENDOR:
        continue
    vendor = VENDOR[head]
    lanes = [LANE.get(str(row[k][0]).strip().upper()) if k < len(row) and row[k][0] else None for k in range(1, 6)]
    for rr in range(r + 2, min(r + 5, len(grid))):
        m = re.search(r"(\d+)", str(grid[rr][0][0] if grid[rr] else ""))
        if not m:
            continue
        size = int(m.group(1))
        for k, ln in enumerate(lanes, start=1):
            if not ln or k >= len(grid[rr]):
                continue
            val, green = grid[rr][k]
            if isinstance(val, (int, float)):
                quotes.append(dict(vendor=vendor, lane=ln, size=size, rate=float(val), awarded=green))
                if green:
                    awarded.setdefault(ln, dict(vendor=vendor, rates={}))
                    if awarded[ln]["vendor"] != vendor:
                        raise SystemExit(f"two vendors shaded green on {ln}: {awarded[ln]['vendor']} and {vendor}")
                    awarded[ln]["rates"][size] = float(val)
out = dict(read_at=dt.datetime.now().isoformat(timespec="seconds"), valid_from="2026-07-01", valid_to="2027-12-31",
           quotes=quotes, awarded=awarded)
json.dump(out, open(W + "awarded_card.json", "w", encoding="utf-8"), indent=1)
print(len(quotes), "quotes")
for ln, a in awarded.items():
    print(f"  {ln:10s} {a['vendor']:28s} {a['rates']}")
