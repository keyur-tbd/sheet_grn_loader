"""Truck-by-truck table for the Freight review, with the action each truck calls for -> work/icl_trips.json.

Universe: every truck the warehouses logged in the period (icl_env.py) plus every billed LR the logs missed. Boxes come from
the log, else from the ERP (transfer units / the logistics carton size) on the LR date when that day carries no
logged truck on the lane. Same rules as the levers (icl_analyse2.py / icl_levers_ext.py):
  right-size  = cheapest truck priced on that lane that month that holds the boxes
  combine     = the next truck on the lane within 2 days, same month, both loads <= 95% of a 32 ft; saving net of
                right-sizing both
  overbilled  = billed amount above the card for the billed size
Run after icl_levers_ext.py; publish_review.py adds the result to the snapshot.
"""
import sys
import json
import datetime as dt
import pandas as pd

from dbq import run

from icl_env import W, M0, M1
t = pd.read_pickle(W + "icl2_t.pkl")      # logged trucks, carrier+LR joined to bills, deduped
b = pd.read_pickle(W + "icl2_b.pkl")      # bill lines of the period with the card for the billed size
rates = pd.read_pickle(W + "icl_rates.pkl")
rate_map = {(r.month, r.lane, r.size): r.rate for r in rates.itertuples()}
from cards import card_rate, card_source, sizes_priced
import volume
CAP = volume.CAP
WH = volume.WH

# boxes = normal-carton equivalents by volume (215): logged trucks via icl2_t, ERP-boxed trucks from the day's load
day = volume.erp_days().rename(columns={"load": "boxes"})
erp_day = {(x.lane, x.d): x.boxes for x in day.itertuples()}

trips = []
lt = t[(t.month >= M0) & (t.month <= M1)]
busy = set(zip(lt.lane, lt.dispatch))
for x in lt.itertuples():
    good = pd.notna(x.boxes) and not x.bad_boxes
    trips.append(dict(date=x.dispatch, month=x.month, lane=x.lane, carrier=x.carrier, lr=str(x.lr), size=None if pd.isna(x.size) else int(x.size),
                      bill_size=None if pd.isna(x.bill_size) else int(x.bill_size), boxes=float(x.boxes) if good else None,
                      box_src="log" if good else None, cartons=float(x.cartons) if good else None, billed=None if pd.isna(x.billed) else float(x.billed), in_log=True))
logged = set(zip(t.carrier, t.lane, t.lr))
for x in b.itertuples():
    if (x.carrier, x.lane, x.lr) in logged:
        continue
    d = x.lr_date if (x.lr_date is not None and not pd.isna(x.lr_date)) else None
    bx = None
    if d is not None:
        for off in (0, -1, 1):
            k = (x.lane, d + dt.timedelta(days=off))
            if k in erp_day:
                if (x.lane, d) not in busy:
                    bx = erp_day[k]
                break
    trips.append(dict(date=d or x.month, month=x.month, lane=x.lane, carrier=x.carrier, lr=str(x.lr), size=int(x.bill_size),
                      bill_size=int(x.bill_size), boxes=bx, box_src="erp" if bx is not None else None, cartons=None, billed=float(x.billed), in_log=False))
T = pd.DataFrame(trips).sort_values(["lane", "date"]).reset_index(drop=True)
T["cap"] = T["size"].map(CAP)
T["card"] = [card_rate(x.month, x.lane, x.size, x.carrier) for x in T.itertuples()]
T["card_src"] = [card_source(x.month, x.lane, x.size, x.carrier) for x in T.itertuples()]
T["card_billed_size"] = [card_rate(x.month, x.lane, x.bill_size, x.carrier) if x.bill_size and not pd.isna(x.bill_size) else None for x in T.itertuples()]
T["fill"] = T.boxes / T.cap
T["over_card"] = (T.billed - T.card_billed_size).where(T.billed.notna() & T.card_billed_size.notna())


def fit(x):
    if pd.isna(x.boxes) or x.card is None or pd.isna(x.card):
        return (None, None)
    opts = [(rt, s) for s, rt in sizes_priced(x.month, x.lane, x.carrier).items() if CAP[s] >= x.boxes]
    return min(opts) if opts else (None, None)


T[["fit_rate", "fit_size"]] = T.apply(lambda x: pd.Series(fit(x)), axis=1)
T["rs_save"] = (T.card - T.fit_rate).clip(lower=0).where(T.fit_rate.notna(), 0).fillna(0)
T["merge_with"] = None
T["merge_save"] = 0.0
T["merge_role"] = None
for ln, g in T[T.boxes.notna() & T.card.notna()].sort_values("date").groupby("lane"):
    idx = list(g.index)
    i = 0
    while i < len(idx) - 1:
        a, c = T.loc[idx[i]], T.loc[idx[i + 1]]
        r32 = card_rate(a.month, ln, 32, a.carrier)
        if (c.date - a.date).days <= 2 and a.month == c.month and r32 and a.boxes + c.boxes <= CAP[32] * .95:
            save = a.card + c.card - r32 - a.rs_save - c.rs_save
            T.at[idx[i], "merge_with"], T.at[idx[i], "merge_save"], T.at[idx[i], "merge_role"] = c.date, save, "first"
            T.at[idx[i + 1], "merge_with"], T.at[idx[i + 1], "merge_role"] = a.date, "second"
            i += 2
        else:
            i += 1

fmt = lambda v: "₹" + f"{v:,.0f}"
ival = lambda v: None if v is None or pd.isna(v) else int(v)          # NaN never reaches the JSON
fval = lambda v: None if v is None or pd.isna(v) else float(v)
dd = lambda d: d.strftime("%d %b")
rows = []
for x in T.itertuples():
    acts = []
    if pd.notna(x.over_card) and x.over_card > 0:
        acts.append(dict(type="recover", save=float(x.over_card), text=f"Billed {fmt(x.over_card)} above the card: recover from {x.carrier}"))
    if ival(x.bill_size) and ival(x.size) and ival(x.bill_size) != ival(x.size) and x.in_log:
        acts.append(dict(type="size", save=0.0, text=f"Billed as {ival(x.bill_size)} ft, logged as {ival(x.size)} ft: check the LR"))
    if x.merge_role == "first":
        acts.append(dict(type="combine", save=float(x.merge_save),
                         text=f"Combine with the {dd(x.merge_with)} truck: both loads fit one 32 ft, save {fmt(x.merge_save)}"))
    if x.merge_role == "second":
        acts.append(dict(type="combine", save=0.0, text=f"Could have gone with the {dd(x.merge_with)} truck (saving shown there)"))
    if x.rs_save > 0:
        acts.append(dict(type="rightsize", save=float(x.rs_save),
                         text=f"A {int(x.fit_size)} ft holds this load ({x.boxes:,.0f} normal cartons by volume): save {fmt(x.rs_save)}"))
    if pd.notna(x.fill) and x.fill < .6 and x.rs_save == 0 and x.merge_role is None:
        acts.append(dict(type="lowfill", save=0.0, text=f"{x.fill * 100:.0f}% full and no smaller truck priced: hold for the next load"))
    if ival(x.size) is None:
        acts.append(dict(type="data", save=0.0, text="No truck size in the warehouse log: fill it in"))
    if x.boxes is None or pd.isna(x.boxes):
        acts.append(dict(type="data", save=0.0, text="No box count: fill it in the warehouse log" if x.in_log else "Not in the dispatch log and no ERP match: log this truck"))
    elif x.box_src == "erp":
        acts.append(dict(type="data", save=0.0, text="Not in the dispatch log (boxes from the ERP): log this truck"))
    if x.billed is None or pd.isna(x.billed):
        acts.append(dict(type="data", save=0.0, text="No bill line found for this LR"))
    rows.append(dict(date=x.date.strftime("%Y-%m-%d"), month=x.month.strftime("%Y-%m-%d"), lane=x.lane, carrier=x.carrier, lr=x.lr,
                     size=ival(x.size), bill_size=ival(x.bill_size), boxes=None if pd.isna(x.boxes) else round(float(x.boxes)),
                     box_src=x.box_src, cartons=None if pd.isna(x.cartons) else round(float(x.cartons)), cap=None if pd.isna(x.cap) else int(x.cap), fill=None if pd.isna(x.fill) else round(float(x.fill) * 100, 1),
                     card=fval(x.card), card_src=x.card_src, billed=fval(x.billed),
                     avoidable=round(sum(a["save"] for a in acts), 0), actions=acts))
json.dump(rows, open(W + "icl_trips.json", "w", encoding="utf-8"), separators=(",", ":"), allow_nan=False)
A = pd.DataFrame(rows)
print("trucks", len(A), "| with boxes", A.boxes.notna().sum(), "| with an action", (A.actions.str.len() > 0).sum(),
      "| avoidable Rs", A.avoidable.sum())
types = pd.Series([a["type"] for acts in A.actions for a in acts]).value_counts().to_dict()
print("actions by type", types)
print("by month", A.groupby("month").agg(trucks=("lane", "size"), avoidable=("avoidable", "sum")).to_dict("index"))
