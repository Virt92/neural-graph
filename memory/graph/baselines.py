"""Phase 5b — per-project baseline aggregator.

Walks memory/daily/*.json sessions[], groups by project_id, computes:
  - mean/std for: cost_usd, msg_count, tok_total, tools_count
  - seen_tools set (all tools ever used in this project)
  - seen_mcps set (all mcp__* tool names)
  - tool_freq distribution (normalized counts)

Output: memory/graph/activity/baselines.json
{
  "generated_at": "...",
  "n_days": int,
  "projects": {
     proj_id: {n_sessions, n_days, cost_mean, cost_std, msg_mean, msg_std,
               tok_mean, tok_std, tools_mean, tools_std,
               seen_tools: [...], seen_mcps: [...],
               tool_freq: {tool: pct}}
  },
  "global": {...same shape, all projects pooled...}
}

CLI:
  py baselines.py
  py baselines.py --print proj_example
"""

import argparse
import json
import math
import os
import statistics
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
DAILY_DIR = MEM_DIR / "daily"
ACT_DIR = HERE / "activity"
ACT_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = ACT_DIR / "baselines.json"

# Opus 4.6 pricing per 1M tokens
PRICE_IN = 15.0
PRICE_OUT = 75.0
PRICE_CC = 18.75
PRICE_CR = 1.50


def cost_of(s):
    return (
        s.get("tok_in", 0) * PRICE_IN
        + s.get("tok_out", 0) * PRICE_OUT
        + s.get("cache_create", 0) * PRICE_CC
        + s.get("cache_read", 0) * PRICE_CR
    ) / 1_000_000.0


def safe_std(xs):
    if len(xs) < 2:
        return 0.0
    try:
        return statistics.stdev(xs)
    except statistics.StatisticsError:
        return 0.0


def aggregate(sessions):
    """Take list of session dicts, return baseline stats dict."""
    if not sessions:
        return None
    costs = [cost_of(s) for s in sessions]
    msgs = [s.get("msg_count", 0) for s in sessions]
    toks = [s.get("tok_in", 0) + s.get("tok_out", 0) for s in sessions]
    tools_per = [sum((s.get("tools") or {}).values()) for s in sessions]

    seen_tools = set()
    seen_mcps = set()
    tool_counts = {}
    for s in sessions:
        tools = s.get("tools") or {}
        for t, c in tools.items():
            seen_tools.add(t)
            if t.startswith("mcp__"):
                # extract mcp server name: mcp__servername__action
                parts = t.split("__")
                if len(parts) >= 2:
                    seen_mcps.add(parts[1])
            tool_counts[t] = tool_counts.get(t, 0) + c
    total = sum(tool_counts.values()) or 1
    tool_freq = {t: round(c / total, 4) for t, c in tool_counts.items()}

    return {
        "n_sessions": len(sessions),
        "cost_mean": round(statistics.mean(costs), 4),
        "cost_std": round(safe_std(costs), 4),
        "msg_mean": round(statistics.mean(msgs), 2),
        "msg_std": round(safe_std(msgs), 2),
        "tok_mean": round(statistics.mean(toks), 1),
        "tok_std": round(safe_std(toks), 1),
        "tools_mean": round(statistics.mean(tools_per), 2),
        "tools_std": round(safe_std(tools_per), 2),
        "seen_tools": sorted(seen_tools),
        "seen_mcps": sorted(seen_mcps),
        "tool_freq": dict(sorted(tool_freq.items(), key=lambda x: -x[1])[:50]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="print_pid", help="print baseline for one project")
    args = ap.parse_args()

    if not DAILY_DIR.exists():
        print("no daily/ dir")
        return

    daily_files = sorted(DAILY_DIR.glob("*.json"))
    if not daily_files:
        print("no daily JSONs")
        return

    by_project = {}
    all_sessions = []
    days_per_project = {}
    for jf in daily_files:
        try:
            d = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        date = d.get("date") or jf.stem
        sessions = d.get("sessions") or []
        proj_today = set()
        for s in sessions:
            pid = s.get("project_id") or "_unassigned"
            by_project.setdefault(pid, []).append(s)
            all_sessions.append(s)
            proj_today.add(pid)
        for pid in proj_today:
            days_per_project[pid] = days_per_project.get(pid, 0) + 1

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_days": len(daily_files),
        "projects": {},
    }
    for pid, sess in by_project.items():
        agg = aggregate(sess)
        if agg:
            agg["n_days"] = days_per_project.get(pid, 0)
            agg["reliable"] = agg["n_days"] >= 7
            out["projects"][pid] = agg
    out["global"] = aggregate(all_sessions)
    if out["global"]:
        out["global"]["n_days"] = len(daily_files)
        out["global"]["reliable"] = len(daily_files) >= 7

    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, OUT_PATH)

    if args.print_pid:
        p = out["projects"].get(args.print_pid)
        if not p:
            print(f"no baseline for {args.print_pid}")
            return
        print(json.dumps(p, ensure_ascii=False, indent=2))
        return

    print(f"baselines: {len(out['projects'])} projects, {out['n_days']} days")
    for pid, p in sorted(out["projects"].items(), key=lambda x: -x[1]["n_sessions"])[:10]:
        flag = "" if p["reliable"] else " [thin]"
        print(f"  {pid:<30}  n={p['n_sessions']:<4} ${p['cost_mean']:<6.2f}/sess  tools={len(p['seen_tools'])}{flag}")


if __name__ == "__main__":
    main()
