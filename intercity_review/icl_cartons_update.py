"""What the logistics team's carton sizes add to the freight review:
   1. ERP boxes per dispatch (units / sheet carton) vs the logged boxes -> confidence in the fill figures
   2. billed trucks missing from the dispatch log, their boxes from the ERP on the LR date -> wider fill coverage
   3. freight per unit by SKU (billed months of the period) and as % of MRP
Writes icl_cartons.json for the report.
"""
import sys
import json
import datetime as dt
import pandas as pd

from dbq import run

from icl_env import W as SP, M0, M1, D1
u = pd.read_pickle(SP + "icl2_u.pkl")
b = pd.read_pickle(SP + "icl2_b.pkl")
t = pd.read_pickle(SP + "icl2_t.pkl")
rates = pd.read_pickle(SP + "icl_rates.pkl")
rate_map = {(r.month, r.lane, r.size): r.rate for r in rates.itertuples()}
from volume import CAP   # a truck's boxes = normal-carton equivalents by volume (215)
WH = {"WH/0002": "Mumbai", "WH/0003": "Hyderabad", "WH/0004": "Bangalore", "WH/0005": "Gurgaon", "WH/0006": "Kolkata"}

_, r = run("""
select e."Posting_Date" d, e."Transfer_to" wh, sum(-e."Quantity" / c.carton_size) boxes,
       sum(-e."Quantity" / c.carton_size * wip.carton_weight(e."Item_Description")) load,
       sum(-e."Quantity") filter (where c.carton_size is null) units_no_carton, sum(-e."Quantity") units
  from bc_item_ledger_entries e
  left join wip.ref_carton_size c on c.item_no = e."Item_No" and c.carton_size > 0
 where e."Document_Type" = 'Transfer Shipment' and e."Location_Code" = 'PL/0003'
   and e."Posting_Date" between %s::date - 7 and %s
   and e."Transfer_to" in ('WH/0002','WH/0003','WH/0004','WH/0005','WH/0006')
   and (e."Item_No" like 'FG/%%' or e."Item_No" like 'TG/%%')
 group by 1, 2""", (M0, D1), limit=100000)
erp = pd.DataFrame(r, columns=["d", "wh", "boxes", "load", "units_no_carton", "units"])
erp[["boxes", "load", "units_no_carton", "units"]] = erp[["boxes", "load", "units_no_carton", "units"]].astype(float).fillna(0)
erp["lane"] = erp.wh.map(WH)
day = erp.groupby(["lane", "d"]).agg(erp_boxes=("boxes", "sum"), erp_load=("load", "sum"), no_carton=("units_no_carton", "sum")).reset_index()
no_carton_share = erp.units_no_carton.sum() / erp.units.sum()

# 1. per dispatch: log vs ERP (same lane, same day; days with one truck only, so the day is the truck)
one = u.groupby(["lane", "dispatch"]).filter(lambda g: len(g) == 1)
x = one.merge(day, left_on=["lane", "dispatch"], right_on=["lane", "d"], how="inner")
x["ratio"] = x.cartons / x.erp_boxes   # counts against counts
within10 = ((x.ratio - 1).abs() <= .10).mean()
cmp = dict(trucks=int(len(x)), median=round(float(x.ratio.median()), 3), within10=round(float(within10) * 100),
           by_lane={k: round(float(v), 2) for k, v in x.groupby("lane").ratio.median().items()},
           no_carton_pct=round(no_carton_share * 100, 2))
print("1.", cmp)

# 2. billed trucks with no logged dispatch: boxes from the ERP on the LR date (+/- 1 day if nothing that day)
logged = set(zip(t.carrier, t.lane, t.lr))
miss = b[[(c, l, r) not in logged for c, l, r in zip(b.carrier, b.lane, b.lr)]].copy()
def erp_boxes(r):
    if r.lr_date is None or pd.isna(r.lr_date):
        return None
    for off in (0, -1, 1):
        d = r.lr_date + dt.timedelta(days=off)
        hit = day[(day.lane == r.lane) & (day.d == d)]
        if len(hit):
            return float(hit.erp_load.iloc[0])   # by volume, like the logged trucks
    return None
miss["boxes"] = miss.apply(erp_boxes, axis=1)
# a day's ERP boxes belong to one truck only if no logged truck that lane left the same day
busy = set(zip(u.lane, u.dispatch))
miss["clean"] = [pd.notna(bx) and (ln, d) not in busy for ln, d, bx in zip(miss.lane, miss.lr_date, miss.boxes)]
add = miss[miss.clean].copy()
add["size"] = add.bill_size.astype(int)
add["cap"] = add["size"].map(CAP)
add["rate"] = add.billed
add["util"] = add.boxes / add.cap
print("2. billed trucks not in the log:", len(miss), "| with ERP boxes on a clean day:", len(add),
      add.groupby("lane").size().to_dict(), "| their fill", round(add.boxes.sum() / add.cap.sum() * 100, 1))
both = pd.concat([u[["lane", "boxes", "cap", "rate"]], add[["lane", "boxes", "cap", "rate"]]])
both["util"] = both.boxes / both.cap
ext = dict(added=int(len(add)), missing=int(len(miss)), added_by_lane=add.groupby("lane").size().astype(int).to_dict(),
           added_fill=round(float(add.boxes.sum() / add.cap.sum() * 100), 1),
           trucks=int(len(both)), fill=round(float(both.boxes.sum() / both.cap.sum() * 100), 1),
           empty_l=round(float((both.rate * (1 - both.util.clip(upper=1))).sum() / 1e5), 2),
           paid_l=round(float(both.rate.sum() / 1e5), 2))
print("   extended:", ext)

# 3. freight per unit by SKU, billed months of the period, and as % of MRP
_, r = run("""
select s.item_no, max(s.description) d, max(s.category) cat, max(c.carton_size) carton, max(c.mrp) mrp,
       sum(s.units) units, sum(s.freight) freight, sum(s.freight) / nullif(sum(s.units), 0) per_unit
  from wip.intercity_freight_sku s left join wip.ref_carton_size c on c.item_no = s.item_no
 where s.month between %s and %s and s.freight is not null
 group by 1 order by 7 desc""", (M0, M1), limit=1000)
sku = pd.DataFrame(r, columns=["item", "desc", "cat", "carton", "mrp", "units", "freight", "per_unit"])
for c in ("carton", "mrp", "units", "freight", "per_unit"):
    sku[c] = sku[c].astype(float)
sku["pct_mrp"] = sku.per_unit / sku.mrp * 100
_, r2 = run("""select item_no, sum(freight)/nullif(sum(units),0) from wip.intercity_freight_sku
                where month between %s and %s and freight is not null group by 1""", (M0, M1))
top = sku.head(15)
skus = [dict(item=r.item, desc=r.desc.title(), cat=(r.cat or "").title(), carton=None if pd.isna(r.carton) else int(r.carton),
             mrp=None if pd.isna(r.mrp) else int(r.mrp), units=int(r.units), freight_l=round(r.freight / 1e5, 2),
             per_unit=round(r.per_unit, 2), pct_mrp=None if pd.isna(r.pct_mrp) else round(r.pct_mrp, 1)) for r in top.itertuples()]
hi = sku[sku.units > 20000].sort_values("pct_mrp", ascending=False).head(5)
print("3. top SKUs:", [(s["item"], s["per_unit"], s["pct_mrp"]) for s in skus[:6]])
print("   highest % of MRP (>20k units):", [(r.item, r.desc, round(r.per_unit, 2), round(r.pct_mrp, 1)) for r in hi.itertuples()])
tot_units = sku.units.sum()
overall = dict(units=int(tot_units), freight_l=round(sku.freight.sum() / 1e5, 2), per_unit=round(sku.freight.sum() / tot_units, 2),
               mrp_weighted_pct=round(float((sku.freight.sum()) / (sku.units * sku.mrp).sum() * 100), 2))
print("   overall:", overall)
json.dump(dict(cmp=cmp, ext=ext, skus=skus, overall=overall,
               high_pct=[dict(item=r.item, desc=r.desc.title(), per_unit=round(r.per_unit, 2), mrp=int(r.mrp), pct_mrp=round(r.pct_mrp, 1))
                         for r in hi.itertuples()]), open(SP + "icl_cartons.json", "w"), indent=1)
