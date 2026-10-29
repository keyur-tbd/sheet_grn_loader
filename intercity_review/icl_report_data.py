"""Final figures for the intercity freight report -> icl_report.json, plus the Excel backup."""
import json
import datetime as dt
import pandas as pd

from icl_env import W as SP, M1, NEXT, ANNUAL, N_CARD, JULY, EXCEL
R = {k: pd.read_pickle(SP + f"icl2_{k}.pkl") for k in ("b", "t", "u", "mg", "js", "months", "BL", "PB")}
rates = pd.read_pickle(SP + "icl_rates.pkl")
quotes = pd.read_pickle(SP + "icl_quotes.pkl")
b, u, mg, js, B, BL, PB = R["b"], R["u"], R["mg"], R["js"], R["months"], R["BL"], R["PB"]
from volume import CAP, NORMAL_L   # boxes below = normal-carton equivalents by volume (215)
rate_map = {(r.month, r.lane, r.size): r.rate for r in rates.itertuples()}   # lane block rates
from cards import card_rate, AWARDED_VENDOR
L = 1e5
lk = lambda v: round(float(v) / L, 2)

out = {}
spend = b.billed.sum()
out["kpi"] = dict(spend_l=lk(spend), trucks=int(len(b)), fill=round(u.boxes.sum() / u.cap.sum() * 100, 1),
                  empty_l=lk(u.empty_cost.sum()), logged=int(len(u)), boxes=int(u.cartons.sum()),
                  load=int(u.boxes.sum()), load_l=round(float(u.boxes.sum() * NORMAL_L)), cap_l=round(float(u.cap.sum() * NORMAL_L)),
                  over_card_l=lk(b.over_card.sum()), over_lines=int((b.over_card > 0).sum()), over_amt=int(b.over_card.clip(lower=0).sum()))

# levers
blr_sep = u[(u.lane == "Bangalore") & (u.month == M1)]
# the next month's block can price Bangalore above the July card on some sizes: at the last month's truck mix
OCT = NEXT
blr_uplift_month = sum(max(0, (rate_map.get((OCT, "Bangalore", int(s))) or 0) - card_rate(OCT, "Bangalore", int(s)))
                       for s in blr_sep["size"] if rate_map.get((OCT, "Bangalore", int(s))))
levers = [
    dict(key="rightsize", label="Send each load on the smallest truck that holds it", six=lk(u.rs_saving.sum()), year=lk(u.rs_saving.sum() * ANNUAL),
         basis=f"{int((u.rs_saving > 0).sum())} of {len(u)} trucks were bigger than the load needed"),
    dict(key="merge", label="Combine two part-loads dispatched within 2 days", six=lk(mg.saving.sum()), year=lk(mg.saving.sum() * ANNUAL),
         basis=f"{len(mg)} pairs fit one 32 ft truck at 95% or less; net of right-sizing"),
    dict(key="bangalore", label="Hold Bangalore at the July card", six=None, year=lk(blr_uplift_month * 12),
         basis=f"the {OCT:%B} block prices Bangalore 22 ft at ₹" + f"{rate_map.get((OCT, 'Bangalore', 22)) or 0:,.0f}"
               + " against the card's ₹" + f"{card_rate(OCT, 'Bangalore', 22):,.0f}" + f" (Meta); at {M1:%B}'s volume. {OCT:%B} is still being filled in"),
    dict(key="vendor", label="Award each lane and size to the lowest quote", six=None, year=lk(js.v_saving.sum() * 12 / N_CARD) if N_CARD else None,
         basis=f"₹{lk(js.v_saving.sum())} L in {JULY:%b}-{M1:%b} on the same trucks: the card is above the lowest quote on Mumbai 22 / 32 ft, Gurgaon 22 / 32 ft and Hyderabad"),
    dict(key="recover", label="Recover R R's May bills above the card", six=lk(b.over_card.clip(lower=0).sum()), year=None,
         basis=f"{int((b.over_card > 0).sum())} LRs billed above the card for their month and size"),
]
out["levers"] = [l for l in levers if (l["six"] or 0) > 0 or (l["year"] or 0) > 0]

# rate exceptions
ex = b[b.over_card != 0].sort_values("over_card", ascending=False)
out["exceptions"] = [dict(carrier=r.carrier, lane=r.lane, size=int(r.bill_size), lr=str(r.lr), month=r.month.strftime("%b"),
                          billed=int(r.billed), card=int(r.card), diff=int(r.over_card)) for r in ex.itertuples()]

# reconciliation by month
rec = []
for r in B.itertuples():
    rec.append(dict(month=r.month.strftime("%b"), mis_trips=int(r.mis_trips or 0), bill_lines=int(r.bill_lines or 0),
                    mis_trunk=lk(r.mis_trunk), bills=lk(r.bills), at_card=lk(r.bills_at_card), gl=lk(r.gl) if r.gl == r.gl and r.gl else None,
                    harry=lk(r.mis_harry or 0), mis_total=lk(r.mis_total)))
out["rec"] = rec
bl = BL[(BL.lane != "Harry")].copy()
bl["d"] = bl.mis - bl.bills
out["mis_vs_bills"] = [dict(lane=r.lane, month=r.month.strftime("%b"), mis=lk(r.mis), bills=lk(r.bills), diff=lk(r.d))
                       for r in bl[bl.d.abs() > 1000].itertuples()]

# utilisation
out["bands"] = [dict(band=str(k), trips=int(v.trips), cost_l=lk(v.cost), empty_l=lk(v.empty_cost))
                for k, v in u.groupby("band", observed=False).agg(trips=("util", "size"), cost=("rate", "sum"), empty_cost=("empty_cost", "sum")).iterrows()]
lanes = []
for ln, g in u.groupby("lane"):
    r32 = card_rate(M1, ln, 32)
    lanes.append(dict(lane=ln, trucks=int(len(g)), boxes=int(g.boxes.sum()), fill=round(g.boxes.sum() / g.cap.sum() * 100, 1),
                      under60=int((g.util < .6).sum()), cost_l=lk(g.rate.sum()), per_box=round(g.rate.sum() / g.boxes.sum(), 1),
                      full32_per_box=round(r32 / CAP[32], 1), avg_boxes=int(g.boxes.mean())))
out["lanes"] = sorted(lanes, key=lambda x: -x["per_box"])
sz = u.groupby(["lane", "size"]).agg(trucks=("util", "size"), avg_boxes=("boxes", "mean"), fill=("util", "mean"), cost=("rate", "sum")).reset_index()
out["lane_size"] = [dict(lane=r.lane, size=int(r.size), trucks=int(r.trucks), avg_boxes=int(r.avg_boxes), cap=CAP[int(r.size)],
                         fill=round(r.fill * 100), cost_l=lk(r.cost)) for r in sz.itertuples()]

# 17 ft vs 20 ft
fits17 = u[u.boxes <= CAP[17]]
fits20 = u[(u.boxes <= CAP[20]) & (u["size"] > 20)]
r17 = {ln: rates[(rates.lane == ln) & (rates["size"] == 17)].rate.min() for ln in u.lane.unique()}
save17 = sum(max(0, r.rate - r17.get(r.lane, r.rate)) for r in fits17.itertuples())
out["size_q"] = dict(fits17=int(len(fits17)), fits17_cost_l=lk(fits17.rate.sum()), save17_l=lk(save17),
                     fits20_on_bigger=int(len(fits20)), fits20_bigger_cost_l=lk(fits20.rate.sum()),
                     r17={k: int(v) for k, v in r17.items()},
                     cap=CAP, avg_22=int(u[u["size"] == 22].boxes.mean()), avg_32=int(u[u["size"] == 32].boxes.mean()),
                     avg_20=int(u[u["size"] == 20].boxes.mean()))
qt = quotes.pivot_table(index=["lane", "size"], columns="vendor", values="rate").reset_index()
out["quotes"] = [dict(lane=r["lane"], size=int(r["size"]), **{v: int(r[v]) for v in ("Meta", "Radhe Radhe", "DBRL", "RR Logistics")},
                      paid_sep=int(card_rate(M1, r["lane"], int(r["size"])) or 0),
                      awarded=AWARDED_VENDOR.get(r["lane"], "")) for _, r in qt.iterrows()]
json.dump(out, open(SP + "icl_report.json", "w"), indent=1, default=str)
print(json.dumps({k: out[k] for k in ("kpi", "levers", "size_q", "bands", "lanes")}, indent=1, default=str))

# Excel backup
with pd.ExcelWriter(EXCEL, engine="openpyxl") as xw:
    pd.DataFrame(levers).to_excel(xw, sheet_name="Levers", index=False)
    b[["carrier", "lane", "bill_size", "lr", "lr_date", "month", "billed", "card", "over_card", "status", "src"]].sort_values(["month", "lane"]).to_excel(xw, sheet_name="Bills vs rate card", index=False)
    pd.DataFrame(rec).to_excel(xw, sheet_name="Month reconciliation", index=False)
    u[["lane", "carrier", "dispatch", "month", "lr", "vehicle", "size", "cartons", "boxes", "cap", "util", "rate", "empty_cost", "fit_size", "fit_rate", "rs_saving", "billed", "bill_size"]].sort_values(["lane", "dispatch"]).to_excel(xw, sheet_name="Trucks (utilisation)", index=False)
    mg.to_excel(xw, sheet_name="Merge candidates", index=False)
    pd.DataFrame(out["lanes"]).to_excel(xw, sheet_name="Lanes", index=False)
    qt.to_excel(xw, sheet_name="Vendor quotes Jul26-Dec27", index=False)
print("excel ok")

# the cleaned copy the next steps read (NaN -> null)
import math


def _clean(o):
    if isinstance(o, float) and math.isnan(o):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


json.dump(_clean(json.load(open(SP + "icl_report.json"))), open(SP + "icl_report_clean.json", "w", encoding="utf-8"), separators=(",", ":"))
