"""Decision log extractor (Phase 5a).

Walks ~/.claude/projects/**/*.jsonl, regex-matches decision markers (RU+EN) in
assistant + user messages, extracts decision sentence with surrounding context,
appends to memory/graph/activity/decisions.jsonl.

Incremental: tracks per-file byte offset in decisions_state.json so re-runs only
process new tail. Idempotent (decision_id = sha1(session_id + ts + text[:60])).

CLI:
  py decisions.py              # incremental scan
  py decisions.py --reset      # wipe state + decisions.jsonl
  py decisions.py --dry        # print matches, don't write
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
PROJECTS_ROOT = HOME / ".claude" / "projects"
HERE = Path(__file__).resolve().parent
ACTIVITY_DIR = HERE / "activity"
ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = ACTIVITY_DIR / "decisions.jsonl"
STATE_PATH = ACTIVITY_DIR / "decisions_state.json"

# Decision marker patterns. Each: (regex, label). Match is case-insensitive.
# Designed to match within a single sentence; extraction = whole sentence.
PATTERNS = [
    # RU
    (r"выбрал[аи]?\s+\w+\s+(?:потому|т\.?к\.?|поскольку)", "ru_chose_because"),
    (r"выбираем\s+\w+\s+(?:вместо|а не|поскольку|потому)", "ru_choose_over"),
    (r"реш(?:ил|или|ено)\s+(?:использовать|использую|перейти|применить)", "ru_decided_use"),
    (r"вместо\s+\w+\s+(?:использу|берём|возьм)", "ru_use_instead"),
    (r"(?:не\s+)?используем\s+\w+\s+(?:а\s+не|вместо)", "ru_use_not"),
    (r"будем\s+использовать\s+\w+", "ru_will_use"),
    (r"остановил[аи]?сь\s+на\s+\w+", "ru_settled_on"),
    (r"переход[ия]м\s+(?:с|на)\s+\w+", "ru_switching"),
    # EN
    (r"decided\s+to\s+use\s+\w+", "en_decided_use"),
    (r"going\s+with\s+\w+(?:\s+over\s+\w+)?", "en_going_with"),
    (r"chose\s+\w+\s+over\s+\w+", "en_chose_over"),
    (r"switching\s+(?:from|to)\s+\w+", "en_switching"),
    (r"let'?s\s+use\s+\w+\s+(?:because|since|over)", "en_lets_use"),
    (r"we'?ll\s+go\s+with\s+\w+", "en_we_go_with"),
    (r"picked\s+\w+\s+(?:because|since|over)", "en_picked"),
]
COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in PATTERNS]

# Anti-patterns: skip noise. Kill matches that look like generic chatter.
NEG_PATTERNS = [
    re.compile(r"^(?:ok|sure|yes|no|yeah|alright)\b", re.I),
    re.compile(r"^(?:да|нет|хорошо|ладно)\b", re.I),
]
# Phrase-level anti-patterns: match anywhere in sentence, kill if hit.
NEG_PHRASES = [
    re.compile(r"\bswitching\s+to\s+(?:tab|window|page|browser|app|view|file|directory|dir|branch)\b", re.I),
    re.compile(r"\bswitching\s+from\s+(?:tab|window|page|file)\b", re.I),
    re.compile(r"\bgoing\s+with\s+(?:the\s+)?(?:default|standard|usual|same)\b", re.I),
    re.compile(r"\bdecided\s+to\s+use\s+(?:the\s+)?(?:tool|command|approach)\s*$", re.I),
]
# Junk shapes: code, URL-heavy, JSON-like, markdown list bullets describing past
JUNK_PREFIX = re.compile(r"^[\-\*\>`#]\s")
URL_RE = re.compile(r"https?://\S+")
JSON_LIKE = re.compile(r'^\s*"[^"]+"\s*:')


def looks_like_junk(sent: str) -> bool:
    if JUNK_PREFIX.match(sent):
        return True
    if JSON_LIKE.match(sent):
        return True
    # too URL-heavy
    if len(URL_RE.findall(sent)) >= 1 and len(sent) < 150:
        return True
    return False

# Project tag map (mirror reflect.py).
# Add (substring_in_path_lowercase, project_id) tuples here for your projects.
# Example: ("myapp", "proj_myapp"), ("client_acme", "proj_acme"),
PROJECT_TAGS = [
    ("neural-graph", "proj_neural_graph"), ("memory", "proj_neural_graph"),
]


def decode_cwd(dir_name):
    s = re.sub(r"^([A-Za-z])--", r"\1:\\", dir_name)
    return s.replace("-", "\\")


def infer_project(cwd, raw_dir_name=None, *extra_text):
    haystacks = []
    if cwd:
        haystacks.append(cwd.lower().replace("\\", "/"))
    if raw_dir_name:
        haystacks.append(raw_dir_name.lower().replace("--", "/").replace("-", "/"))
    for t in extra_text:
        if t:
            haystacks.append(t.lower())
    for hay in haystacks:
        for tag, pid in PROJECT_TAGS:
            if tag in hay:
                return pid
    return None


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"offsets": {}, "ids_seen": []}


def save_state(s):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def make_id(sid: str, ts: str, text: str) -> str:
    h = hashlib.sha1(f"{sid}|{ts}|{text[:60]}".encode("utf-8", errors="ignore"))
    return h.hexdigest()[:16]


def split_sentences(text: str):
    """Split on sentence boundaries AND newlines (markdown-aware)."""
    # split on newlines first (markdown), then on sentence ends
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # within a line, split on . ! ?
        for part in re.split(r"(?<=[\.\!\?])\s+", line):
            p = part.strip()
            if p:
                out.append(p)
    return out


def match_decision(text: str):
    """Return list of (sentence, label) for sentences that look like decisions."""
    out = []
    for sent in split_sentences(text):
        if len(sent) < 20 or len(sent) > 600:
            continue
        if any(p.match(sent) for p in NEG_PATTERNS):
            continue
        if any(p.search(sent) for p in NEG_PHRASES):
            continue
        if looks_like_junk(sent):
            continue
        for rx, label in COMPILED:
            if rx.search(sent):
                out.append((sent, label))
                break
    return out


def extract_text_from_message(msg_block):
    """Pull plain text from assistant/user message content (string OR list of parts)."""
    if not msg_block:
        return ""
    content = msg_block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text") or ""
                if t:
                    chunks.append(t)
        return "\n".join(chunks)
    return ""


def parse_jsonl(path: Path, default_project_id: str, raw_dir_name: str, offset: int, ids_seen: set):
    """Yields decisions starting at byte offset. Returns new offset."""
    new_decisions = []
    sid = path.stem
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
            new_offset = offset + len(data)
    except Exception as e:
        print(f"read err {path}: {e}", file=sys.stderr)
        return [], offset

    text = data.decode("utf-8", errors="ignore")
    # buffer of recent message texts for context_before
    recent = []
    last_cwd = None  # per-message cwd if present
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        # cwd may be top-level or inside message
        cwd_field = m.get("cwd") or (m.get("message") or {}).get("cwd")
        if cwd_field:
            last_cwd = cwd_field
        ts_str = m.get("timestamp") or m.get("ts") or m.get("created_at")
        if not ts_str:
            continue
        msg_type = m.get("type")
        if msg_type not in ("user", "assistant"):
            continue
        msg = m.get("message") or {}
        body = extract_text_from_message(msg)
        if not body:
            recent.append({"type": msg_type, "text": ""})
            recent[:] = recent[-3:]
            continue

        matches = match_decision(body)
        if matches:
            ctx_before = " | ".join(r["text"][:200] for r in recent[-2:] if r.get("text"))
            for sent, label in matches:
                did = make_id(sid, ts_str, sent)
                if did in ids_seen:
                    continue
                ids_seen.add(did)
                # project: per-message cwd > default > content scan (sent + ctx)
                pid = infer_project(last_cwd, raw_dir_name, sent, ctx_before) or default_project_id
                new_decisions.append({
                    "id": did,
                    "ts": ts_str,
                    "date": ts_str[:10],
                    "session_id": sid,
                    "project_id": pid,
                    "speaker": msg_type,
                    "decision_text": sent,
                    "match_pattern": label,
                    "context_before": ctx_before,
                    "outcome": None,
                    "outcome_date": None,
                    "outcome_note": None,
                })
        recent.append({"type": msg_type, "text": body[:300]})
        recent[:] = recent[-3:]

    return new_decisions, new_offset


def append_decisions(decisions):
    if not decisions:
        return
    with OUT_PATH.open("a", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    if args.reset:
        for p in (STATE_PATH, OUT_PATH):
            if p.exists():
                p.unlink()
        print("decisions reset")
        return

    if not PROJECTS_ROOT.exists():
        print("no projects dir found")
        return

    state = load_state()
    offsets = state.get("offsets", {})
    ids_seen = set(state.get("ids_seen", []))

    total_new = 0
    files_scanned = 0
    for proj_dir in PROJECTS_ROOT.iterdir():
        if not proj_dir.is_dir():
            continue
        cwd = decode_cwd(proj_dir.name)
        project_id = infer_project(cwd, raw_dir_name=proj_dir.name)

        for jsonl_path in proj_dir.rglob("*.jsonl"):
            files_scanned += 1
            key = str(jsonl_path)
            prev_off = offsets.get(key, 0)
            try:
                size = jsonl_path.stat().st_size
            except OSError:
                continue
            if size <= prev_off:
                continue  # nothing new
            new_decisions, new_off = parse_jsonl(jsonl_path, project_id, proj_dir.name, prev_off, ids_seen)
            offsets[key] = new_off
            if new_decisions:
                total_new += len(new_decisions)
                if args.dry:
                    for d in new_decisions:
                        print(f"[{d['project_id'] or '?'}] {d['decision_text'][:120]}")
                else:
                    append_decisions(new_decisions)

    if not args.dry:
        state["offsets"] = offsets
        state["ids_seen"] = list(ids_seen)[-5000:]
        save_state(state)

    print(f"scanned {files_scanned} files, {total_new} new decisions")


if __name__ == "__main__":
    main()
