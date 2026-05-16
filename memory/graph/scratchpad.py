"""Phase 7 — cross-session scratchpad CLI.

Append/read/list notes in memory/_scratchpad.md.

CLI:
  py scratchpad.py write "WORKING: editing src/MyComponent.tsx, hands off"
  py scratchpad.py read              # last 20 lines
  py scratchpad.py read --tail 50
  py scratchpad.py working           # only WORKING: locks
  py scratchpad.py clear-old --days 3   # drop entries older than N days
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
PAD = MEM_DIR / "_scratchpad.md"

LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\s+([\d:]+)(?:\s+([^\]]+))?\]\s*(.*)$")


def session_short():
    sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("SESSION_ID") or "?"
    return sid[:8]


def cmd_write(args):
    PAD.parent.mkdir(parents=True, exist_ok=True)
    if not PAD.exists():
        PAD.write_text("# Cross-Session Scratchpad\n\n", encoding="utf-8")
    ts = time.strftime("%Y-%m-%d %H:%M")
    sid = args.sid or session_short()
    line = f"[{ts} {sid}] {args.text}\n"
    with PAD.open("a", encoding="utf-8") as f:
        f.write(line)
    print(f"appended: {line.rstrip()}")


def cmd_read(args):
    if not PAD.exists():
        print("(empty)")
        return
    lines = PAD.read_text(encoding="utf-8").splitlines()
    body = [l for l in lines if LINE_RE.match(l)]
    tail = body[-args.tail:]
    for l in tail:
        print(l)


def cmd_working(args):
    if not PAD.exists():
        return
    cutoff = datetime.now() - timedelta(hours=args.hours)
    lines = PAD.read_text(encoding="utf-8").splitlines()
    for l in lines:
        m = LINE_RE.match(l)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if ts < cutoff:
            continue
        if "WORKING:" in m.group(4):
            print(l)


def cmd_clear_old(args):
    if not PAD.exists():
        return
    cutoff = datetime.now() - timedelta(days=args.days)
    lines = PAD.read_text(encoding="utf-8").splitlines()
    keep = []
    dropped = 0
    in_header = True
    for l in lines:
        m = LINE_RE.match(l)
        if not m:
            keep.append(l)
            continue
        in_header = False
        try:
            ts = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M")
        except Exception:
            keep.append(l)
            continue
        if ts >= cutoff:
            keep.append(l)
        else:
            dropped += 1
    PAD.write_text("\n".join(keep) + "\n", encoding="utf-8")
    print(f"dropped {dropped} entries older than {args.days}d")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("write"); p.add_argument("text"); p.add_argument("--sid")
    p = sub.add_parser("read"); p.add_argument("--tail", type=int, default=20)
    p = sub.add_parser("working"); p.add_argument("--hours", type=int, default=24)
    p = sub.add_parser("clear-old"); p.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    {"write": cmd_write, "read": cmd_read, "working": cmd_working, "clear-old": cmd_clear_old}[args.cmd](args)


if __name__ == "__main__":
    main()
