"""Phase 5b — anomaly detector.

Reads latest daily JSON + baselines.json. Per session:
  - cost > μ+3σ          -> z_score_cost
  - msg_count > μ+3σ     -> z_score_msg
  - tools_count > μ+3σ   -> z_score_tools
  - new tool not in seen_tools  -> new_tool list
  - new mcp not in seen_mcps    -> new_mcp list

Baseline must be reliable (n_days>=7). For thin baselines, only emits new_tool/new_mcp.

Output: memory/graph/activity/anomalies.jsonl (append-only)
Each line: {ts, date, session_id, project_id, kind, severity, detail}

CLI:
  py anomalies.py             # scan latest daily
  py anomalies.py --all       # scan all daily files
  py anomalies.py --reset     # wipe anomalies.jsonl
"""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
DAILY_DIR = MEM_DIR / "daily"
ACT_DIR = HERE / "activity"
ACT_DIR.mkdir(parents=True, exist_ok=True)
BL_PATH = ACT_DIR / "baselines.json"
OUT_PATH = ACT_DIR / "anomalies.jsonl"
SEEN_PATH = ACT_DIR / "anomalies_seen.json"

# Opus 4.6 pricing
PRICE_IN = 15.0
PRICE_OUT = 75.0
PRICE_CC = 18.75
PRICE_CR = 1.50

Z_THRESHOLD = 3.0


def cost_of(s):
    return (
        s.get("tok_in", 0) * PRICE_IN
        + s.get("tok_out", 0) * PRICE_OUT
        + s.get("cache_create", 0) * PRICE_CC
        + s.get("cache_read", 0) * PRICE_CR
    ) / 1_000_000.0


def load_baselines():
    if not BL_PATH.exists():
        return None
    try:
        return json.loads(BL_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_seen():
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_seen(seen):
    tmp = SEEN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(seen)[-5000:], ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, SEEN_PATH)


def make_id(*parts):
    s = "|".join(str(p) for p in parts)
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:16]


def z(x, mean, std):
    if std <= 0:
        return 0.0
    return (x - mean) / std


def detect_in_session(session, baselines):
    """Return list of anomaly dicts."""
    out = []
    pid = session.get("project_id") or "_unassigned"
    bl = baselines.get("projects", {}).get(pid)
    glob = baselines.get("global")
    # Use project baseline if reliable, else fall back to global
    use_bl = bl if (bl and bl.get("reliable")) else glob
    sid = session.get("id")

    sess_cost = cost_of(session)
    sess_msg = session.get("msg_count", 0)
    sess_tools = sum((session.get("tools") or {}).values())

    # numeric anomalies — only emit if baseline reliable
    if use_bl and use_bl.get("reliable"):
        zc = z(sess_cost, use_bl["cost_mean"], use_bl["cost_std"])
        if zc >= Z_THRESHOLD:
            out.append({
                "kind": "cost_spike", "severity": round(zc, 2),
                "detail": f"${sess_cost:.2f} vs μ=${use_bl['cost_mean']:.2f} σ=${use_bl['cost_std']:.2f}",
            })
        zm = z(sess_msg, use_bl["msg_mean"], use_bl["msg_std"])
        if zm >= Z_THRESHOLD:
            out.append({
                "kind": "msg_spike", "severity": round(zm, 2),
                "detail": f"{sess_msg} msgs vs μ={use_bl['msg_mean']:.0f} σ={use_bl['msg_std']:.0f}",
            })
        zt = z(sess_tools, use_bl["tools_mean"], use_bl["tools_std"])
        if zt >= Z_THRESHOLD:
            out.append({
                "kind": "tool_spike", "severity": round(zt, 2),
                "detail": f"{sess_tools} tool calls vs μ={use_bl['tools_mean']:.0f}",
            })

    # new tool/mcp — works even on thin baseline (against project's own seen set)
    if bl:
        seen_tools = set(bl.get("seen_tools") or [])
        seen_mcps = set(bl.get("seen_mcps") or [])
        sess_tools_set = set((session.get("tools") or {}).keys())
        new_tools = sess_tools_set - seen_tools
        new_mcps = set()
        for t in new_tools:
            if t.startswith("mcp__"):
                parts = t.split("__")
                if len(parts) >= 2:
                    new_mcps.add(parts[1])
        truly_new_mcps = new_mcps - seen_mcps
        for t in new_tools:
            out.append({"kind": "new_tool", "severity": 1.0, "detail": t})
        for m in truly_new_mcps:
            out.append({"kind": "new_mcp", "severity": 2.0, "detail": m})

    # tag every anomaly with session metadata
    enriched = []
    for a in out:
        a["session_id"] = sid
        a["project_id"] = pid
        enriched.append(a)
    return enriched


def append(items):
    if not items:
        return
    with OUT_PATH.open("a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def scan_daily(jf, baselines, seen):
    try:
        d = json.loads(jf.read_text(encoding="utf-8"))
    except Exception:
        return []
    date = d.get("date") or jf.stem
    new = []
    for s in d.get("sessions") or []:
        for a in detect_in_session(s, baselines):
            aid = make_id(date, a["session_id"], a["kind"], a["detail"])
            if aid in seen:
                continue
            seen.add(aid)
            a["id"] = aid
            a["date"] = date
            a["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            new.append(a)
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="scan all daily files (else: latest only)")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.reset:
        for p in (OUT_PATH, SEEN_PATH):
            if p.exists():
                p.unlink()
        print("anomalies reset")
        return

    baselines = load_baselines()
    if not baselines:
        print("no baselines.json — run baselines.py first")
        return

    daily_files = sorted(DAILY_DIR.glob("*.json"))
    if not daily_files:
        print("no daily JSONs")
        return

    targets = daily_files if args.all else daily_files[-1:]
    seen = load_seen()
    total = 0
    for jf in targets:
        items = scan_daily(jf, baselines, seen)
        if items:
            append(items)
            total += len(items)
            print(f"{jf.stem}: +{len(items)} anomalies")
            for it in items[:10]:
                print(f"  [{it['kind']:<10}] [{it['project_id']}] sev={it['severity']}  {it['detail'][:80]}")
    save_seen(seen)
    print(f"total new anomalies: {total}")


if __name__ == "__main__":
    main()
