"""Sheets the review workbook gets after icl_report_data.py rewrites it: per-product freight, trucks incl. the ones
the logs missed, merge pairs, the contracted card, and truck-by-truck actions. Run after icl_trips_out.py."""
import sys
import json
import pandas as pd

from dbq import run

from icl_env import W, EXCEL as X, M0, M1, PERIOD
_, r = run("""select s.item_no, max(s.description), max(c.carton_size), max(c.mrp), sum(s.units), sum(s.freight),
  sum(s.freight)/nullif(sum(s.units),0), string_agg(distinct s.status, ', ')
  from wip.intercity_freight_sku s left join wip.ref_carton_size c on c.item_no = s.item_no
 where s.month between %s and %s and s.freight is not null group by 1 order by 6 desc""", (M0, M1), limit=1000)
prod = pd.DataFrame(r, columns=["Item", "Product", "Units per carton (sheet)", "MRP", "Units sent " + PERIOD, "Freight Rs", "Rs per unit", "Basis"])
for c in ["Units per carton (sheet)", "MRP", "Units sent " + PERIOD, "Freight Rs", "Rs per unit"]:
    prod[c] = prod[c].astype(float)
prod["% of MRP"] = prod["Rs per unit"] / prod["MRP"] * 100
card = json.load(open(W + "awarded_card.json", encoding="utf-8"))
q = pd.DataFrame(card["quotes"]).pivot_table(index=["lane", "size"], columns="vendor", values="rate").reset_index()
q["Awarded to"] = q.lane.map({ln: a["vendor"] for ln, a in card["awarded"].items()})
q["Card"] = [card["awarded"][ln]["rates"].get(str(s), card["awarded"][ln]["rates"].get(s)) for ln, s in zip(q.lane, q["size"])]
trips = json.load(open(W + "icl_trips.json", encoding="utf-8"))
tr = pd.DataFrame([{**{k: v for k, v in t.items() if k != "actions"}, "what to do": "; ".join(a["text"] for a in t["actions"])} for t in trips])
with pd.ExcelWriter(X, engine="openpyxl", mode="a", if_sheet_exists="replace") as xw:
    prod.round(2).to_excel(xw, sheet_name="Freight per product", index=False)
    pd.read_pickle(W + "icl3_allt.pkl").sort_values(["lane", "dispatch"]).round(3).to_excel(xw, sheet_name="Trucks (utilisation)", index=False)
    pd.read_pickle(W + "icl3_mg.pkl").to_excel(xw, sheet_name="Merge candidates", index=False)
    q.to_excel(xw, sheet_name="Rate card Jul26-Dec27", index=False)
    tr.to_excel(xw, sheet_name="Truck by truck", index=False)
    cc = json.load(open(W + "icl_carton_cmp.json", encoding="utf-8"))
    pd.DataFrame(cc["days"]).to_excel(xw, sheet_name="Cartons vs log (days)", index=False)
    pd.DataFrame(cc["skus"]).to_excel(xw, sheet_name="Cartons vs log (items)", index=False)
print("excel ok:", len(prod), "products,", len(tr), "trucks")
