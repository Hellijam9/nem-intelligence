import sys
sys.path.insert(0, "scripts")
import nemweb_common as nw  # noqa
B = "https://www.nemweb.com.au/REPORTS/CURRENT/"
u = nw.get_latest_files(B + "Predispatch_Reports/", r"^PUBLIC_PREDISPATCH_\d{12}_\d{14}_LEGACY\.zip$")[-1]
t = nw.parse_mms_zip(nw.download_bytes(u)); r = nw.get_table(t, "PDREGION")
print("PDREGION cols", list(r.columns))
cols = [c for c in r.columns if any(k in c for k in ["UIGF", "SOLAR", "WIND", "SEMI", "AVAILABLE", "UNCONSTRAINED", "CLEARED", "SUPPLY"])]
print(r[["REGIONID", "PERIODID", "DATETIME", "RRP", "TOTALDEMAND"] + cols].tail(12).to_string())
print("PD periods", r["PERIODID"].nunique(), r["DATETIME"].min() if "DATETIME" in r else "", r["DATETIME"].max() if "DATETIME" in r else "")
s = nw.get_latest_files(B + "STPASA_DUIDAvailability/", r".*\.zip$")[-1]
d = nw.get_table(nw.parse_mms_zip(nw.download_bytes(s)), "DUIDAVAILABILITY")
print("STPASA horizon", d["INTERVAL_DATETIME"].min(), d["INTERVAL_DATETIME"].max(), d["DUID"].nunique())
p7 = nw.get_latest_files(B + "PD7Day/", r".*\.zip$")[-1]
p = nw.get_table(nw.parse_mms_zip(nw.download_bytes(p7)), "PRICESOLUTION")
print("PD7DAY horizon", p["INTERVAL_DATETIME"].min(), p["INTERVAL_DATETIME"].max(), len(p))
