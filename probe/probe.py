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
dirs = sorted(set(re.findall(r'/CURRENT/([^"/]+)/"', r.text, re.I)))
print("CURRENT dirs matching:", [d for d in dirs if re.search("sens|pasa|predisp|pd7|p5", d, re.I)])
for d in dirs:
    if re.search("sensitiv", d, re.I):
        show(BASE + d + "/", r".*\.zip$", rows_for=("SENSITIV", "SCENARIO"))
show(BASE + "Predispatch_Reports/", r"^PUBLIC_PREDISPATCH_\d{12}_\d{14}_LEGACY\.zip$", rows_for=("SCENARIO",))
show(BASE + "STPASA_DUIDAvailability/", r".*\.zip$", rows_for=("DUIDAVAIL",))
