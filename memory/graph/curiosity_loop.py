"""Curiosity loop — when idle, research one stale topic via Claude CLI.

Conservative: max 1 invocation per day, only if no active CC sessions in last 30 min,
only if last_curiosity_run was > 23h ago. Picks 1 stale memory topic (least-recently
modified project_*.md), asks `claude -p` for any recent developments, writes note to
memory/daily/curiosity_YYYY-MM-DD.md.

CLI:
  py curiosity_loop.py            # respect gates (idle + cooldown)
  py curiosity_loop.py --force    # ignore gates, run now
  py curiosity_loop.py --dry      # print what would be done

Schedule: append to nightly.bat (runs 03:13 daily — usually idle).
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
DAILY_DIR = MEM_DIR / "daily"
ACTIVITY_DIR = HERE / "activity"
SESSIONS_PATH = ACTIVITY_DIR / "sessions.json"
STATE_PATH = ACTIVITY_DIR / "curiosity_state.json"

IDLE_WINDOW = 1800  # 30 min
COOLDOWN = 23 * 3600  # 23h


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_run": 0, "topics_done": []}


def save_state(s):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def is_idle():
    """No CC session activity in last IDLE_WINDOW seconds."""
    if not SESSIONS_PATH.exists():
        return True
    try:
        data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return True
    sessions = data.get("sessions", {})
    now = time.time()
    for s in sessions.values():
        last = s.get("last_event_at", 0)
        if (now - last) < IDLE_WINDOW:
            return False
    return True


def pick_topic(state):
    """Least-recently-modified project_*.md whose name not in recent topics_done."""
    candidates = []
    for p in MEM_DIR.glob("project_*.md"):
        candidates.append((p.stat().st_mtime, p))
    candidates.sort()  # oldest first
    recent_done = set(state.get("topics_done", [])[-10:])
    for _, p in candidates:
        if p.stem not in recent_done:
            return p
    # all recently done — fall back to oldest
    return candidates[0][1] if candidates else None


def call_claude(prompt: str, timeout=240) -> str | None:
    try:
        r = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="ignore",
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        print(f"claude rc={r.returncode}: {r.stderr[:200]}", file=sys.stderr)
    except FileNotFoundError:
        print("claude CLI not on PATH", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("claude timed out", file=sys.stderr)
    except Exception as e:
        print(f"claude failed: {e}", file=sys.stderr)
    return None


def build_prompt(topic_path: Path) -> str:
    """Read topic frontmatter + first 2000 chars, ask for recent developments."""
    raw = topic_path.read_text(encoding="utf-8", errors="ignore")[:2500]
    return (
        "You are scanning for any developments worth knowing about a topic in my memory. "
        "Read the topic snapshot below, then use WebSearch (if available) to look for "
        "news/releases/breaking changes from the last 7 days that would matter for this "
        "project. If nothing relevant found, say 'no notable updates' and stop. "
        "If something found, output a short markdown note: 1 H2 title, 3-6 bullets, "
        "links if you have them. Keep under 200 words. No preamble.\n\n"
        f"=== topic: {topic_path.stem} ===\n{raw}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    state = load_state()
    now = time.time()

    # gate 1: cooldown
    if not args.force:
        elapsed = now - state.get("last_run", 0)
        if elapsed < COOLDOWN:
            print(f"cooldown: {int((COOLDOWN-elapsed)/3600)}h remaining")
            return

    # gate 2: idle
    if not args.force and not is_idle():
        print("active sessions detected, skipping")
        return

    topic = pick_topic(state)
    if not topic:
        print("no topics found")
        return
    print(f"picked topic: {topic.stem}")

    if args.dry:
        print(f"would call claude -p with prompt for {topic.stem}")
        return

    prompt = build_prompt(topic)
    response = call_claude(prompt)
    if not response:
        print("no response from claude — aborting")
        return

    # write note
    today = date.today().isoformat()
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    note_path = DAILY_DIR / f"curiosity_{today}.md"
    header = (
        f"---\n"
        f"date: {today}\n"
        f"topic: {topic.stem}\n"
        f"source: curiosity_loop\n"
        f"---\n\n"
    )
    note_path.write_text(header + response + "\n", encoding="utf-8")
    print(f"wrote {note_path}")

    # update state
    state["last_run"] = int(now)
    state.setdefault("topics_done", []).append(topic.stem)
    state["topics_done"] = state["topics_done"][-50:]
    save_state(state)


if __name__ == "__main__":
    main()
