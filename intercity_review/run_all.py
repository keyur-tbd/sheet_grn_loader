"""Rebuild the Intercity Freight Review end to end and publish it to Birbal's WIP area (wip.intercity_review).

GitHub Actions runs this daily at 02:35 IST (.github/workflows/intercity_review.yml); on the laptop: python run_all.py.
The repo is public, so each step's output (bills, rates, freight figures) goes to work/run_all.log, never to the
console: the Actions log shows only which step ran and whether it passed (and a failing step's last error lines).
"""
import os
import subprocess
import sys
import time

from icl_env import W, PERIOD

STEPS = [
    "ratecard_read.py",       # the Intercity Logistics Tracker -> work/ratecard.json (birbal@ token)
    "awarded_card.py",        # the July 2026 card with the awarded (green) vendor per lane
    "icl_build.py",           # trips, LR bills, rate cards, quotes, MIS
    "icl_analyse2.py",        # bills vs card, fill by volume, right-size, merge, vendors
    "icl_report_data.py",     # headline figures + the Excel
    "icl_cartons_update.py",  # ERP boxes for unlogged trucks, freight per product
    "icl_levers_ext.py",      # levers including billed trucks the logs missed
    "icl_trips_out.py",       # truck by truck, with the action each one calls for
    "icl_cartons_compare.py", # our cartons vs the logged boxes, by lane and day
    "excel_extras.py",        # the workbook sheets icl_report_data.py does not write
    "publish_review.py",      # -> wip.intercity_review
]
HERE = os.path.dirname(os.path.abspath(__file__))
print("Intercity Freight Review,", PERIOD)
with open(W + "run_all.log", "w", encoding="utf-8") as log:
    for s in STEPS:
        t0 = time.time()
        p = subprocess.run([sys.executable, s], cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace")
        log.write(f"===== {s} (exit {p.returncode})\n{p.stdout}\n{p.stderr}\n")
        log.flush()
        print(f"  {s:24s} {'ok' if p.returncode == 0 else 'FAILED'} ({time.time() - t0:.0f} s)")
        if p.returncode:
            print("\n".join(p.stderr.strip().splitlines()[-4:]))
            sys.exit(1)
print("published; details in", W + "run_all.log")
