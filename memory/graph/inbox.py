"""Phase 7 — cross-session task inbox.

Append-only queue. Other CC sessions can pick up tasks.

CLI:
  py inbox.py add "rebuild graph after Phase 8b" --project proj_neural_graph --tag urgent
  py inbox.py list                          # all open tasks
  py inbox.py list --project proj_example
  py inbox.py claim <task_id_prefix>        # mark in_progress with my session id
  py inbox.py done <task_id_prefix> "result note"
  py inbox.py drop <task_id_prefix>

File: memory/_inbox.jsonl  (one record per line)
Schema: {id, ts_created, status, project, text, tags, claimed_by, ts_claimed, result, ts_done}
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
INBOX = MEM_DIR / "_inbox.jsonl"


def session_short():
    sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("SESSION_ID") or "local"
    return sid[:8]


def make_id(text):
    return hashlib.sha1((text + str(time.time())).encode("utf-8", errors="ignore")).hexdigest()[:10]


def load_all():
    if not INBOX.exists():
        return []
    out = []
    for ln in INBOX.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def write_all(items):
    tmp = INBOX.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(tmp, INBOX)


def find_one(items, prefix):
    matches = [i for i in items if i["id"].startswith(prefix)]
    if not matches:
        print(f"no task starting with {prefix!r}")
        sys.exit(2)
    if len(matches) > 1:
        print(f"ambiguous prefix; matches:")
        for m in matches:
            print(f"  {m['id']}  {m['text'][:80]}")
        sys.exit(3)
    return matches[0]


def cmd_add(args):
    items = load_all()
    rec = {
        "id": make_id(args.text),
        "ts_created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "open",
        "project": args.project,
        "text": args.text,
        "tags": args.tag or [],
        "claimed_by": None,
        "ts_claimed": None,
        "result": None,
        "ts_done": None,
        "creator": session_short(),
    }
    items.append(rec)
    write_all(items)
    print(f"added {rec['id']}: {args.text[:80]}")


def cmd_list(args):
    items = load_all()
    rows = [i for i in items if i.get("status") in (args.status,) or args.status == "all"]
    if args.project:
        rows = [i for i in rows if i.get("project") == args.project]
    rows.sort(key=lambda x: x["ts_created"])
    for r in rows:
        proj = r.get("project") or "-"
        st = r.get("status", "?")
        claim = r.get("claimed_by") or ""
        ts = r["ts_created"][:16].replace("T", " ")
        print(f"  {r['id']}  [{st:<11}] {ts}  {proj:<20}  {r['text'][:70]}  {claim}")
    print(f"\n{len(rows)} tasks")


def cmd_claim(args):
    items = load_all()
    rec = find_one(items, args.id_prefix)
    if rec["status"] != "open":
        print(f"already {rec['status']} (claimed by {rec.get('claimed_by')})")
        sys.exit(2)
    rec["status"] = "in_progress"
    rec["claimed_by"] = session_short()
    rec["ts_claimed"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_all(items)
    print(f"claimed {rec['id']}: {rec['text'][:80]}")


def cmd_done(args):
    items = load_all()
    rec = find_one(items, args.id_prefix)
    rec["status"] = "done"
    rec["result"] = args.note
    rec["ts_done"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_all(items)
    print(f"done {rec['id']}: {args.note[:80]}")


def cmd_drop(args):
    items = load_all()
    rec = find_one(items, args.id_prefix)
    rec["status"] = "dropped"
    rec["ts_done"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_all(items)
    print(f"dropped {rec['id']}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add"); p.add_argument("text"); p.add_argument("--project"); p.add_argument("--tag", action="append")
    p = sub.add_parser("list"); p.add_argument("--status", default="open"); p.add_argument("--project")
    p = sub.add_parser("claim"); p.add_argument("id_prefix")
    p = sub.add_parser("done"); p.add_argument("id_prefix"); p.add_argument("note")
    p = sub.add_parser("drop"); p.add_argument("id_prefix")
    args = ap.parse_args()
    {"add": cmd_add, "list": cmd_list, "claim": cmd_claim, "done": cmd_done, "drop": cmd_drop}[args.cmd](args)


if __name__ == "__main__":
    main()
