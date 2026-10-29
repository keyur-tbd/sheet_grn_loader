"""Finance view of intercity freight, April 2026 to the last complete month (v2: carrier-aware joins, cleaned log).

A. Rate check from the BILL side: every billed LR vs the rate card for its month, lane and billed size;
   then billed size vs the size the warehouse logged for the same truck.
B. Counts and totals by month: MIS trips/rupees, bill lines, G/L, rate card.
C. Utilisation on the cleaned dispatch log, and the levers: right-size, merge, vendor.
Writes icl2_*.pkl for the report.
"""
import sys
import datetime as dt
import pandas as pd

from dbq import run

from icl_env import W as SP, M0, M1, NEXT, MONTHS, JULY
L = {k: pd.read_pickle(SP + f"icl_{k}.pkl") for k in ("trips", "lr", "rates", "quotes", "current", "mis")}
trips, lr, rates, quotes, current, mis = (L[k] for k in ("trips", "lr", "rates", "quotes", "current", "mis"))
import volume   # truck capacity in normal cartons; a load's volume in normal-carton equivalents (215)
CAP = volume.CAP
pd.set_option("display.width", 220); pd.set_option("display.max_columns", 30)
rate_map = {(r.month, r.lane, r.size): r.rate for r in rates.itertuples()}   # lane block rates: diagnostics only
from cards import card_rate, sizes_priced, fix_day_month   # the July card from July, each carrier's own rate before


def carrier(s):
    s = str(s).upper()
    for k, v in (("RADHE", "Radhe Radhe"), ("VIHIRA", "Vihira"), ("META", "Meta Road"), ("DELHI", "Delhi Bangalore Roadlines"),
                 ("DBR", "Delhi Bangalore Roadlines"), ("R.R", "R R Logistics"), ("RR", "R R Logistics"), ("R R", "R R Logistics")):
        if k in s:
            return v
    return s.title()


CITY = {"MUMBAI": "Mumbai", "BLR": "Bangalore", "BANGALORE": "Bangalore", "HYD": "Hyderabad", "HYDERABAD": "Hyderabad",
        "KOL": "Kolkata", "KOLKATA": "Kolkata", "DELHI": "Gurgaon", "GURGAON": "Gurgaon"}
MON = {"apr": 4, "april": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
       "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12, "january": 1, "jan": 1,
       "february": 2, "feb": 2, "march": 3, "mar": 3}

# ---------------- bills (LR list) ----------------
b = lr.copy()
b["lane"] = b.city.map(CITY)
b["carrier"] = b.transporter.map(carrier)
b["month"] = b.month_txt.str.lower().map(MON)
b = b[b.month.notna() & b.lane.notna()].copy()
b["month"] = b.month.astype(int).map(lambda m: dt.date(M0.year if m >= 4 else M0.year + 1, m, 1))
b = b.sort_values("src").drop_duplicates(subset=["carrier", "lane", "lr"], keep="first")
b = b[(b.month >= M0) & (b.month <= M1)].copy()
# LR dates typed day-first but read month-first (8 Apr stored as 4 Aug): put them back in the bill month
b["lr_date"] = [fix_day_month(d, m) for d, m in zip(b.lr_date, b.month)]
b["card"] = [card_rate(r.month, r.lane, r.bill_size, r.carrier) for r in b.itertuples()]
b["over_card"] = b.billed - b.card

print("=== A1. Every billed LR vs the rate card (month, lane, billed size)")
print("bill lines", len(b), "| no card for size:", b.card.isna().sum(), "| billed", b.billed.sum(), "| at card", b.card.sum())
off = b[b.over_card.abs() > 0]
print(off[["carrier", "lane", "bill_size", "lr", "month", "billed", "card", "over_card"]].sort_values("over_card").to_string())
print(b.groupby("month")[["billed", "card", "over_card"]].sum())

# ---------------- dispatch log, cleaned ----------------
t = trips[(trips.dispatch >= M0) & (trips.dispatch < NEXT)].copy()
t["carrier"] = t.transporter.map(carrier)
t["bad_boxes"] = (t.boxes > 3000) | t.boxes.isna()
t = t.merge(b[["carrier", "lane", "lr", "billed", "bill_size", "month", "card"]].rename(columns={"month": "bmonth"}),
            on=["carrier", "lane", "lr"], how="left")
# a truck logged twice under one LR: keep the row whose size agrees with the bill, else the first
t["agree"] = (t["size"] == t.bill_size).astype(int)
t = t.sort_values(["agree", "dispatch"], ascending=[False, True])
dups = t.duplicated(subset=["carrier", "lane", "lr"], keep="first")
print("\nlog: rows", len(t), "| duplicate LR rows dropped", dups.sum(), "| bad box counts", t.bad_boxes.sum())
t = t[~dups].copy()
t["dispatch"] = [fix_day_month(d, m) if m is not None and not pd.isna(m) else d for d, m in zip(t.dispatch, t.bmonth)]
t["month"] = t.bmonth.where(t.bmonth.notna(), t.dispatch.map(lambda d: d.replace(day=1)))   # the bill's month wins
t["rate"] = [card_rate(r.month, r.lane, r["size"], r.carrier) for _, r in t.iterrows()]
# by volume (215): cartons = the boxes the warehouse logged; boxes = their volume in normal cartons (that day's ERP mix)
t["cartons"] = t.boxes
t["boxes"] = t.cartons * [volume.mix(ln, d) for ln, d in zip(t.lane, t.dispatch)]

print("\n=== A2. Billed size vs logged size (same carrier + LR)")
sm = t[t.bill_size.notna() & t["size"].notna() & (t.bill_size != t["size"])]
sm = sm.assign(card_logged=[card_rate(r.month, r.lane, r["size"], r.carrier) for _, r in sm.iterrows()])
sm["overbilled"] = sm.billed - sm.card_logged
print(sm[["carrier", "lane", "lr", "dispatch", "size", "bill_size", "boxes", "billed", "card_logged", "overbilled"]].to_string())

# ---------------- B. counts and money by month ----------------
_, gl = run("""select service_month, count(*) filter (where amount > 0), sum(amount) from wip.intercity_freight_bills
                where service_month between %s and %s group by 1""", (M0, M1))
gl = pd.DataFrame(gl, columns=["month", "gl_lines", "gl"]).set_index("month").astype(float)
mm = mis[~mis.lane.str.startswith("_")]
B = pd.DataFrame(index=MONTHS)
B["mis_trips"] = mis[mis.lane == "_trips"].set_index("month").mis
B["bill_lines"] = b.groupby("month").size()
B["gl_lines"] = gl.gl_lines
B["logged"] = t.groupby("month").size()
B["mis_trunk"] = mm[mm.lane != "Harry"].groupby("month").mis.sum()
B["bills"] = b.groupby("month").billed.sum()
B["bills_at_card"] = b.groupby("month").card.sum()
B["gl"] = gl.gl
B["mis_harry"] = mm[mm.lane == "Harry"].groupby("month").mis.sum()
B["mis_total"] = B.mis_trunk + B.mis_harry.fillna(0)
print("\n=== B. Month: trips and rupees on every side")
print(B.fillna(0).round(0))
BL = b.groupby(["lane", "month"]).agg(bill_lines=("billed", "size"), bills=("billed", "sum")).reset_index().merge(
    mm.rename(columns={"mis": "mis"}), on=["lane", "month"], how="outer")
print(BL.assign(mis_minus_bills=BL.mis - BL.bills).pivot(index="lane", columns="month", values="mis_minus_bills").round(0))

# ---------------- C. utilisation ----------------
u = t[~t.bad_boxes & t.rate.notna() & t["size"].isin(list(CAP))].copy()
u["cap"] = u["size"].map(CAP)
u["util"] = u.boxes / u.cap
u["empty_cost"] = u.rate * (1 - u.util.clip(upper=1))
print("\n=== C1. Utilisation: trips", len(u), "boxes", u.boxes.sum(), "cost", u.rate.sum(), "fill", round(u.boxes.sum() / u.cap.sum(), 3),
      "| empty-space rupees", round(u.empty_cost.sum()))
print(u.groupby(["lane", "size"]).agg(trips=("util", "size"), avg_boxes=("boxes", "mean"), cap=("cap", "first"), fill=("util", "mean"),
      under60=("util", lambda s: (s < .6).sum()), cost=("rate", "sum"), empty=("empty_cost", "sum")).round(2))
u["band"] = pd.cut(u.util, [0, .4, .6, .8, 1.0, 9], labels=["<40%", "40-60%", "60-80%", "80-100%", ">100%"])
print(u.groupby("band", observed=False).agg(trips=("util", "size"), cost=("rate", "sum"), empty=("empty_cost", "sum")))
PB = u.groupby("lane").agg(trips=("rate", "size"), boxes=("boxes", "sum"), cost=("rate", "sum"))
PB["per_box"] = PB.cost / PB.boxes
best32 = {ln: card_rate(M1, ln, 32) / CAP[32] for ln in PB.index}
PB["full_32_per_box"] = pd.Series(best32)
print(PB.round(1))

# C2. right-size: cheapest size priced on that lane that month that holds the load
def fit(r):
    opts = [(rt, s) for s, rt in sizes_priced(r.month, r.lane, r.carrier).items() if CAP[s] >= r.boxes]
    return pd.Series(min(opts) if opts else (r.rate, r["size"]))
u[["fit_rate", "fit_size"]] = u.apply(fit, axis=1)
u["rs_saving"] = (u.rate - u.fit_rate).clip(lower=0)
print("\n=== C2. Right-size:", u.rs_saving.sum(), "on", (u.rs_saving > 0).sum(), "trips")
print(u[u.rs_saving > 0].groupby(["lane", "size", "fit_size"]).agg(trips=("boxes", "size"), avg_boxes=("boxes", "mean"),
      saving=("rs_saving", "sum")).round(0))

# C3. merge: next dispatch on the lane within 2 days, same month, both loads fit one 32' at <=95%
rows = []
for ln, g in u.sort_values("dispatch").groupby("lane"):
    g = g.reset_index(drop=True)
    i = 0
    while i < len(g) - 1:
        a, c = g.iloc[i], g.iloc[i + 1]
        r32 = card_rate(a.month, ln, 32, a.carrier)
        if (c.dispatch - a.dispatch).days <= 2 and a.month == c.month and r32 and a.boxes + c.boxes <= CAP[32] * .95:
            rows.append(dict(lane=ln, d1=a.dispatch, d2=c.dispatch, b1=a.boxes, b2=c.boxes, was=a.rate + c.rate, now=r32,
                             saving=a.rate + c.rate - r32 - max(0, a.rs_saving) - max(0, c.rs_saving)))
            i += 2
        else:
            i += 1
mg = pd.DataFrame(rows)
print("\n=== C3. Merge (net of what right-sizing already saves on those trucks)")
print(mg.groupby("lane").agg(pairs=("saving", "size"), saving=("saving", "sum")).round(0), "total", mg.saving.sum())

# C4. vendor: Jul-Sep trips at the lowest of the four quotes, same size
qbest = quotes.groupby(["lane", "size"]).rate.min().to_dict()
qwho = quotes.sort_values("rate").drop_duplicates(["lane", "size"]).set_index(["lane", "size"]).vendor.to_dict()
js = u[u.month >= JULY].copy()
js["best"] = [qbest.get((r.lane, r["size"])) for _, r in js.iterrows()]
js["v_saving"] = (js.rate - js.best).clip(lower=0)
print("\n=== C4. Vendor: Jul-Sep at the lowest quote")
print(js.groupby("lane").agg(trips=("rate", "size"), paid=("rate", "sum"), saving=("v_saving", "sum")))
print({k: v for k, v in qwho.items()})
oct_blr = {s: rate_map.get((NEXT, "Bangalore", s)) for s in (22, 32)}
print("Oct Bangalore card", oct_blr, "vs Sep", {s: rate_map.get((M1, "Bangalore", s)) for s in (22, 32)})
blr_sep = u[(u.lane == "Bangalore") & (u.month == M1)]
print("Bangalore Sep trips by size", blr_sep["size"].value_counts().to_dict(), "-> monthly uplift at Oct card",
      sum((oct_blr.get(s) or 0) - rate_map.get((M1, "Bangalore", s), 0) for s in blr_sep["size"]))

for name, df in [("b", b), ("t", t), ("u", u), ("mg", mg), ("js", js), ("months", B.reset_index().rename(columns={"index": "month"})),
                 ("BL", BL), ("sm", sm), ("PB", PB.reset_index())]:
    df.to_pickle(SP + f"icl2_{name}.pkl")
