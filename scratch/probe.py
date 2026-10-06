import re, io, zipfile, sys, csv, collections, requests
sys.path.insert(0, "scripts")
import nemweb_common as nw
H = nw.HTTP_HEADERS
D = "https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/2026/MMSDM_2026_08/MMSDM_Historical_Data_SQLLoader/DATA/"
r = requests.get(D, headers=H, timeout=60)
files = [h.rsplit("/", 1)[-1] for h in re.findall(r'href="([^"]+)"', r.text, re.I)]
print("n files", len(files))
hits = [f for f in files if re.search(r"GENUNITS|DUALLOC|DUDETAIL|STATION|PARTICIPANT[^A-Z]|BIDTYPES", f, re.I)]
print("\n".join(hits))
def load(name):
    b = requests.get(D + name, headers=H, timeout=300).content
    with zipfile.ZipFile(io.BytesIO(b)) as z:
        txt = z.read(z.namelist()[0]).decode("utf-8", "replace").splitlines()
    rows = list(csv.reader(txt)); hdr = next(r for r in rows if r and r[0] == "I")
    data = [r for r in rows if r and r[0] == "D"]
    print("----", name, "rows", len(data)); print("   cols:", hdr[4:])
    for d in data[:3]: print("   ", d[4:])
    return hdr, data
for key in ["GENUNITS#", "DUALLOC", "DUDETAILSUMMARY", "DUDETAIL#", "STATION#", "STATIONOWNER#", "GENUNITS_UNIT"]:
    f = [h for h in hits if key.rstrip("#") in h and (not key.endswith("#") or re.search(key.rstrip("#") + r"[#_]FILE|" + key.rstrip("#") + r"_2", h))]
    if not f: f = [h for h in hits if key.rstrip("#") in h]
    if not f: print("MISSING", key); continue
    hdr, data = load(f[0])
    ix = {c: i for i, c in enumerate(hdr)}
    for col in ["CO2E_ENERGY_SOURCE", "GENSETTYPE", "DISPATCHTYPE", "SCHEDULE_TYPE", "UNITTYPE", "DUTYPE", "STARTTYPE"]:
        if col in ix:
            print("   vocab", col, collections.Counter(d[ix[col]] for d in data).most_common(50))
