"""The one rate lookup every review script uses (owner, 2026-10-08).

From July 2026: the contracted card (awarded_card.json, the green cells of "Rate Card - July, 2026 onwards until
31st Dec, 2027"): the awarded vendor's rate for the lane and size, whoever carried the truck.
Before July: no contracted card in the tracker, so each carrier's own rate from the undated vendor tables
(vendor_card.py: A1:L9, then AH:AX), and the lane's monthly block rate where the carrier has none.
card_rate() returns the rate; card_source() says which rule gave it.
"""
import datetime as dt
import json

import pandas as pd

import vendor_card

from icl_env import W
JULY = dt.date(2026, 7, 1)
AW = json.load(open(W + "awarded_card.json", encoding="utf-8"))
AWARDED = {ln: {int(s): r for s, r in a["rates"].items()} for ln, a in AW["awarded"].items()}
AWARDED_VENDOR = {ln: a["vendor"] for ln, a in AW["awarded"].items()}
_rates = pd.read_pickle(W + "icl_rates.pkl")
LANE_RATE = {(r.month, r.lane, r.size): r.rate for r in _rates.itertuples()}   # the FY'27 monthly block "Cost" rows


def _lookup(month, lane, size, carrier):
    if size is None or pd.isna(size):
        return None, None
    size = int(size)
    if month >= JULY:
        r = AWARDED.get(lane, {}).get(size)
        if r is not None:
            return float(r), "July card"
        lr = LANE_RATE.get((month, lane, size))
        return (float(lr), "lane rate") if lr is not None else (None, None)
    return vendor_card.card(month, lane, size, carrier, lane_rate=LANE_RATE.get((month, lane, size)))


def card_rate(month, lane, size, carrier=None):
    return _lookup(month, lane, size, carrier)[0]


def card_source(month, lane, size, carrier=None):
    return _lookup(month, lane, size, carrier)[1]


def sizes_priced(month, lane, carrier=None):
    """{size: rate} for every truck size the card prices for this lane (and carrier, before July)."""
    out = {}
    for s in (9, 14, 17, 20, 22, 32):
        r = card_rate(month, lane, s, carrier)
        if r is not None:
            out[s] = r
    return out


def fix_day_month(d, month):
    """A date typed day-first that the sheet read month-first (8 Apr stored as 4 Aug): when the date is not in the
    month the truck was billed in but would be with day and month swapped, swap them. Otherwise leave it."""
    if d is None or month is None or pd.isna(d) or pd.isna(month):
        return d
    if d.month == month.month and d.year == month.year:
        return d
    if d.day <= 12:
        try:
            sw = dt.date(d.year, d.day, d.month)
        except ValueError:
            return d
        if sw.month == month.month and sw.year == month.year:
            return sw
    return d
