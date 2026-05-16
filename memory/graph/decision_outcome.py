"""Mark a past decision with outcome (success/fail/partial) + note.

CLI:
  py decision_outcome.py <id_prefix> success "shipped, no regressions"
  py decision_outcome.py <id_prefix> fail "rolled back after 2 days"
  py decision_outcome.py --list                 # list recent decisions w/ ids
  py decision_outcome.py --pending              # only un-tagged decisions

Mutates: memory/graph/activity/decisions.jsonl (rewrites file)
Appends: memory/graph/activity/decision_outcomes.jsonl (audit log)
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACT_DIR = HERE / "activity"
SRC_PATH = ACT_DIR / "decisions.jsonl"
LOG_PATH = ACT_DIR / "decision_outcomes.jsonl"

VALID = {"success", "fail", "partial", "clear"}


def load_all():
    if not SRC_PATH.exists():
        return []
    out = []
    for ln in SRC_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def write_all(items):
    tmp = SRC_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for d in items:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    os.replace(tmp, SRC_PATH)


def find_match(items, prefix):
    matches = [d for d in items if d.get("id", "").startswith(prefix)]
    return matches


def list_decisions(items, only_pending=False, limit=20):
    rows = items[-limit:] if not only_pending else [d for d in items if not d.get("outcome")][-limit:]
    for d in rows:
        tag = d.get("outcome") or "-"
        print(f"{d['id'][:10]}  [{d.get('date')}]  [{d.get('project_id') or '?'}]  [{tag}]  {d['decision_text'][:100]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("id_prefix", nargs="?", help="decision id prefix (>=6 chars)")
    ap.add_argument("outcome", nargs="?", choices=sorted(VALID), help="outcome label")
    ap.add_argument("note", nargs="?", default=None, help="freeform outcome note")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    items = load_all()
    if not items:
        print("no decisions in log")
        return

    if args.list or args.pending:
        list_decisions(items, only_pending=args.pending, limit=args.limit)
        return

    if not args.id_prefix or not args.outcome:
        ap.print_help()
        sys.exit(1)

    if len(args.id_prefix) < 6:
        print("id_prefix must be >=6 chars to avoid collisions")
        sys.exit(2)

    matches = find_match(items, args.id_prefix)
    if not matches:
        print(f"no decision id starting with {args.id_prefix!r}")
        sys.exit(3)
    if len(matches) > 1:
        print(f"ambiguous prefix, {len(matches)} matches:")
        for m in matches:
            print(f"  {m['id']}  {m['decision_text'][:80]}")
        sys.exit(4)

    d = matches[0]
    today = time.strftime("%Y-%m-%d")

    if args.outcome == "clear":
        d["outcome"] = None
        d["outcome_date"] = None
        d["outcome_note"] = None
    else:
        d["outcome"] = args.outcome
        d["outcome_date"] = today
        d["outcome_note"] = args.note

    write_all(items)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": d["id"],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "outcome": d.get("outcome"),
            "note": args.note,
        }, ensure_ascii=False) + "\n")

    print(f"tagged {d['id']} -> {d['outcome']}: {d['decision_text'][:80]}")


if __name__ == "__main__":
    main()
