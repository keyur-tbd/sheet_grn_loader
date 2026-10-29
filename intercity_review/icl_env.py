"""Where the Intercity Freight Review reads and writes, and which months it covers (run daily, 02:35 IST).

Environment (GitHub Actions sets these; on the Birbal laptop the defaults apply):
  ICL_WORK         folder for the intermediate files (default: ./work next to the scripts)
  ICL_OUT          folder for the Excel workbook (default: D:/Python/Birbal on Windows, else ICL_WORK)
  ICL_TOKEN_JSON   birbal@'s Google token, which opens the Intercity Logistics Tracker
                   (default: D:/Python/Birbal/salary_loader/token_birbal.json)

Period: April 2026 (FY 2026-27 opens) through the last complete month, in IST. N = months covered;
ANNUAL turns a period total into a year (12 / N). NEXT = the month after the period, whose tracker block is
priced but not yet billed.
"""
import calendar
import datetime as dt
import os

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.environ.get("ICL_WORK", os.path.join(HERE, "work")).rstrip("/\\") + "/"
os.makedirs(W, exist_ok=True)
OUT = os.environ.get("ICL_OUT", r"D:/Python/Birbal" if os.name == "nt" else W).rstrip("/\\") + "/"
TOKEN = os.environ.get("ICL_TOKEN_JSON", r"D:/Python/Birbal/salary_loader/token_birbal.json")
EXCEL = OUT + "Intercity Freight - Rate Card and Utilisation.xlsx"


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


TODAY = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5, minutes=30)).date()
M0 = dt.date(2026, 4, 1)
M1 = max(M0, add_months(TODAY.replace(day=1), -1))          # first day of the last month in the period
NEXT = add_months(M1, 1)
D1 = dt.date(M1.year, M1.month, calendar.monthrange(M1.year, M1.month)[1])   # last day of the period
MONTHS = [add_months(M0, i) for i in range((M1.year - M0.year) * 12 + M1.month - M0.month + 1)]
N = len(MONTHS)
ANNUAL = 12 / N
JULY = dt.date(2026, 7, 1)                                     # the contracted card starts
N_CARD = len([m for m in MONTHS if m >= JULY])                 # months on the contracted card
PERIOD = M0.strftime("%b") + "-" + M1.strftime("%b %Y") if N > 1 else M1.strftime("%b %Y")
