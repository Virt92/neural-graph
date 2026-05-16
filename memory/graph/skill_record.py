"""Record skill usage outcome — mutates skill frontmatter + appends jsonl log.

CLI:
  py skill_record.py SKILL_NAME success [--note "deployed cleanly"]
  py skill_record.py SKILL_NAME fail    [--note "gradle OOM"]

Updates skills/learned/<name>.md frontmatter (uses, success, fail, last_used).
Appends to memory/graph/activity/skill_outcomes.jsonl for trend analysis.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
SKILLS_DIR = MEM_DIR / "skills" / "learned"
LOG_PATH = HERE / "activity" / "skill_outcomes.jsonl"

FM_RE = re.compile(r"^(---\s*\n)(.*?)(\n---\s*\n)(.*)$", re.DOTALL)


def update_frontmatter(skill_name: str, outcome: str, note: str | None):
    p = SKILLS_DIR / f"{skill_name}.md"
    if not p.exists():
        # try by frontmatter "name:" match
        for cand in SKILLS_DIR.glob("*.md"):
            txt = cand.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"^name:\s*(\S+)", txt, re.MULTILINE)
            if m and m.group(1) == skill_name:
                p = cand
                break
        else:
            print(f"skill '{skill_name}' not found in {SKILLS_DIR}", file=sys.stderr)
            sys.exit(2)

    raw = p.read_text(encoding="utf-8")
    m = FM_RE.match(raw)
    if not m:
        print(f"no frontmatter in {p}", file=sys.stderr)
        sys.exit(3)

    fm_open, fm, fm_close, body = m.group(1), m.group(2), m.group(3), m.group(4)
    fm_lines = fm.splitlines()

    def get(key, default="0"):
        for line in fm_lines:
            if line.startswith(f"{key}:"):
                return line.split(":", 1)[1].strip()
        return default

    def set_or_add(key, value):
        nonlocal fm_lines
        for i, line in enumerate(fm_lines):
            if line.startswith(f"{key}:"):
                fm_lines[i] = f"{key}: {value}"
                return
        fm_lines.append(f"{key}: {value}")

    uses = int(get("uses") or 0) + 1
    success = int(get("success") or 0) + (1 if outcome == "success" else 0)
    fail = int(get("fail") or 0) + (1 if outcome == "fail" else 0)
    today = time.strftime("%Y-%m-%d")
    set_or_add("uses", uses)
    set_or_add("success", success)
    set_or_add("fail", fail)
    set_or_add("last_used", today)

    new_raw = fm_open + "\n".join(fm_lines) + fm_close + body
    p.write_text(new_raw, encoding="utf-8")
    return p, uses, success, fail


def append_log(skill_name, outcome, note):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": int(time.time()),
        "date": time.strftime("%Y-%m-%d"),
        "skill": skill_name,
        "outcome": outcome,
        "note": note or "",
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("skill")
    ap.add_argument("outcome", choices=["success", "fail"])
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    p, uses, success, fail = update_frontmatter(args.skill, args.outcome, args.note)
    append_log(args.skill, args.outcome, args.note)
    rate = success / max(1, success + fail)
    print(f"{args.skill}: uses={uses} success={success} fail={fail} win={rate*100:.0f}% [{p.name}]")


if __name__ == "__main__":
    main()
