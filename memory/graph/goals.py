"""Phase 8b — goal hierarchy CRUD.

Reads/writes memory/goals/active.yaml. Supports list/show/add-subgoal/progress/status/done.

CLI:
  py goals.py list                              # all goals (compact)
  py goals.py list --status active --project proj_example
  py goals.py show <goal_id>
  py goals.py progress <goal_id> 75
  py goals.py status <goal_id> done|paused|active|dropped
  py goals.py block <goal_id> "blocker text"    # append blocker
  py goals.py unblock <goal_id> <index>         # remove blocker by index
  py goals.py sub-done <goal_id> <subgoal_idx>  # mark subgoal done w/ today's date
  py goals.py sub-add <goal_id> "title"         # append pending subgoal
  py goals.py history <goal_id>                 # show changelog
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
GOALS_DIR = MEM_DIR / "goals"
GOALS_FILE = GOALS_DIR / "active.yaml"
LOG_FILE = HERE / "activity" / "goals_log.jsonl"

VALID_STATUS = {"active", "paused", "done", "dropped"}
VALID_SUB_STATUS = {"pending", "in_progress", "done"}


def load():
    if not GOALS_FILE.exists():
        return {"generated_at": "", "goals": {}}
    with open(GOALS_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"generated_at": "", "goals": {}}


def save(data):
    GOALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data["generated_at"] = time.strftime("%Y-%m-%d")
    tmp = GOALS_FILE.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    os.replace(tmp, GOALS_FILE)


def log(action, goal_id, detail=None):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "date": time.strftime("%Y-%m-%d"),
        "action": action,
        "goal_id": goal_id,
        "detail": detail,
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def get_goal(data, gid):
    g = data.get("goals", {}).get(gid)
    if not g:
        # try prefix match
        matches = [k for k in data.get("goals", {}) if k.startswith(gid)]
        if len(matches) == 1:
            return matches[0], data["goals"][matches[0]]
        if len(matches) > 1:
            print(f"ambiguous goal_id, matches: {matches}")
            sys.exit(2)
        return None, None
    return gid, g


def cmd_list(args):
    data = load()
    rows = []
    for gid, g in data.get("goals", {}).items():
        if args.status and g.get("status") != args.status:
            continue
        if args.project and g.get("project") != args.project:
            continue
        rows.append((gid, g))
    rows.sort(key=lambda x: (x[1].get("priority", 5), -x[1].get("progress", 0)))
    for gid, g in rows:
        st = g.get("status", "?")
        pr = g.get("priority", "?")
        pg = g.get("progress", 0)
        proj = g.get("project") or "-"
        print(f"  P{pr} [{st:<7}] {pg:>3}%  {gid:<32} {proj:<20} {g.get('title','')[:50]}")
    print(f"\n{len(rows)} goals")


def cmd_show(args):
    data = load()
    gid, g = get_goal(data, args.goal_id)
    if not g:
        print(f"no goal: {args.goal_id}")
        sys.exit(2)
    print(yaml.safe_dump({gid: g}, sort_keys=False, allow_unicode=True, default_flow_style=False))


def cmd_progress(args):
    data = load()
    gid, g = get_goal(data, args.goal_id)
    if not g:
        print(f"no goal: {args.goal_id}")
        sys.exit(2)
    pct = max(0, min(100, args.percent))
    g["progress"] = pct
    save(data)
    log("progress", gid, {"new_pct": pct})
    print(f"{gid} progress -> {pct}%")


def cmd_status(args):
    data = load()
    gid, g = get_goal(data, args.goal_id)
    if not g:
        print(f"no goal: {args.goal_id}")
        sys.exit(2)
    if args.new_status not in VALID_STATUS:
        print(f"status must be one of {VALID_STATUS}")
        sys.exit(2)
    g["status"] = args.new_status
    if args.new_status == "done":
        g["progress"] = 100
        g["completed"] = time.strftime("%Y-%m-%d")
    save(data)
    log("status", gid, {"new": args.new_status})
    print(f"{gid} status -> {args.new_status}")


def cmd_block(args):
    data = load()
    gid, g = get_goal(data, args.goal_id)
    if not g:
        print(f"no goal: {args.goal_id}")
        sys.exit(2)
    g.setdefault("blockers", []).append(args.text)
    save(data)
    log("block_add", gid, {"text": args.text})
    print(f"{gid} blocker added: {args.text}")


def cmd_unblock(args):
    data = load()
    gid, g = get_goal(data, args.goal_id)
    if not g:
        print(f"no goal: {args.goal_id}")
        sys.exit(2)
    blockers = g.get("blockers") or []
    if args.idx < 0 or args.idx >= len(blockers):
        print(f"index out of range (have {len(blockers)} blockers)")
        sys.exit(2)
    removed = blockers.pop(args.idx)
    save(data)
    log("block_remove", gid, {"text": removed})
    print(f"{gid} unblocked: {removed}")


def cmd_sub_done(args):
    data = load()
    gid, g = get_goal(data, args.goal_id)
    if not g:
        print(f"no goal: {args.goal_id}")
        sys.exit(2)
    subs = g.get("subgoals") or []
    if args.idx < 0 or args.idx >= len(subs):
        print(f"index out of range (have {len(subs)} subgoals)")
        sys.exit(2)
    subs[args.idx]["status"] = "done"
    subs[args.idx]["done_date"] = time.strftime("%Y-%m-%d")
    # auto-bump progress
    done_count = sum(1 for s in subs if s.get("status") == "done")
    if subs:
        g["progress"] = round(100 * done_count / len(subs))
    save(data)
    log("sub_done", gid, {"idx": args.idx, "title": subs[args.idx].get("title")})
    print(f"{gid} subgoal[{args.idx}] done. progress={g.get('progress')}%")


def cmd_sub_add(args):
    data = load()
    gid, g = get_goal(data, args.goal_id)
    if not g:
        print(f"no goal: {args.goal_id}")
        sys.exit(2)
    g.setdefault("subgoals", []).append({"title": args.title, "status": "pending"})
    save(data)
    log("sub_add", gid, {"title": args.title})
    print(f"{gid} +subgoal: {args.title}")


def cmd_history(args):
    if not LOG_FILE.exists():
        print("no log yet")
        return
    for ln in LOG_FILE.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if args.goal_id and not r.get("goal_id", "").startswith(args.goal_id):
            continue
        print(f"  {r['date']}  {r['action']:<14}  {r['goal_id']:<32}  {json.dumps(r.get('detail') or {}, ensure_ascii=False)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list"); p.add_argument("--status"); p.add_argument("--project")
    p = sub.add_parser("show"); p.add_argument("goal_id")
    p = sub.add_parser("progress"); p.add_argument("goal_id"); p.add_argument("percent", type=int)
    p = sub.add_parser("status"); p.add_argument("goal_id"); p.add_argument("new_status")
    p = sub.add_parser("block"); p.add_argument("goal_id"); p.add_argument("text")
    p = sub.add_parser("unblock"); p.add_argument("goal_id"); p.add_argument("idx", type=int)
    p = sub.add_parser("sub-done"); p.add_argument("goal_id"); p.add_argument("idx", type=int)
    p = sub.add_parser("sub-add"); p.add_argument("goal_id"); p.add_argument("title")
    p = sub.add_parser("history"); p.add_argument("goal_id", nargs="?")

    args = ap.parse_args()
    {
        "list": cmd_list, "show": cmd_show, "progress": cmd_progress, "status": cmd_status,
        "block": cmd_block, "unblock": cmd_unblock, "sub-done": cmd_sub_done,
        "sub-add": cmd_sub_add, "history": cmd_history,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
