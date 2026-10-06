import re, requests, io, zipfile, sys
sys.path.insert(0, "scripts")
import nemweb_common as nw
H = nw.HTTP_HEADERS
cands = [
 "https://www.nemweb.com.au/Reports/Current/NEMDE/",
 "https://www.nemweb.com.au/REPORTS/CURRENT/NEMDE/",
 "https://nemweb.com.au/Reports/Current/NEMDE/",
 "https://www.nemweb.com.au/Reports/Current/",
 "https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/NEMDE/2026/",
]
for u in cands:
    try:
        r = requests.get(u, headers=H, timeout=60)
        hrefs = re.findall(r'href="([^"]+)"', r.text, re.I)
        print("==", u, r.status_code, len(hrefs))
        print("   ", [h for h in hrefs if "nemde" in h.lower() or "price" in h.lower() or u.endswith("2026/")][:60])
    except Exception as e: print("ERR", u, e)
# walk archive month
for u in ["https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/NEMDE/2026/NEMDE_2026_10/",
          "https://www.nemweb.com.au/Data_Archive/Wholesale_Electricity/NEMDE/2026/NEMDE_2026_09/"]:
    try:
        r = requests.get(u, headers=H, timeout=60)
        hrefs = re.findall(r'href="([^"]+)"', r.text, re.I)
        print("==", u, r.status_code, hrefs[-15:])
        for h in hrefs:
            if h.endswith("/") and len(h) > len(u.split("nemweb.com.au")[-1]):
                r2 = requests.get(requests.compat.urljoin(u, h), headers=H, timeout=60)
                h2 = re.findall(r'href="([^"]+)"', r2.text, re.I)
                print("   sub", h, h2[-8:])
    except Exception as e: print("ERR", u, e)
