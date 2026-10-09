import re
import sys

import requests

sys.path.insert(0, "scripts")
import nemweb_common as nw  # noqa: E402

BASE = "https://www.nemweb.com.au/REPORTS/CURRENT/"


def show(dir_url, pat, rows_for=(), n=1):
    try:
        files = nw.list_nemweb_files(dir_url, pat)
    except Exception as e:
        print("LIST FAIL", dir_url, e)
        return
    print("\n###", dir_url, len(files), "files; last:", [f.rsplit("/", 1)[-1] for f in files[-3:]])
    if not files:
        return
    t = nw.parse_mms_zip(nw.download_bytes(files[-1]))
    for k, df in t.items():
        print("  TABLE", k, df.shape)
        print("    cols:", list(df.columns)[:70])
        if any(x in str(k).upper() for x in rows_for):
            print(df.head(4).to_string()[:4000])


r = requests.get(BASE, headers=nw.HTTP_HEADERS, timeout=30)
hrefs = re.findall(r'href="([^"]+)"', r.text, re.I)
print("N hrefs", len(hrefs)); print([h for h in hrefs][:400])
for d in ["Predispatch_Sensitivities", "PredispatchIS_Reports", "Predispatch_IRSR", "PD7Day", "P5_Reports"]:
    try:
        f = nw.list_nemweb_files(BASE + d + "/", r".*\.zip$")
        print("DIR", d, len(f), [x.rsplit('/',1)[-1] for x in f[-2:]])
        if f and d != "P5_Reports":
            t = nw.parse_mms_zip(nw.download_bytes(f[-1]))
            for k, df in t.items():
                print("  TABLE", k, df.shape, list(df.columns)[:70])
                if "SENS" in str(k).upper() or "SCENARIO" in str(k).upper():
                    print(df.head(6).to_string()[:5000])
    except Exception as e:
        print("DIR", d, "FAIL", e)
f = nw.list_nemweb_files(BASE + "Predispatch_Reports/", r".*\.zip$")
print("PD all", len(f), sorted({re.sub(r"\d+", "#", x.rsplit('/',1)[-1]) for x in f}))
