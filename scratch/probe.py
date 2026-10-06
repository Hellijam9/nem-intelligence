import re, io, zipfile, sys, requests
sys.path.insert(0, "scripts")
import nemweb_common as nw
H = nw.HTTP_HEADERS
def ls(u):
    r = requests.get(u, headers=H, timeout=60); print("LS", u, r.status_code)
    return re.findall(r'href="([^"]+)"', r.text, re.I)
base = "https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/"
years = [h for h in ls(base) if re.search(r"/\d{4}/$", h)]
print(years[-3:])
y = requests.compat.urljoin(base, years[-1]); months = [h for h in ls(y) if "MMSDM_" in h and h.endswith("/")]
print(months[-3:])
for m in months[-2:][::-1]:
    mu = requests.compat.urljoin(y, m)
    sub = ls(mu); print(sub[:10])
    for s in sub:
        if "SQLLoader" in s or "DATA" in s:
            du = requests.compat.urljoin(mu, s)
            inner = ls(du); print(inner[:10])
            for i in inner:
                if i.endswith("DATA/") :
                    du2 = requests.compat.urljoin(du, i); files = ls(du2)
                    hits = [f for f in files if re.search(r"GENUNITS|DUALLOC|DUDETAILSUMMARY|DUDETAIL[#_]|STATIONOWNER|STATION[#_]|PARTICIPANT[#_]", f)]
                    print("\n".join(hits[:30]))
                    def show(name, maxrows=8):
                        u = requests.compat.urljoin(du2, name)
                        b = requests.get(u, headers=H, timeout=300).content
                        with zipfile.ZipFile(io.BytesIO(b)) as z:
                            txt = z.read(z.namelist()[0]).decode("utf-8", "replace").splitlines()
                        print("----", name, len(txt))
                        for l in txt[:maxrows]: print("   ", l[:400])
                        return txt
                    g = [h for h in hits if "GENUNITS" in h and "GENUNITS_UNIT" not in h]
                    if g:
                        txt = show(g[0].rsplit("/",1)[-1] if g[0].startswith("/") else g[0])
                        import collections, csv
                        rows = list(csv.reader(txt)); hdr = next(r for r in rows if r and r[0]=="I")
                        ix = {c:i for i,c in enumerate(hdr)}
                        c = collections.Counter(r[ix["CO2E_ENERGY_SOURCE"]] for r in rows if r and r[0]=="D")
                        print("CO2E_ENERGY_SOURCE vocab:", c.most_common(60))
                        c2 = collections.Counter(r[ix["GENSETTYPE"]] for r in rows if r and r[0]=="D"); print("GENSETTYPE", c2)
                    for key in ["DUALLOC", "DUDETAILSUMMARY", "GENUNITS_UNIT"]:
                        f = [h for h in hits if key in h]
                        if f: show(f[0].rsplit("/",1)[-1] if f[0].startswith("/") else f[0], 5)
                    sys.exit(0)
