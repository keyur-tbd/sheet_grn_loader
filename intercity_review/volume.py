"""Volumetric truck load for the Freight review (owner, 2026-10-27; migration 215).

A truck holds a VOLUME, not a carton count. Capacity per truck size = wip.ref_truck_capacity (the tracker's FY'27
"Carton Capacity", read as normal cartons), so its volume = that count x the normal carton (445 x 220 x 195 mm =
19.09 L). A load's volume = its cartons x each carton's own size relative to a normal one (wip.carton_weight():
Donut / Love Cake 2.4875 from their 640 x 280 x 265 mm carton, Protein Chips 4.8608 from the 660 x 380 x 370 mm Harry Chips
carton (216), the rest 1).
Fill = load volume / truck volume, i.e. normal-carton equivalents / capacity.

The dispatch logs count boxes with no product mix, so a logged truck's load = its boxes x the mix of the ERP
transfer shipments on that lane that day (load / cartons; the day before or after when the day has none, else the
lane's month, else 1). The ERP is the only place the product mix is known.

  CAP, NORMAL_L       capacity in normal cartons per truck size, normal carton litres
  erp_days()          lane x posting day: cartons (count) and load (normal-carton equivalents)
  mix(lane, day)      load / cartons for a logged truck
"""
import datetime as dt
import sys

import pandas as pd

from dbq import run
from icl_env import M0, D1

WH = {"WH/0002": "Mumbai", "WH/0003": "Hyderabad", "WH/0004": "Bangalore", "WH/0005": "Gurgaon", "WH/0006": "Kolkata"}

_, _r = run("select size_ft, normal_cartons from wip.ref_truck_capacity")
CAP = {int(s): int(c) for s, c in _r}
_, _r = run("select volume_l from wip.ref_carton_volume where carton = 'normal'")
NORMAL_L = float(_r[0][0])


def erp_days():
    _, r = run("""
select e."Posting_Date" d, e."Transfer_to" wh, sum(-e."Quantity" / c.carton_size) cartons,
       sum(-e."Quantity" / c.carton_size * wip.carton_weight(e."Item_Description")) load
  from bc_item_ledger_entries e
  -- the logistics sheet's carton, Uniware's where the sheet has none (as wip.intercity_freight_sku_live does)
  join lateral (select coalesce((select nullif(s.carton_size, 0) from wip.ref_carton_size s where s.item_no = e."Item_No"),
                                (select nullif(u.carton_size, 0)::numeric from public.uw_item_type u where u.sku_code = e."Item_No")) as carton_size) c
    on c.carton_size is not null
 where e."Document_Type" = 'Transfer Shipment' and e."Location_Code" = 'PL/0003'
   and e."Posting_Date" between %s::date - 7 and %s
   and e."Transfer_to" in ('WH/0002','WH/0003','WH/0004','WH/0005','WH/0006')
   and (e."Item_No" like 'FG/%%' or e."Item_No" like 'TG/%%')
 group by 1, 2""", (M0, D1), limit=100000)
    day = pd.DataFrame(r, columns=["d", "wh", "cartons", "load"])
    day[["cartons", "load"]] = day[["cartons", "load"]].astype(float)
    day["lane"] = day.wh.map(WH)
    return day


_day = erp_days()
_by_day = {(x.lane, x.d): (x.cartons, x.load) for x in _day.itertuples() if x.cartons > 0}
_m = _day.assign(m=_day.d.map(lambda d: d.replace(day=1))).groupby(["lane", "m"])[["cartons", "load"]].sum()
_by_month = {k: v.load / v.cartons for k, v in _m.iterrows() if v.cartons > 0}


def mix(lane, day):
    """Normal-carton equivalents per logged box on that lane and day."""
    if day is None or pd.isna(day):
        return 1.0
    for off in (0, -1, 1):
        hit = _by_day.get((lane, day + dt.timedelta(days=off)))
        if hit:
            return hit[1] / hit[0]
    return _by_month.get((lane, day.replace(day=1)), 1.0)
