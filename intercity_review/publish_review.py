"""Write the Intercity Freight Review into Birbal's WIP area (wip.intercity_review, migration 164).

Run order (each step reads the previous one's files in work/):
    python ratecard_read.py        # the Intercity Logistics Tracker sheet -> work/ratecard.json (birbal@ token)
    python awarded_card.py         # the July 2026 card with the awarded (green) vendor per lane
    python icl_build.py            # trips, LR bills, rate cards, quotes, MIS
    python icl_analyse2.py         # bills vs card, fill (by volume, volume.py), right-size, merge, vendors
    python icl_report_data.py      # headline figures + the Excel (icl_env.EXCEL)
    python icl_cartons_update.py   # ERP boxes via the logistics carton sizes, freight per product
    python icl_levers_ext.py       # levers including billed trucks the logs missed
    python icl_trips_out.py        # truck by truck, with the action each one calls for
    python icl_cartons_compare.py  # our cartons (ERP units / carton size) vs the logged boxes, by lane and day
    python excel_extras.py         # the sheets icl_report_data.py does not write
    python publish_review.py       # -> wip.intercity_review (the WIP page shows the newest row)
"""
import json
import sys

from dbq import connect  # noqa: E402

from icl_env import W, PERIOD
payload = json.load(open(W + "icl_report_clean.json", encoding="utf-8"))
payload["cart"] = json.load(open(W + "icl_cartons.json", encoding="utf-8"))
# truck by truck, with the action each one calls for (icl_trips_out.py)
payload["trips"] = json.load(open(W + "icl_trips.json", encoding="utf-8"))
# the contracted card (July 2026 - Dec 2027, awarded = green cells) and the undated vendor rates used before July
import vendor_card
payload["card"] = json.load(open(W + "awarded_card.json", encoding="utf-8"))
payload["card"]["pre_july"] = [e for e in vendor_card.TABLE if e["table"] in ("A1:L9", "AH:AX") and e["carrier"] != "Shiv Shakti"]
# cartons as per our calculation (ERP units / logistics carton size) vs the dispatch logs' boxes (icl_cartons_compare.py)
_cc = json.load(open(W + "icl_carton_cmp.json", encoding="utf-8"))
payload["carton_cmp"] = dict(totals=_cc["totals"], days=_cc["days"],
                             items={x["item"]: x["descr"] for x in _cc["skus"]},
                             lines=[[x["date"], x["lane"], x["item"], x["units"], x["carton"]] for x in _cc["skus"]])
# the rate card itself: FY'27 tab, each month block's "Cost" row -> lane x size x month
import pandas as pd
_r = pd.read_pickle(W + "icl_rates.pkl")
from volume import CAP as _cap, NORMAL_L   # wip.ref_truck_capacity, normal cartons (215)
_months = sorted(_r.month.unique())
payload["rate_card"] = dict(
    months=[m.strftime("%Y-%m-%d") for m in _months],
    rows=[dict(lane=ln, size=int(sz), cap=_cap.get(int(sz)),
               rates={m.strftime("%Y-%m-%d"): float(g[g.month == m].rate.iloc[0]) for m in _months if (g.month == m).any()})
          for (ln, sz), g in _r.groupby(["lane", "size"])])
# where every figure comes from, shown on the WIP page's Freight review view
TRACKER = "https://docs.google.com/spreadsheets/d/1jMW9R_y21ytPlnCpXwcRbzjIaqzcZtH61aVQtephci8/edit?gid=1468631383"
CARTONS = "https://docs.google.com/spreadsheets/d/1i1VnYly-QEExfqwYldnxFh8ZRSkJqm4rpfsw1V8-pAU/edit?gid=1669474053"
payload["sources"] = [
    dict(what="Rate card, July 2026 to December 2027", where="Intercity Logistics Tracker, tab FY'27, block \"Rate Card - July, 2026 onwards "
         "untill 31st Dec, 2027\" (columns BA-BF): four vendors' rates per lane for 20 / 22 / 32 ft; the green cells are the vendor each lane is "
         "awarded to (Mumbai RR, Bangalore Meta, Gurgaon Radhe Radhe, Hyderabad and Kolkata DBRL)",
         use="Every \"billed vs rate card\" figure from July: each bill against the awarded rate for its lane and billed size, whoever carried it. "
             "Also the cheaper-truck and combine savings from July.", link=TRACKER),
    dict(what="Rates before July", where="Same tab, the undated vendor tables A1:L9 (\"Rate Card\": RR, Vihira, Radhe, DB, Meta) and AH-AX "
         "(Meta Road, Delhi Bangalore Roadlines): each carrier's own rate per lane and size",
         use="April-June bills against the carrier's own rate. Not confirmed as contracted; where a carrier has no rate, the lane's monthly rate is used.",
         link=TRACKER),
    dict(what="Lane rates by month", where="Same tab, each month block's \"Cost\" row (rows 17 Apr, 38 May, 60 Jun, 81 Jul, 102 Aug, 123 Sep, "
         "144 Oct): one rate per lane and truck size, the rates logistics prices the MIS at",
         use="Shown for reference, and the fallback for a size no card prices. October's block is still being filled in.", link=TRACKER),

    dict(what="Truck capacity, by volume", where="Birbal wip.ref_truck_capacity (migration 232): the load by volume that each truck size has "
         f"proven it carries = the 95th percentile of Apr-Sep 2026 loads on trucks billed at their logged size: 22 ft {_cap[22]:,}, "
         f"32 ft {_cap[32]:,} normal cartons; 20 ft {_cap[20]:,} = the 22 ft scaled by length (few 20 ft trucks). 9 / 14 / 17 ft keep the "
         "tracker's FY'27 \"Carton Capacity\" (no trips to prove them). That row (1,430 / 1,595 / 2,500) counts BOXES of a mixed load: no logged "
         f"truck ever went past it. Truck volume = capacity x the normal carton (445 x 220 x 195 mm = {NORMAL_L:.2f} L)",
         use="Truck fill, right-sizing and combining. Fill = load volume / truck volume. Every box figure on this page is a load in "
             "normal-carton equivalents: a Donut Cake or Love Cake carton (640 x 280 x 265 mm) counts 2.49, a Protein Chips or Combo Box "
             "carton (Harry Chips, 660 x 380 x 370 mm) 4.86, every other carton 1. A logged truck's boxes are scaled by the product mix the ERP "
             "shipped on that lane that day.", link=TRACKER),
    dict(what="Trucks and boxes", where="Same sheet, the five warehouse dispatch-log tabs (Mumbai, Bangalore, Hyderabad, Kolkata, Delhi "
         "Warehouse): dispatch date, LR, actual truck size, boxes", use="Truck fill and the size check.", link=TRACKER),
    dict(what="Billed amount per LR", where="Same sheet, tabs \"Intercity Bills/LR-2026-27\" (Jun-Sep) and \"MIS Vs Actual - Bill Details\" (Apr-May)",
         use="The billed side of the rate check, and bill counts.", link=TRACKER),
    dict(what="Carton dimensions", where="Logistics, Carton Size.xlsx (27 Oct 2026): normal 445 x 220 x 195 mm, Donut Cake 640 x 280 x 265 mm; "
         "Harry Chips 66 x 38 x 37 cm", use="Each carton's volume relative to a normal one (wip.ref_carton_volume, wip.ref_carton_weight).", link=None),
    dict(what="Carton sizes", where="Pending POs sheet, tab \"Carton & MRP\" (loaded into Birbal every three hours)",
         use="ERP boxes for trucks the logs missed, and freight per product.", link=CARTONS),
    dict(what="Ledger", where="ERP G/L account 44120010 (dimension PLANT) and transfer shipments from plant PL/0003",
         use="The G/L column of the reconciliation and the per-product split.", link=None),
    dict(what="Not used", where="The tracker's older \"Rate Card\" and \"Updated Rate Card\" tabs (2022: diesel Rs 92.52/l, 550 boxes for a 20 ft)",
         use="Superseded by the FY'27 figures.", link=None),
]
conn = connect()
cur = conn.cursor()
cur.execute("insert into wip.intercity_review (period, source, payload) values (%s, %s, %s::jsonb) returning id, built_at",
            (PERIOD, "Intercity Logistics Tracker (1jMW9R_y21yt...) + ERP; carton sizes from Pending POs > Carton & MRP; fill by volume (215, 216, 232)",
             json.dumps(payload)))
print("published review id %s at %s (%d bytes)" % (*cur.fetchone(), len(json.dumps(payload))))
# rebuilt daily: keep the newest 30 snapshots (the WIP page shows the newest)
cur.execute("delete from wip.intercity_review where id < (select min(id) from (select id from wip.intercity_review order by id desc limit 30) k)")
print("pruned", cur.rowcount, "old snapshots")
conn.commit()
conn.close()
