"""Phase 8a — daily state snapshot.

Writes memory/graph/state/YYYY-MM-DD.json with diffable summary:
  - graph topology: counts by node type, edges count
  - active goals + progress
  - open inbox tasks
  - last 20 decisions
  - last 20 anomalies
  - per-project session/cost rollup (today)
  - active ext-agents (kinds present)

Diff two snapshots: `py snapshot.py --diff 2026-05-15 2026-05-16`

CLI:
  py snapshot.py            # write today
  py snapshot.py --date 2026-05-14
  py snapshot.py --diff 2026-05-15 2026-05-16
  py snapshot.py --list
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
STATE_DIR = HERE / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
ACT_DIR = HERE / "activity"
GOALS_DIR = MEM_DIR / "goals"


def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_jsonl(p, last_n=None):
    if not p.exists():
        return []
    rows = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    if last_n:
        rows = rows[-last_n:]
    return rows


def load_yaml(p):
    """Minimal YAML reader for goals/active.yaml — depends on PyYAML if present, else manual."""
    if not p.exists():
        return []
    try:
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8")) or []
    except ImportError:
        return []
    except Exception:
        return []


def gather():
    snap = {
        "date": time.strftime("%Y-%m-%d"),
        "ts": int(time.time()),
        "graph": {},
        "goals": [],
        "inbox_open": [],
        "decisions_recent": [],
        "anomalies_recent": [],
        "project_rollup_today": {},
        "ext_agents": {},
    }
    # graph topology
    g = load_json(MEM_DIR / "graph.json", {"nodes": [], "links": []})
    counts = {}
    for n in g.get("nodes", []):
        t = n.get("type", "?")
        counts[t] = counts.get(t, 0) + 1
    snap["graph"] = {"nodes_by_type": counts, "node_total": len(g.get("nodes", [])), "edge_total": len(g.get("links", []))}
    # goals (active.yaml: {generated_at, goals: {gid: {...}}})
    yroot = load_yaml(GOALS_DIR / "active.yaml") or {}
    g_dict = (yroot.get("goals") if isinstance(yroot, dict) else None) or {}
    for gid, goal in g_dict.items():
        if not isinstance(goal, dict):
            continue
        snap["goals"].append({
            "id": gid,
            "status": goal.get("status"),
            "priority": goal.get("priority"),
            "progress": goal.get("progress", 0),
            "blockers": len(goal.get("blockers") or []),
        })
    # inbox open
    inbox = load_jsonl(MEM_DIR / "_inbox.jsonl")
    snap["inbox_open"] = [
        {"id": i["id"], "project": i.get("project"), "text": (i.get("text") or "")[:100]}
        for i in inbox if i.get("status") == "open"
    ]
    # decisions
    snap["decisions_recent"] = load_jsonl(ACT_DIR / "decisions.jsonl", last_n=20)
    # anomalies
    snap["anomalies_recent"] = load_jsonl(ACT_DIR / "anomalies.jsonl", last_n=20)
    # project rollup today (from sessions.json daily totals if present)
    sessions = load_json(ACT_DIR / "sessions.json", {})
    if sessions and "sessions" in sessions:
        today = time.strftime("%Y-%m-%d")
        per = {}
        for sid, s in sessions["sessions"].items():
            pid = s.get("project_id") or "_unassigned"
            d = per.setdefault(pid, {"sessions": 0, "tools": 0, "cost_usd": 0.0})
            d["sessions"] += 1
            d["tools"] += s.get("tool_count", 0) or 0
        snap["project_rollup_today"] = per
    # ext-agents snapshot
    procs = load_json(ACT_DIR / "processes.json", {"agents": [], "counts_by_kind": {}})
    snap["ext_agents"] = {
        "counts_by_kind": procs.get("counts_by_kind", {}),
        "active": [
            {"kind": a.get("agent_kind"), "pid": a.get("pid"), "project": a.get("project_id")}
            for a in procs.get("agents", [])[:20]
        ],
    }
    return snap


def write_snapshot(date):
    snap = gather()
    if date:
        snap["date"] = date
    out = STATE_DIR / f"{snap['date']}.json"
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return snap


def diff_snapshots(date_a, date_b):
    fa = STATE_DIR / f"{date_a}.json"
    fb = STATE_DIR / f"{date_b}.json"
    if not fa.exists() or not fb.exists():
        print(f"missing snapshot(s): {date_a}={fa.exists()} {date_b}={fb.exists()}")
        sys.exit(2)
    a = json.loads(fa.read_text(encoding="utf-8"))
    b = json.loads(fb.read_text(encoding="utf-8"))
    print(f"\n=== diff {date_a} -> {date_b} ===")
    # graph deltas
    ag, bg = a.get("graph", {}), b.get("graph", {})
    print(f"\nnodes: {ag.get('node_total',0)} -> {bg.get('node_total',0)}  edges: {ag.get('edge_total',0)} -> {bg.get('edge_total',0)}")
    a_types = ag.get("nodes_by_type", {})
    b_types = bg.get("nodes_by_type", {})
    for t in sorted(set(a_types) | set(b_types)):
        delta = b_types.get(t, 0) - a_types.get(t, 0)
        if delta:
            sign = "+" if delta > 0 else ""
            print(f"  {t}: {a_types.get(t,0)} -> {b_types.get(t,0)} ({sign}{delta})")
    # goals deltas
    a_goals = {g["id"]: g for g in a.get("goals", [])}
    b_goals = {g["id"]: g for g in b.get("goals", [])}
    for gid in sorted(set(a_goals) | set(b_goals)):
        ga, gb = a_goals.get(gid), b_goals.get(gid)
        if not ga: print(f"  goal NEW {gid} ({gb.get('status')} p{gb.get('progress',0)})")
        elif not gb: print(f"  goal GONE {gid}")
        else:
            d_pct = (gb.get("progress",0) or 0) - (ga.get("progress",0) or 0)
            if d_pct or ga.get("status") != gb.get("status"):
                print(f"  goal {gid}: {ga.get('status')}/{ga.get('progress',0)}% -> {gb.get('status')}/{gb.get('progress',0)}% ({d_pct:+d})")
    # decisions/anomalies counts
    print(f"\ndecisions_recent: {len(a.get('decisions_recent',[]))} -> {len(b.get('decisions_recent',[]))}")
    print(f"anomalies_recent: {len(a.get('anomalies_recent',[]))} -> {len(b.get('anomalies_recent',[]))}")
    # ext agents
    a_kinds = a.get("ext_agents", {}).get("counts_by_kind", {})
    b_kinds = b.get("ext_agents", {}).get("counts_by_kind", {})
    for k in sorted(set(a_kinds) | set(b_kinds)):
        if a_kinds.get(k,0) != b_kinds.get(k,0):
            print(f"  ext_agent {k}: {a_kinds.get(k,0)} -> {b_kinds.get(k,0)}")


def list_snapshots():
    files = sorted(STATE_DIR.glob("*.json"))
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            print(f"  {f.stem}  nodes={d.get('graph',{}).get('node_total',0)}  goals={len(d.get('goals',[]))}  inbox={len(d.get('inbox_open',[]))}")
        except Exception:
            print(f"  {f.stem}  (parse err)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="override date (YYYY-MM-DD)")
    ap.add_argument("--diff", nargs=2, metavar=("DATE_A", "DATE_B"))
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        list_snapshots()
    elif args.diff:
        diff_snapshots(args.diff[0], args.diff[1])
    else:
        write_snapshot(args.date)


if __name__ == "__main__":
    main()
