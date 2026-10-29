"""Open the Intercity Logistics Tracker with birbal@'s token; dump every tab (UNFORMATTED) to JSON.
A 429 (per-user read quota, shared with the other birbal@ jobs) is waited out, never read as a sharing problem."""
import json
import time
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SHEET = "1jMW9R_y21ytPlnCpXwcRbzjIaqzcZtH61aVQtephci8"
from icl_env import W, TOKEN

OUT = W + "ratecard.json"
TOKENS = [TOKEN]
for t in TOKENS:
    try:
        creds = Credentials.from_authorized_user_file(t)
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
        svc = build("sheets", "v4", credentials=creds)
        meta = svc.spreadsheets().get(spreadsheetId=SHEET, fields="properties.title,sheets.properties").execute()
    except Exception as e:
        print("no:", t.split("/")[-2], str(e)[:90])
        continue
    print("opened with", t.split("/")[-2], "-", meta["properties"]["title"])
    out = {}
    for s in meta["sheets"]:
        p = s["properties"]
        title = p["title"]
        for wait in (10, 20, 40, 60, 60, None):
            try:
                g = svc.spreadsheets().values().get(spreadsheetId=SHEET, range="'%s'" % title,
                                                    valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
                break
            except Exception as e:
                if wait is None or "429" not in str(e):
                    raise
                print("  429 on", repr(title), "- waiting", wait, "s")
                time.sleep(wait)
        out[title] = {"gid": p["sheetId"], "rows": g}
        print("  tab", p["sheetId"], repr(title), len(g), "rows x", max((len(r) for r in g), default=0), "cols")
    json.dump(out, open(OUT, "w"))
    break
else:
    raise SystemExit("no token opened the Intercity Logistics Tracker")
