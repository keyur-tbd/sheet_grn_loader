"""Intercity Logistics Tracker -> trip-level table: dispatch log (warehouse tabs) + LR/bill amounts + rate card in force.

Writes icl_trips.pkl, icl_rates.pkl, icl_lr.pkl for the analysis step and prints coverage checks.
"""
import json
import re
import datetime as dt
import pandas as pd

from icl_env import W as SP, M0, NEXT, add_months
d = json.load(open(SP + "ratecard.json"))
E = dt.date(1899, 12, 30)


def to_date(v):
    if isinstance(v, (int, float)) and 40000 < v < 50000:
        return E + dt.timedelta(days=int(v))
    if isinstance(v, str):
        m = re.match(r"\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})", v)
        if m:
            try:
                return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                return None
    return None


def size_of(v):
    s = str(v).upper()
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def lr_of(v):
    if isinstance(v, (int, float)):
        return str(int(v))
    m = re.search(r"\d+", str(v))
    return m.group(0) if m else None


# ---------------- 1. dispatch log ----------------
LANES = {"Mumbai Warehouse": "Mumbai", "Bangalore Warehouse": "Bangalore", "Hyderabad Warehouse": "Hyderabad",
         "Kolkata Warehouse": "Kolkata", "Delhi Warehouse": "Gurgaon"}
trips = []
for tab, lane in LANES.items():
    rows = d[tab]["rows"]
    hdr = [str(h).strip().lower() for h in rows[0]]
    col = lambda key: next(i for i, h in enumerate(hdr) if key in h)
    c_disp, c_lr, c_size, c_box, c_tr, c_veh = col("dispatch date"), col("lr no"), col("truck size"), col("total no of boxes"), col("transporter"), col("vehicle no")
    for r in rows[2:]:
        r = r + [""] * (len(hdr) - len(r))
        dd = to_date(r[c_disp])
        if not dd or not (M0 <= dd < add_months(NEXT, 1)):     # the period + the month after it
            continue
        box = r[c_box]
        trips.append(dict(lane=lane, dispatch=dd, month=dd.replace(day=1), lr=lr_of(r[c_lr]), size=size_of(r[c_size]),
                          size_raw=r[c_size], boxes=float(box) if isinstance(box, (int, float)) else None,
                          transporter=str(r[c_tr]).strip(), vehicle=str(r[c_veh]).strip()))
trips = pd.DataFrame(trips)

# ---------------- 2. LR / bill amounts ----------------
lr = []
for tab in ("Intercity Bills/LR-2026-27", "MIS Vs Actual - Bill Details"):
    rows = d[tab]["rows"]
    hdr = [str(h).strip().lower() for h in rows[0]]
    gi = lambda key: next(i for i, h in enumerate(hdr) if h.startswith(key))
    c_tr, c_city, c_size, c_lr, c_date, c_month, c_amt = gi("transporter"), gi("city"), gi("size"), gi("lr no"), gi("lr date"), gi("month"), gi("amount")
    c_status = next((i for i, h in enumerate(hdr) if "status" in h), None)
    for r in rows[1:]:
        r = r + [""] * (len(hdr) - len(r))
        if not isinstance(r[c_amt], (int, float)):
            continue
        lr.append(dict(src=tab, transporter=str(r[c_tr]).strip(), city=str(r[c_city]).strip().upper(), bill_size=size_of(r[c_size]),
                       lr=lr_of(r[c_lr]), lr_date=to_date(r[c_date]), month_txt=str(r[c_month]).strip(), billed=float(r[c_amt]),
                       status=str(r[c_status]).strip() if c_status is not None else ""))
lr = pd.DataFrame(lr)

# ---------------- 3. rate card in force, per month block of the FY'27 tab ----------------
rows = d["FY'27"]["rows"]
# month blocks are headed "April'26", "May' 26", "Sept'26", ...: month name + two-digit year
MON3 = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def block_month(v):
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\s*'\s*(\d{2})", str(v).strip())
    return dt.date(2000 + int(m.group(2)), MON3[m.group(1).lower()], 1) if m and m.group(1).lower() in MON3 else None


SIZES = [9, 14, 17, 20, 22, 32]
LANE_COLS = {"Mumbai": 1, "Bangalore": 8, "Hyderabad": 15, "Gurgaon": 23, "Kolkata": 30}
CAP = dict(zip(SIZES, [240, 644, 716, 1430, 1595, 2500]))
rates = []
for i, r in enumerate(rows):
    mon = block_month(r[0]) if r and isinstance(r[0], str) else None
    cost = rows[i + 2] if mon and i + 2 < len(rows) else None
    if cost and str(cost[0]).strip() == "Cost":    # a month block: heading, capacity row, "Cost" row
        for lane, c0 in LANE_COLS.items():
            for k, s in enumerate(SIZES):
                v = cost[c0 + k] if c0 + k < len(cost) else ""
                if isinstance(v, (int, float)) and v > 100:
                    rates.append(dict(month=mon, lane=lane, size=s, rate=float(v)))
rates = pd.DataFrame(rates)

# the July 2026 - Dec 2027 vendor quotes (FY'27 tab, columns BB-BG) and "cost as on 5 Oct"
quotes = []
QLANES = ["Mumbai", "Bangalore", "Gurgaon", "Hyderabad", "Kolkata"]
for i, r in enumerate(rows[:30]):
    if len(r) > 53 and str(r[52]).strip() in ("Meta", "Radhe Radhe", "RR Logistics", "DBRL"):
        vendor = str(r[52]).strip()
        for j in (i + 2, i + 3, i + 4):
            rr = rows[j]
            size = size_of(rr[52]) if len(rr) > 52 else None
            if not size:
                continue
            for k, ln in enumerate(QLANES):
                v = rr[53 + k] if len(rr) > 53 + k else ""
                if isinstance(v, (int, float)):
                    quotes.append(dict(vendor=vendor, lane=ln, size=size, rate=float(v)))
quotes = pd.DataFrame(quotes)
current = []
for i in range(2, 9):
    rr = rows[i]
    if len(rr) > 65 and str(rr[59]).strip() in ("Hyd", "Gur", "Mum", "Kol", "BLR"):
        ln = {"Hyd": "Hyderabad", "Gur": "Gurgaon", "Mum": "Mumbai", "Kol": "Kolkata", "BLR": "Bangalore"}[str(rr[59]).strip()]
        for k, s in enumerate([9, 14, 17, 20, 22, 32]):
            v = rr[60 + k]
            if isinstance(v, (int, float)):
                current.append(dict(lane=ln, size=s, rate=float(v)))
current = pd.DataFrame(current)

# MIS by month x lane (FY'27 tab, rows 3-9: Mum, Banga, Hbad, Delhi, Kol, Harry's, Total, Bill submitted)
mis = []
MNAME = {"April": 4, "May": 5, "June": 6, "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
         "December": 12, "January": 1, "February": 2, "March": 3}
for i in range(2, 20):
    rr = rows[i]
    if len(rr) > 23 and str(rr[14]).strip() in MNAME:
        mn = MNAME[str(rr[14]).strip()]
        mon = dt.date(M0.year if mn >= 4 else M0.year + 1, mn, 1)   # FY'27 tab
        for k, ln in enumerate(["Mumbai", "Bangalore", "Hyderabad", "Gurgaon", "Kolkata", "Harry"]):
            v = rr[15 + k]
            if isinstance(v, (int, float)):
                mis.append(dict(month=mon, lane=ln, mis=float(v)))
        mis.append(dict(month=mon, lane="_bill_submitted", mis=float(rr[22]) if isinstance(rr[22], (int, float)) else None))
        mis.append(dict(month=mon, lane="_trips", mis=float(rr[31]) if len(rr) > 31 and isinstance(rr[31], (int, float)) else None))
mis = pd.DataFrame(mis)

for name, df in [("trips", trips), ("lr", lr), ("rates", rates), ("quotes", quotes), ("current", current), ("mis", mis)]:
    df.to_pickle(SP + f"icl_{name}.pkl")
    print(name, len(df))
print(trips.groupby(["lane", "month"]).size().unstack(0).fillna(0).astype(int))
print("sizes:", trips["size"].value_counts(dropna=False).to_dict(), "| no boxes:", trips["boxes"].isna().sum())
print("transporters:", trips["transporter"].str.upper().value_counts().to_dict())
print(lr.groupby(["src", "month_txt"]).size())
print("LR dup:", lr["lr"].duplicated().sum(), "| LR in log:", lr["lr"].isin(trips["lr"]).mean().round(3))
print(quotes.pivot_table(index=["lane", "size"], columns="vendor", values="rate"))
print(current.pivot(index="lane", columns="size", values="rate"))
print(rates.pivot_table(index=["lane", "size"], columns="month", values="rate"))
