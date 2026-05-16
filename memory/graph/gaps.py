"""Phase 9 — uncertainty / "don't-know" detector.

Scans recent assistant turns in JSONL transcripts for hedging/uncertainty markers.
Builds activity/gaps.jsonl: each entry = {ts, session_id, project_id, snippet, marker, score}.

Markers (en/ru):
  - "i don't know", "not sure", "unclear", "can't tell", "no idea"
  - "не уверен", "не знаю", "не понятно", "не могу сказать"
  - "may be", "might be", "possibly", "presumably", "appears to"
  - "needs verification", "should verify", "verify before"

Aggregated: top gaps by project surfaced in graph as `type=gap` nodes (size by frequency).

CLI:
  py gaps.py            # incremental scan + write
  py gaps.py --rebuild  # full rescan
  py gaps.py --top 20   # print top recent gaps
"""

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
ACT_DIR = HERE / "activity"
ACT_DIR.mkdir(parents=True, exist_ok=True)
GAPS_FILE = ACT_DIR / "gaps.jsonl"
STATE_FILE = ACT_DIR / "_gaps_state.json"
HOME = Path(os.path.expanduser("~"))
PROJECTS_ROOT = HOME / ".claude" / "projects"

# Project tag map (mirror decisions.py).
# Add (substring_in_path_lowercase, project_id) tuples for your projects.
PROJECT_TAGS = [
    ("neural-graph", "proj_neural_graph"), ("memory", "proj_neural_graph"),
]

UNCERTAINTY_MARKERS = [
    # English strong
    (re.compile(r"\bi\s+(?:don'?t|do not)\s+know\b", re.I), "dont_know", 1.0),
    (re.compile(r"\bnot\s+sure\b", re.I), "not_sure", 0.7),
    (re.compile(r"\bunclear\b", re.I), "unclear", 0.7),
    (re.compile(r"\bno\s+idea\b", re.I), "no_idea", 1.0),
    (re.compile(r"\bcan'?t\s+tell\b", re.I), "cant_tell", 0.8),
    (re.compile(r"\bunsure\b", re.I), "unsure", 0.7),
    (re.compile(r"\bneed(?:s)?\s+to\s+verify\b", re.I), "needs_verify", 0.6),
    (re.compile(r"\bshould\s+verify\b", re.I), "should_verify", 0.5),
    # English hedge
    (re.compile(r"\bmight\s+be\b", re.I), "might", 0.4),
    (re.compile(r"\bpresumably\b", re.I), "presumably", 0.5),
    (re.compile(r"\bappears?\s+to\b", re.I), "appears_to", 0.4),
    # Russian/Ukrainian
    (re.compile(r"\bне\s+(?:уверен|знаю|понятно|могу\s+сказать)\b", re.I), "ru_unsure", 0.9),
    (re.compile(r"\bне\s+впевнен\b", re.I), "ua_unsure", 0.9),
    (re.compile(r"\bвозможно\b", re.I), "ru_maybe", 0.4),
]


def infer_project(*texts):
    for t in texts:
        if not t:
            continue
        low = t.lower().replace("\\", "/")
        for tag, pid in PROJECT_TAGS:
            if tag in low:
                return pid
    return None


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"offsets": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def append_gaps(items):
    if not items:
        return
    with GAPS_FILE.open("a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def gap_id(sid, ts, snippet):
    return hashlib.sha1(f"{sid}|{ts}|{snippet}".encode("utf-8", errors="ignore")).hexdigest()[:12]


def scan_text(text):
    """Yield (marker, score) for matches in text."""
    out = []
    for pat, name, score in UNCERTAINTY_MARKERS:
        if pat.search(text):
            out.append((name, score))
    return out


def process_jsonl(path, offset, sid, project_hint):
    """Walk JSONL from offset, return (gaps_list, new_offset)."""
    gaps = []
    try:
        st = path.stat()
        if st.st_size <= offset:
            return gaps, offset
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read().decode("utf-8", errors="replace")
        new_off = st.st_size
        for ln in data.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                m = json.loads(ln)
            except Exception:
                continue
            if m.get("type") != "assistant":
                continue
            msg = m.get("message") or {}
            content = msg.get("content")
            text_parts = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for c in content:
                    if c.get("type") == "text":
                        text_parts.append(c.get("text") or "")
            text = " ".join(text_parts)
            if not text or len(text) < 30:
                continue
            ts = m.get("timestamp") or int(time.time())
            cwd_field = m.get("cwd") or msg.get("cwd")
            pid = infer_project(cwd_field, project_hint, text)
            for marker, score in scan_text(text):
                # extract small window around match for context
                snippet = text[:200]
                gaps.append({
                    "id": gap_id(sid, ts, marker),
                    "ts": ts,
                    "session_id": sid,
                    "project_id": pid,
                    "marker": marker,
                    "score": score,
                    "snippet": snippet,
                })
        return gaps, new_off
    except Exception:
        return gaps, offset


def scan_all(rebuild=False):
    state = {"offsets": {}} if rebuild else load_state()
    if rebuild and GAPS_FILE.exists():
        GAPS_FILE.unlink()
    if not PROJECTS_ROOT.exists():
        return 0
    seen_ids = set()
    if not rebuild and GAPS_FILE.exists():
        for ln in GAPS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                seen_ids.add(json.loads(ln)["id"])
            except Exception:
                pass
    total = 0
    for proj_dir in PROJECTS_ROOT.iterdir():
        if not proj_dir.is_dir():
            continue
        decoded = proj_dir.name
        # gather session jsonl + subagent jsonl
        targets = []
        for f in proj_dir.iterdir():
            if f.is_file() and f.name.endswith(".jsonl"):
                targets.append((f.stem, f, decoded))
            elif f.is_dir():
                sub = f / "subagents"
                if sub.exists():
                    for sf in sub.iterdir():
                        if sf.suffix == ".jsonl":
                            targets.append((f.name + ":" + sf.stem, sf, decoded))
        for sid, fp, hint in targets:
            key = str(fp)
            offset = state["offsets"].get(key, 0)
            new_gaps, new_off = process_jsonl(fp, offset, sid, hint)
            new_gaps = [g for g in new_gaps if g["id"] not in seen_ids]
            for g in new_gaps:
                seen_ids.add(g["id"])
            append_gaps(new_gaps)
            state["offsets"][key] = new_off
            total += len(new_gaps)
    save_state(state)
    return total


def print_top(n=20):
    if not GAPS_FILE.exists():
        print("no gaps yet")
        return
    rows = []
    for ln in GAPS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    rows.sort(key=lambda r: r["ts"], reverse=True)
    for r in rows[:n]:
        proj = r.get("project_id") or "-"
        print(f"  [{r['marker']:<14}] {proj:<20}  {r['snippet'][:80]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--top", type=int, default=0)
    args = ap.parse_args()
    if args.top:
        print_top(args.top)
        return
    n = scan_all(rebuild=args.rebuild)
    print(f"added {n} gap entries -> {GAPS_FILE}")


if __name__ == "__main__":
    main()
