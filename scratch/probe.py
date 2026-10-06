import io, re, zipfile, sys, requests
sys.path.insert(0, "scripts")
import nemweb_common as nw
H = nw.HTTP_HEADERS
dirs = {
 "bidmove": "https://www.nemweb.com.au/REPORTS/CURRENT/Bidmove_Complete/",
 "nextday": "https://www.nemweb.com.au/REPORTS/CURRENT/Next_Day_Dispatch/",
 "prices": "https://www.nemweb.com.au/REPORTS/CURRENT/Public_Prices/",
 "nemde": "https://www.nemweb.com.au/REPORTS/CURRENT/NEMDE/",
 "predisp": "https://www.nemweb.com.au/REPORTS/CURRENT/PredispatchIS_Reports/",
}
latest = {}
for k, u in dirs.items():
    try:
        r = requests.get(u, headers=H, timeout=60)
        hrefs = re.findall(r'href="([^"]+)"', r.text, re.I)
        names = [h.rsplit("/",1)[-1] for h in hrefs if h.lower().endswith((".zip",".xml",".csv"))]
        subdirs = [h for h in hrefs if h.endswith("/")][-5:]
        print(f"== {k} status={r.status_code} n={len(names)} last={names[-4:]} subdirs={subdirs}")
        if names: latest[k] = u + names[-1]
    except Exception as e:
        print("ERR", k, e)

def headers_of(url, maxlines=None):
    b = requests.get(url, headers=H, timeout=300).content
    print(f"-- {url} bytes={len(b)}")
    zf = zipfile.ZipFile(io.BytesIO(b))
    for n in zf.namelist()[:5]:
        print("   member", n, zf.getinfo(n).file_size)
    name = zf.namelist()[0]
    with zf.open(name) as f:
        if name.lower().endswith(".xml") or name.lower().endswith(".zip"):
            print(f.read(3000).decode("utf-8","replace")); return
        counts = {}
        for i, line in enumerate(io.TextIOWrapper(f, encoding="utf-8", errors="replace")):
            if line.startswith("I,"):
                print("   ", line.strip()[:900])
            elif line.startswith("D,"):
                key = ",".join(line.split(",")[1:3]); counts[key] = counts.get(key,0)+1
                if counts[key] <= 2: print("      D:", line.strip()[:500])
        print("   rowcounts", counts)

for k in ["bidmove","nextday","prices"]:
    if k in latest:
        try: headers_of(latest[k])
        except Exception as e: print("ERR", k, e)
# NEMDE: walk into subdirs
try:
    r = requests.get(dirs["nemde"], headers=H, timeout=60)
    print(r.text[:3000])
except Exception as e: print("ERR nemde", e)
