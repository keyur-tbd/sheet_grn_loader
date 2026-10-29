"""The vendor-wise rate card from the Intercity Logistics Tracker's FY'27 tab (owner, 2026-10-08: rebuild on it).

Three vendor tables in that tab, none of them dated:
  A1:L9    "Rate Card"            RR, Vihira, Radhe (+ "Radhe new", June), DB, Meta, per lane and truck size
  AH:AX    vendor blocks           Shiv Shakti, Meta Road, Banglore-Delhi Roadways, per lane (20 / 22 / 32 ft)
  BA:BF    July 2026 quotes        Meta, Radhe Radhe, RR Logistics, DBRL, "July 2026 onwards until 31 Dec 2027"
Assumed validity (not confirmed by logistics): April-June = A1:L9, then AH:AX; July on = the July quote, then A1:L9,
then AH:AX. Where a carrier has no rate for a lane and size, card() falls back to the lane's monthly block rate (the
"Cost" row) and says so in the source.
"""
import datetime as dt
import json
import re

from icl_env import W
JULY = dt.date(2026, 7, 1)
CARRIER = {"RR": "R R Logistics", "RR LOGISTICS": "R R Logistics", "VIHIRA": "Vihira", "RADHE": "Radhe Radhe",
           "RADHE NEW": "Radhe Radhe", "RADHE RADHE": "Radhe Radhe", "DB": "Delhi Bangalore Roadlines", "DBRL": "Delhi Bangalore Roadlines",
           "BANGLORE - DELHI ROADWAYS": "Delhi Bangalore Roadlines", "META": "Meta Road", "META ROAD": "Meta Road",
           "SHIV SHAKT": "Shiv Shakti"}
LANE = {"MUMBAI": "Mumbai", "HBAD": "Hyderabad", "HYDERABD": "Hyderabad", "DELHI": "Gurgaon", "DELHI (JUNE)": "Gurgaon",
        "GURGAON": "Gurgaon", "KOLKATTA": "Kolkata", "KOLKATA": "Kolkata", "KOL": "Kolkata", "BANGALORE": "Bangalore",
        "BANGLORE": "Bangalore", "BANG": "Bangalore", "BLR": "Bangalore"}


def _size(v):
    m = re.search(r"(\d+)", str(v))
    return int(m.group(1)) if m else None


def build(path=W + "ratecard.json"):
    rows = json.load(open(path, encoding="utf-8"))["FY'27"]["rows"]
    cell = lambda r, c: rows[r][c] if c < len(rows[r]) else ""
    out = []   # dict(table, carrier, lane, size, rate, note)
    # A1:L9: vendor in row 2, lane in row 3, sizes in rows 4-9 (column A)
    for c in range(2, 12):
        v, ln = str(cell(1, c)).strip().upper(), str(cell(2, c)).strip().upper()
        if v not in CARRIER or ln not in LANE:
            continue
        for r in range(3, 9):
            rate = cell(r, c)
            if isinstance(rate, (int, float)) and rate > 1000:
                out.append(dict(table="A1:L9", carrier=CARRIER[v], lane=LANE[ln], size=_size(cell(r, 0)), rate=float(rate),
                                note=("June" if "JUNE" in ln else "") + (" (col %s)" % chr(65 + c))))
    # AH:AX: three vendor blocks, vendor name two columns right of 'Vendor', lanes in row 3, sizes in rows 4-9
    for c0 in range(33, 51):
        if str(cell(1, c0)).strip() != "Vendor":
            continue
        v = str(cell(1, c0 + 2)).strip().upper()
        if v not in CARRIER:
            continue
        for c in range(c0 + 1, c0 + 6):
            ln = str(cell(2, c)).strip().upper()
            if ln not in LANE:
                continue
            for r in range(3, 9):
                rate = cell(r, c)
                if isinstance(rate, (int, float)) and rate > 1000:
                    out.append(dict(table="AH:AX", carrier=CARRIER[v], lane=LANE[ln], size=_size(cell(r, c0)), rate=float(rate), note=""))
    # BA:BF: blocks headed by the vendor name in column BA (52), lanes beside it, then 'Vehicle Size' and 20/22/32 ft rows
    for r in range(0, 30):
        v = str(cell(r, 52)).strip().upper()
        if v not in CARRIER or v == "SHIV SHAKT":
            continue
        lanes = [LANE.get(str(cell(r, c)).strip().upper()) for c in range(53, 58)]
        for rr in range(r + 2, r + 5):
            s = _size(cell(rr, 52))
            if not s:
                continue
            for k, ln in enumerate(lanes):
                rate = cell(rr, 53 + k)
                if ln and isinstance(rate, (int, float)) and rate > 1000:
                    out.append(dict(table="July quote", carrier=CARRIER[v], lane=ln, size=s, rate=float(rate), note="Jul 2026 - Dec 2027"))
    return out


TABLE = build()
_idx = {}
for e in TABLE:
    _idx.setdefault((e["table"], e["carrier"], e["lane"], e["size"]), []).append(e)


def _pick(table, carrier, lane, size, month):
    es = _idx.get((table, carrier, lane, size)) or []
    if not es:
        return None
    if table == "A1:L9":
        # 'Radhe new' is the June rate; of the two Vihira Bangalore columns the right-hand one (K) is the one billed
        june = [e for e in es if e["note"].startswith("June")]
        if june and month == dt.date(2026, 6, 1):
            return june[0]
        es = [e for e in es if not e["note"].startswith("June")] or es
        return sorted(es, key=lambda e: e["note"])[-1]
    return es[0]


def card(month, lane, size, carrier, lane_rate=None):
    """(rate, source) for this carrier on this lane and size in this month; lane_rate is the monthly block fallback."""
    order = ["July quote", "A1:L9", "AH:AX"] if month >= JULY else ["A1:L9", "AH:AX", "July quote"]
    for tbl in order:
        e = _pick(tbl, carrier, lane, size, month)
        if e:
            return e["rate"], tbl
    if lane_rate is not None:
        return float(lane_rate), "lane rate"
    return None, None


def matches(lane, size, carrier, amount):
    """Every vendor table entry for this carrier, lane and size that equals the amount (to explain a mismatch)."""
    return sorted({e["table"] for e in TABLE if e["carrier"] == carrier and e["lane"] == lane and e["size"] == size and e["rate"] == amount})


if __name__ == "__main__":
    import collections
    print(len(TABLE), "vendor rates;", collections.Counter(e["table"] for e in TABLE))
    for e in TABLE:
        print(e)
