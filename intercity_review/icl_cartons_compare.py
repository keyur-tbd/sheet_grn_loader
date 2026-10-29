"""Cartons as per our calculation vs the boxes in the warehouse dispatch logs (owner, 2026-10-09).

Our cartons = the ERP's transfer shipments from the plant (PL/0003) to the warehouse, FG/TG items, units / the
logistics team's carton size (wip.ref_carton_size), summed per lane per posting day. The log = "Total no of Boxes"
in the tracker's five warehouse tabs, summed per lane per dispatch day (dates already put back where the sheet had
swapped day and month). Compared per lane-day: a day can carry two trucks, and the ERP does not say which truck an
item went on. Where a log day has no ERP posting and the ERP posted the day before or after with no truck logged that
day, the two are paired (the posting date and the dispatch date can straddle midnight).
Writes work/icl_carton_cmp.json: days (lane x day), skus (lane x day x item), totals.
"""
import sys
import json
import datetime as dt
import pandas as pd

from dbq import run

from icl_env import W, M0, D1
WH = {"WH/0002": "Mumbai", "WH/0003": "Hyderabad", "WH/0004": "Bangalore", "WH/0005": "Gurgaon", "WH/0006": "Kolkata"}
D0 = M0

_, r = run("""
select e."Posting_Date" d, e."Transfer_to" wh, e."Item_No" item, max(e."Item_Description") descr, -sum(e."Quantity") units,
       max(c.carton_size) carton, count(distinct e."Document_No") docs, string_agg(distinct e."Document_No", ',') doc_list
  from bc_item_ledger_entries e
  left join wip.ref_carton_size c on c.item_no = e."Item_No" and c.carton_size > 0
 where e."Document_Type" = 'Transfer Shipment' and e."Location_Code" = 'PL/0003'
   and e."Posting_Date" between %s and %s
   and e."Transfer_to" in ('WH/0002','WH/0003','WH/0004','WH/0005','WH/0006')
   and (e."Item_No" like 'FG/%%' or e."Item_No" like 'TG/%%')
 group by 1, 2, 3
having sum(e."Quantity") <> 0""", (D0 - dt.timedelta(days=2), D1 + dt.timedelta(days=2)), limit=200000)
sk = pd.DataFrame(r, columns=["d", "wh", "item", "descr", "units", "carton", "docs", "doc_list"])
sk["units"] = sk.units.astype(float)
sk["carton"] = sk.carton.astype(float)
sk["lane"] = sk.wh.map(WH)
sk["cartons"] = sk.units / sk.carton
erp = sk.groupby(["lane", "d"]).agg(our_cartons=("cartons", "sum"), units=("units", "sum"),
                                     units_no_size=("units", lambda s: s[sk.loc[s.index, "carton"].isna()].sum()),
                                     skus=("item", "nunique"), shipments=("doc_list", lambda s: ",".join(sorted(set(",".join(s).split(",")))))).reset_index()

t = pd.read_pickle(W + "icl2_t.pkl")
t = t[(t.dispatch >= D0) & (t.dispatch <= D1)].copy()
t["good"] = t.boxes.notna() & ~t.bad_boxes
log = t.groupby(["lane", "dispatch"]).agg(trucks=("lr", "size"), lrs=("lr", lambda s: ", ".join(str(x) for x in s)),
                                         carriers=("carrier", lambda s: ", ".join(sorted(set(s)))),
                                         log_boxes=("cartons", lambda s: s[t.loc[s.index, "good"]].sum()),   # the count, not the volume (215)
                                         missing_box_rows=("good", lambda s: int((~s).sum()))).reset_index().rename(columns={"dispatch": "d"})

days = log.merge(erp, on=["lane", "d"], how="outer")
days = days[(days.d >= D0) & (days.d <= D1)].copy()
days["paired"] = ""
# pair a log day without ERP with an ERP day (+/- 1) that has no truck logged
for ln in days.lane.unique():
    g = days[days.lane == ln]
    for i in g[g.trucks.notna() & g.our_cartons.isna()].index:
        d = days.at[i, "d"]
        for off in (-1, 1):
            j = days[(days.lane == ln) & (days.d == d + dt.timedelta(days=off)) & days.trucks.isna() & days.our_cartons.notna() & (days.paired == "")].index
            if len(j):
                j = j[0]
                for c in ("our_cartons", "units", "units_no_size", "skus", "shipments"):
                    days.at[i, c] = days.at[j, c]
                days.at[i, "paired"] = f"ERP posted {days.at[j, 'd'].strftime('%d %b')}"
                days.at[j, "paired"] = "moved"
                sk.loc[(sk.lane == ln) & (sk.d == days.at[j, "d"]), "d_paired"] = d
                break
days = days[days.paired != "moved"].copy()
sk["d_key"] = sk.get("d_paired", pd.Series(index=sk.index, dtype=object)).where(sk.get("d_paired", pd.Series(index=sk.index)).notna(), sk.d)
days["diff"] = days.our_cartons - days.log_boxes
days["ratio"] = days.our_cartons / days.log_boxes


def status(x):
    if pd.isna(x.trucks):
        return "ERP shipment, no truck logged"
    if pd.isna(x.our_cartons):
        return "Truck logged, no ERP shipment"
    if x.missing_box_rows and x.log_boxes == 0:
        return "No box count in the log"
    if x.missing_box_rows:
        return "Part of the day has no box count"
    if abs(x.ratio - 1) <= 0.10:
        return "Matches (within 10%)"
    return "ERP higher than the log" if x.ratio > 1 else "Log higher than the ERP"


days["status"] = days.apply(status, axis=1)
days["month"] = days.d.map(lambda d: d.replace(day=1))
days = days.sort_values(["d", "lane"])
comp = days[days.status.isin(["Matches (within 10%)", "ERP higher than the log", "Log higher than the ERP"])]
tot = dict(lane_days=int(len(days)), compared=int(len(comp)), match=int((comp.status == "Matches (within 10%)").sum()),
           log_boxes=float(comp.log_boxes.sum()), our_cartons=float(comp.our_cartons.sum()),
           median_ratio=round(float(comp.ratio.median()), 3),
           erp_only=int((days.status == "ERP shipment, no truck logged").sum()), erp_only_cartons=float(days.loc[days.status == "ERP shipment, no truck logged", "our_cartons"].sum()),
           log_only=int((days.status == "Truck logged, no ERP shipment").sum()),
           units_no_size=float(sk.loc[sk.carton.isna(), "units"].sum()), units=float(sk.units.sum()),
           no_size_items=sorted(sk.loc[sk.carton.isna(), "item"].unique().tolist()))
print(json.dumps(tot, indent=1))
print(days.status.value_counts().to_string())
print(comp.assign(r=comp.ratio).groupby("lane").r.describe()[["count", "25%", "50%", "75%"]].round(2))
f = lambda v: None if v is None or pd.isna(v) else float(v)
out = dict(totals=tot,
           days=[dict(date=x.d.strftime("%Y-%m-%d"), month=x.month.strftime("%Y-%m-%d"), lane=x.lane,
                      trucks=None if pd.isna(x.trucks) else int(x.trucks), lrs=x.lrs if isinstance(x.lrs, str) else "",
                      carriers=x.carriers if isinstance(x.carriers, str) else "", log_boxes=f(x.log_boxes) if not pd.isna(x.trucks) else None,
                      our_cartons=None if pd.isna(x.our_cartons) else round(float(x.our_cartons), 1), units=f(x.units),
                      units_no_size=f(x.units_no_size), skus=None if pd.isna(x.skus) else int(x.skus),
                      shipments=x.shipments if isinstance(x.shipments, str) else "", paired=x.paired, status=x.status)
                 for x in days.itertuples()],
           skus=[dict(date=x.d_key.strftime("%Y-%m-%d"), lane=x.lane, item=x.item, descr=(x.descr or "").title(), units=float(x.units),
                      carton=f(x.carton), cartons=None if pd.isna(x.cartons) else round(float(x.cartons), 2))
                 for x in sk[(sk.d_key >= D0) & (sk.d_key <= D1)].itertuples()])
json.dump(out, open(W + "icl_carton_cmp.json", "w", encoding="utf-8"), separators=(",", ":"), allow_nan=False)
print("days", len(out["days"]), "sku lines", len(out["skus"]))
