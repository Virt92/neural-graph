"""Phase 8c — causal edge inference.

Detects time-proximate (project, decision -> outcome event) pairs and writes
activity/causal_edges.json with: from, to, kind, gap_minutes, confidence.

Outcome events sources:
  1. anomalies.jsonl — cost_spike/msg_spike/new_tool/etc. after a decision
  2. decisions.jsonl outcome field — direct success/fail tag flips
  3. goals_log.jsonl — goal progress/status changes

Linkage rule: same project_id + outcome_ts within (decision_ts, decision_ts + 24h).

Edges injected into graph by build_graph.py via scan_causal().

CLI:
  py causality.py            # write activity/causal_edges.json
  py causality.py --print
"""

import argparse
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACT_DIR = HERE / "activity"
ACT_DIR.mkdir(parents=True, exist_ok=True)
DEC_FILE = ACT_DIR / "decisions.jsonl"
ANOM_FILE = ACT_DIR / "anomalies.jsonl"
GOAL_LOG = ACT_DIR / "goals_log.jsonl"
OUT_FILE = ACT_DIR / "causal_edges.json"

WINDOW_SEC = 24 * 3600
MIN_GAP_SEC = 60  # at least 1 min after decision


def load_jsonl(p):
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def to_epoch(d):
    """Return epoch seconds from a record's ts/timestamp/date_iso field."""
    for k in ("ts", "timestamp", "ts_done", "ts_claimed", "ts_created"):
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v:
            try:
                return time.mktime(time.strptime(v[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                continue
    # decisions.jsonl has 'date': YYYY-MM-DD
    if d.get("date"):
        try:
            return time.mktime(time.strptime(d["date"], "%Y-%m-%d"))
        except Exception:
            pass
    return 0.0


def detect_pairs():
    decisions = load_jsonl(DEC_FILE)
    anomalies = load_jsonl(ANOM_FILE)
    goal_events = load_jsonl(GOAL_LOG)
    pairs = []
    for d in decisions:
        d_ts = to_epoch(d)
        if not d_ts:
            continue
        d_proj = d.get("project_id")
        d_id = "dec_" + (d.get("id") or "")[:10]
        # anomalies in window with same project
        for a in anomalies:
            a_ts = to_epoch(a)
            if not a_ts:
                continue
            if a.get("project_id") != d_proj:
                continue
            gap = a_ts - d_ts
            if MIN_GAP_SEC < gap < WINDOW_SEC:
                kind = a.get("kind") or "anomaly"
                # confidence: closer in time = stronger; cap at 0.9
                conf = round(max(0.2, 0.9 - gap / WINDOW_SEC * 0.7), 2)
                pairs.append({
                    "from": d_id,
                    "to": d_proj,  # outcome attaches to project node (anomaly is transient)
                    "kind": "decision_caused_" + kind,
                    "gap_minutes": int(gap / 60),
                    "confidence": conf,
                    "evidence": (a.get("detail") or "")[:120],
                })
        # goal events near decision
        for ge in goal_events:
            ge_ts = to_epoch(ge)
            if not ge_ts:
                continue
            if ge.get("project") != d_proj:
                continue
            gap = ge_ts - d_ts
            if MIN_GAP_SEC < gap < WINDOW_SEC:
                op = ge.get("op") or "goal_change"
                conf = round(max(0.2, 0.85 - gap / WINDOW_SEC * 0.6), 2)
                pairs.append({
                    "from": d_id,
                    "to": ge.get("goal_id") or d_proj,
                    "kind": "decision_influenced_" + op,
                    "gap_minutes": int(gap / 60),
                    "confidence": conf,
                    "evidence": (ge.get("note") or "")[:120],
                })
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    pairs = detect_pairs()
    out = {"generated_at": int(time.time()), "edges": pairs}
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(pairs)} causal edges -> {OUT_FILE}")
    if args.print:
        for p in pairs[:20]:
            print(f"  {p['from']} -> {p['to']}  {p['kind']}  +{p['gap_minutes']}m  conf={p['confidence']}")


if __name__ == "__main__":
    main()
