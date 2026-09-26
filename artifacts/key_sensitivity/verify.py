"""Recompute the answer-key sensitivity results (paper Appendix, Table "keysens") from this folder."""
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
load = lambda n: json.load(open(HERE / n))["records"]


def summarise(rows, label):
    by_sys = defaultdict(lambda: [0, 0, 0])
    agree = 0
    for r in rows:
        a, b = r["released_key"]["strict"], r["perturbed_key"]["strict"]
        s = by_sys[r["system"]]
        s[0] += a; s[1] += b; s[2] += 1
        agree += a == b
    print(f"\n{label}: {len(rows)} system-item pairs, strict agreement {agree}/{len(rows)} = {100 * agree / len(rows):.1f}%")
    for sys_, (a, b, n) in sorted(by_sys.items()):
        print(f"  {sys_:20s} released {100 * a / n:5.1f}%  perturbed {100 * b / n:5.1f}%  delta {100 * (b - a) / n:+5.1f}pp  (n={n})")


dnv = [r for r in load("dnv_rejudge.json") if r["in_paper_analysis"]]
summarise(dnv, "DNV, SME-accepted merge/split/reword")

basel = load("basel_rejudge.json")
ops = {k["item_id"]: k["op"] for k in load("basel_perturbed_keys.json")}
summarise(basel, "Basel, all 281 items (table columns)")
for op in ("recomp", "compress", "dilute"):
    summarise([r for r in basel if ops[r["item_id"]] == op], f"Basel, {op}")
