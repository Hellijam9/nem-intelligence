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

hdr, data = load("PUBLIC_ARCHIVE%23GENUNITS%23FILE01%23202608010000.zip")
ix = {c: i for i, c in enumerate(hdr)}
for col in ["CO2E_ENERGY_SOURCE", "GENSETTYPE", "CO2E_DATA_SOURCE"]:
    if col in ix: print("vocab", col, collections.Counter(d[ix[col]] for d in data).most_common(80))
want = "DRXNAE01 GESF1 GPWFWST1 QPSFB2 SMFBESS2 BUNGAMB1 SNB02 WOORB1 KIDSPHG1 TB3B1 CRWARP1 ERB02 MLB01 SWANBBF1 WOOLES1 UWF1 MREHA3 WAMBOWF2 WILLBES1 SNB01 MUCRKSF1".split()
h2, da = load("PUBLIC_ARCHIVE%23DUALLOC%23FILE01%23202608010000.zip"); ia = {c: i for i, c in enumerate(h2)}
gen = {d[ia["DUID"]]: d[ia["GENSETID"]] for d in da}
src = {}
for d in data:
    src[d[ix["GENSETID"]]] = (d[ix.get("CO2E_ENERGY_SOURCE")], d[ix.get("GENSETTYPE")])
for w in want: print(w, gen.get(w), src.get(gen.get(w, w)))
h3, ds = load("PUBLIC_ARCHIVE%23STATION%23FILE01%23202608010000.zip")
