"""Right-size and merge levers on the logged trucks PLUS the billed trucks the logs missed (boxes from the ERP via the
logistics team's carton sizes). Same rules as icl_analyse2.py; rewrites the two levers in icl_report_clean.json."""
import sys
import json
import datetime as dt
import pandas as pd

from dbq import run

from icl_env import W as SP, ANNUAL
u = pd.read_pickle(SP + "icl2_u.pkl")
b = pd.read_pickle(SP + "icl2_b.pkl")
t = pd.read_pickle(SP + "icl2_t.pkl")
rates = pd.read_pickle(SP + "icl_rates.pkl")
rate_map = {(r.month, r.lane, r.size): r.rate for r in rates.itertuples()}
from cards import card_rate, sizes_priced
import volume
CAP = volume.CAP
WH = volume.WH

# --- the added trucks, exactly as icl_cartons_update.py picked them; boxes = normal-carton equivalents (215) ---
day = volume.erp_days().rename(columns={"load": "boxes"})
logged = set(zip(t.carrier, t.lane, t.lr))
miss = b[[(c, l, x) not in logged for c, l, x in zip(b.carrier, b.lane, b.lr)]].copy()
busy = set(zip(u.lane, u.dispatch))
rows = []
for r in miss.itertuples():
    if r.lr_date is None or pd.isna(r.lr_date):
        continue
    for off in (0, -1, 1):
        d = r.lr_date + dt.timedelta(days=off)
        hit = day[(day.lane == r.lane) & (day.d == d)]
        if len(hit):
            if (r.lane, r.lr_date) not in busy:
                rows.append(dict(lane=r.lane, carrier=r.carrier, dispatch=r.lr_date, month=r.month, size=int(r.bill_size), boxes=float(hit.boxes.iloc[0]),
                                 source="ERP boxes (not in the log)"))
            break
add = pd.DataFrame(rows)
add["rate"] = [card_rate(x.month, x.lane, x["size"], x.carrier) for _, x in add.iterrows()]
add["cap"] = add["size"].map(CAP)
print("added trucks:", len(add), add.groupby(["lane", "month"]).size().to_dict())

base = u[["lane", "carrier", "dispatch", "month", "size", "boxes", "rate", "cap"]].assign(source="dispatch log")
allt = pd.concat([base, add[base.columns]], ignore_index=True)
allt["util"] = allt.boxes / allt.cap


def fit(r):
    opts = [(rt, s) for s, rt in sizes_priced(r.month, r.lane, r.carrier).items() if CAP[s] >= r.boxes]
    return pd.Series(min(opts) if opts else (r.rate, r["size"]))


allt[["fit_rate", "fit_size"]] = allt.apply(fit, axis=1)
allt["rs_saving"] = (allt.rate - allt.fit_rate).clip(lower=0)
mg = []
for ln, g in allt.sort_values("dispatch").groupby("lane"):
    g = g.reset_index(drop=True)
    i = 0
    while i < len(g) - 1:
        a, c = g.iloc[i], g.iloc[i + 1]
        r32 = card_rate(a.month, ln, 32, a.carrier)
        if (c.dispatch - a.dispatch).days <= 2 and a.month == c.month and r32 and a.boxes + c.boxes <= CAP[32] * .95:
            mg.append(dict(lane=ln, d1=a.dispatch, d2=c.dispatch, boxes=a.boxes + c.boxes, was=a.rate + c.rate, now=r32,
                           saving=a.rate + c.rate - r32 - max(0, a.rs_saving) - max(0, c.rs_saving), month=a.month))
            i += 2
        else:
            i += 1
mg = pd.DataFrame(mg)
rs_old, mg_old = u.rs_saving.sum(), pd.read_pickle(SP + "icl2_mg.pkl").saving.sum()
rs_new, mg_new = allt.rs_saving.sum(), mg.saving.sum()
print(f"right-size: {rs_old:,.0f} -> {rs_new:,.0f} ({(allt.rs_saving > 0).sum()} of {len(allt)} trucks)")
print(f"merge:      {mg_old:,.0f} -> {mg_new:,.0f} ({len(mg)} pairs)")
ga = allt[(allt.lane == "Gurgaon") & (allt.month == dt.date(2026, 4, 1))]
print("Gurgaon April now:", len(ga), "trucks, fill", round(ga.boxes.sum() / ga.cap.sum() * 100, 1), "| rs", ga.rs_saving.sum(),
      "| merges", len(mg[(mg.lane == "Gurgaon") & (mg.month == dt.date(2026, 4, 1))]) if len(mg) else 0)
print(add[["lane", "dispatch", "size", "boxes", "rate"]].assign(fill=lambda d: (d.boxes / d["size"].map(CAP) * 100).round(0)).to_string())

# --- write into the report data ---
D = json.load(open(SP + "icl_report_clean.json", encoding="utf-8"))
L = lambda v: round(float(v) / 1e5, 2)
for lv in D["levers"]:
    if lv["key"] == "rightsize":
        lv.update(six=L(rs_new), year=L(rs_new * ANNUAL),
                  basis=f"{int((allt.rs_saving > 0).sum())} of {len(allt)} trucks were bigger than the load needed (incl. {len(add)} billed trucks the logs missed, boxed from the ERP)")
    if lv["key"] == "merge":
        lv.update(six=L(mg_new), year=L(mg_new * ANNUAL),
                  basis=f"{len(mg)} pairs fit one 32 ft truck at 95% or less; net of right-sizing; incl. Gurgaon's April trucks")
D["levers_ext"] = dict(added=int(len(add)), trucks=int(len(allt)), rs_old=L(rs_old), mg_old=L(mg_old))
json.dump(D, open(SP + "icl_report_clean.json", "w", encoding="utf-8"), separators=(",", ":"))
allt.to_pickle(SP + "icl3_allt.pkl"); mg.to_pickle(SP + "icl3_mg.pkl")
print("levers now:", [(l["key"], l["year"]) for l in D["levers"]], "total", round(sum(l["year"] or 0 for l in D["levers"]), 2))
