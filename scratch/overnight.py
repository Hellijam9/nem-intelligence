"""Overnight bid stack read: trading day D, 21:00 D -> 04:00 D+1, vs the prior 7 nights."""
import sys
from datetime import datetime, timedelta
sys.path.insert(0, "scripts")
import numpy as np
import pandas as pd
import bid_stack_analysis as b

TARGET = "20261006"
N_BASE = 7
VRE = {"Wind", "Solar"}


def night(day_str):
    day = datetime.strptime(day_str, "%Y%m%d")
    day_df, per = b.load_bids(day_str)
    disp = b.load_dispatch(day_str)
    regions = b.load_regions(day_str)
    reg = b.registry_table(day_df, disp)
    st = b.build_stack(day_df, per, disp, reg)
    lo, hi = day.replace(hour=21), day + timedelta(days=1, hours=4)
    st = st[(st["T"] > lo) & (st["T"] <= hi)].copy()
    regions["T"] = pd.to_datetime(regions["INTERVAL_DATETIME"], format=b.TS_FMT)
    rg = regions[(regions["T"] > lo) & (regions["T"] <= hi)].copy()
    return st, rg, disp


def portfolio_table(st):
    n = st["T"].nunique()
    d = st[~st["FUEL"].isin(VRE)]
    k = ["REGIONID", "PORTFOLIO"]
    f = lambda m: d[m].groupby(k)["MW"].sum() / n
    t = pd.DataFrame({
        "avail": f(d["PRICE"] > -1e9), "neg": f(d["PRICE"] < 0), "lt100": f(d["PRICE"] < 100),
        "hi300": f(d["PRICE"] >= 300), "hi5k": f(d["PRICE"] >= 5000),
    }).fillna(0)
    t["fuels"] = d.groupby(k)["FUEL"].agg(lambda s: "/".join(sorted(set(s))))
    return t


def marginal(st, rg, disp):
    """Unit at the local-stack crossing of dispatched generation, kept only where it matches spot."""
    out = []
    for (r, t), g in st.groupby(["REGIONID", "T"]):
        row = rg[(rg["REGIONID"] == r) & (rg["T"] == t)]
        if row.empty:
            continue
        q, rrp = float(row["DISPATCHABLEGENERATION"].iloc[0]), float(row["RRP"].iloc[0])
        g = g.sort_values("PRICE")
        i = int(np.searchsorted(g["MW"].cumsum().to_numpy(), q))
        m = g.iloc[min(i, len(g) - 1)]
        if abs(m["PRICE"] - rrp) <= max(10, 0.1 * abs(rrp)):
            out.append((r, m["PORTFOLIO"], m["FUEL"], m["DUID"], round(m["PRICE"])))
    return pd.DataFrame(out, columns=["REGIONID", "PORTFOLIO", "FUEL", "DUID", "PRICE"])


tgt = datetime.strptime(TARGET, "%Y%m%d")
st, rg, disp = night(TARGET)
pt = portfolio_table(st)
mg = marginal(st, rg, disp)

base_tabs, base_rg, base_mg = [], [], []
for k in range(1, N_BASE + 1):
    d = (tgt - timedelta(days=k)).strftime("%Y%m%d")
    try:
        s2, r2, d2 = night(d)
        base_tabs.append(portfolio_table(s2))
        base_rg.append(r2.assign(day=d))
        base_mg.append(marginal(s2, r2, d2))
        print(f"base {d} ok", flush=True)
    except Exception as e:
        print(f"base {d} failed: {e}", flush=True)
bt = pd.concat(base_tabs).groupby(level=[0, 1]).mean(numeric_only=True)
brg = pd.concat(base_rg)
bmg = pd.concat(base_mg)

print("\n==== OVERNIGHT 21:00 6 Oct -> 04:00 7 Oct NEM ====")
for r in b.REGIONS:
    x = rg[rg["REGIONID"] == r]; y = brg[brg["REGIONID"] == r]
    s = st[st["REGIONID"] == r]; n = s["T"].nunique()
    sb = s.groupby("BUCKET", observed=False)["MW"].sum() / n
    vre = s[s["FUEL"].isin(VRE)]["MW"].sum() / n
    print(f"\n## {r}: RRP avg {x.RRP.mean():.0f} (7-night avg {y.RRP.mean():.0f}), min {x.RRP.min():.0f}, max {x.RRP.max():.0f}; "
          f"demand {x.TOTALDEMAND.min():.0f}-{x.TOTALDEMAND.max():.0f}; local dispatchable gen avg {x.DISPATCHABLEGENERATION.mean():.0f}; "
          f"net interchange avg {x.NETINTERCHANGE.mean():.0f} (+ = export)")
    print("   offered by band (avg MW): " + ", ".join(f"{k} {v:,.0f}" for k, v in sb.items()) + f" | wind+solar avail {vre:,.0f}")
    p = pt.loc[r] if r in pt.index.get_level_values(0) else pd.DataFrame()
    if len(p):
        q = p.join(bt.loc[r] if r in bt.index.get_level_values(0) else pd.DataFrame(), rsuffix="_b")
        q = q[q["avail"] >= 50].sort_values("avail", ascending=False)
        for name, row in q.iterrows():
            def d(c):
                bv = row.get(c + "_b")
                return "" if pd.isna(bv) else f" ({row[c]-bv:+,.0f})"
            print(f"   {name[:28]:28s} {row['fuels'][:22]:22s} avail {row['avail']:6,.0f}{d('avail'):>8} | <$0 {row['neg']:6,.0f}{d('neg'):>8} | <$100 {row['lt100']:6,.0f}{d('lt100'):>8} | $300+ {row['hi300']:6,.0f}{d('hi300'):>8} | $5k+ {row['hi5k']:6,.0f}{d('hi5k'):>8}")
    m = mg[mg["REGIONID"] == r]; mb = bmg[bmg["REGIONID"] == r]
    if len(m):
        top = m.groupby(["PORTFOLIO", "FUEL"]).size().sort_values(ascending=False).head(5)
        print(f"   local-stack marginal (where it matched spot, {len(m)}/{n} intervals): " +
              "; ".join(f"{a} {f} {c}" for (a, f), c in top.items()))
        if len(mb):
            topb = (mb.groupby(["PORTFOLIO", "FUEL"]).size() / N_BASE).sort_values(ascending=False).head(5)
            print("      prior-7 avg per night: " + "; ".join(f"{a} {f} {c:.0f}" for (a, f), c in topb.items()))
        md = m.groupby("DUID").agg(n=("PRICE", "size"), p=("PRICE", "median")).sort_values("n", ascending=False).head(5)
        print("      top marginal units: " + "; ".join(f"{d} x{int(v.n)} @${v.p:.0f}" for d, v in md.iterrows()))
