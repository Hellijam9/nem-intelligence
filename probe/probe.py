import sys
sys.path.insert(0, "scripts")
import bid_stack_analysis as b  # noqa
data_url, month = b._latest_mmsdm_data_dir()
print("MMSDM", month, data_url)
files = b._links(data_url)
print([f.rsplit('/',1)[-1] for f in files if "SCENARIO" in f.upper() or "SENSITIV" in f.upper()])
for t in ["PREDISPATCHSCENARIODEMAND", "PREDISPATCHSCENARIODEMANDTRK"]:
    try:
        df = b._mmsdm_table(data_url, t)
        print("TABLE", t, df.shape, list(df.columns))
        print(df.sort_values(list(df.columns)[:3]).tail(260).to_string()[:20000])
    except Exception as e:
        print("FAIL", t, e)
