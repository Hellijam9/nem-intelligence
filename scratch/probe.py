import re, requests, io, zipfile, sys
sys.path.insert(0, "scripts")
import nemweb_common as nw
H = nw.HTTP_HEADERS
def listing(u):
    r = requests.get(u, headers=H, timeout=60)
    return [h.rsplit("/",1)[-1] for h in re.findall(r'href="([^"]+)"', r.text, re.I)]
a = listing("https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/")
print("archive dispatchis", len(a), a[-5:])
p = [n for n in listing("https://www.nemweb.com.au/REPORTS/CURRENT/Predispatch_Reports/") if n.endswith("_LEGACY.zip")]
print("predispatch", len(p), p[-2:])
def show(b, label, want=None):
    zf = zipfile.ZipFile(io.BytesIO(b))
    names = zf.namelist(); print("--", label, len(b), names[:3], len(names))
    inner = names[0]; data = zf.read(inner)
    if inner.lower().endswith(".zip"):
        zf2 = zipfile.ZipFile(io.BytesIO(data)); inner2 = zf2.namelist()[0]; data = zf2.read(inner2)
    seen = {}
    for line in data.decode("utf-8","replace").splitlines():
        if line.startswith("I,"):
            print("   ", line[:700])
        elif line.startswith("D,"):
            k = ",".join(line.split(",")[1:3]); seen[k] = seen.get(k,0)+1
            if seen[k] == 1 and (want is None or any(w in k for w in want)): print("     D:", line[:400])
    print("   counts", seen)
dz = [n for n in a if "20261005" in n]
if dz: show(requests.get("https://www.nemweb.com.au/REPORTS/ARCHIVE/DispatchIS_Reports/"+dz[0], headers=H, timeout=300).content, dz[0], ["INTERCONNECTORRES","REGIONSUM","PRICE"])
show(requests.get("https://www.nemweb.com.au/REPORTS/CURRENT/Predispatch_Reports/"+p[-1], headers=H, timeout=300).content, p[-1], ["REGION","INTERCONNECTOR"])
