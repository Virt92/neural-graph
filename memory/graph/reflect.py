"""Daily reflection / consolidation. Runs nightly via cron.

Reads today's CC transcripts from ~/.claude/projects/**/*.jsonl, aggregates:
- prompts (user messages)
- tool calls (counts + targets)
- files touched (Read/Edit/Write)
- errors encountered
- tokens + cost (from message.usage)
- subagents spawned

Then optionally calls Anthropic API to produce a narrative summary.
Output: memory/daily/YYYY-MM-DD.md

Usage:
  py reflect.py                # today
  py reflect.py --date 2026-05-15
  py reflect.py --no-llm       # skip Claude API call, structured digest only
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
PROJECTS_ROOT = HOME / ".claude" / "projects"
MEM_DIR = Path(__file__).resolve().parent.parent
DAILY_DIR = MEM_DIR / "daily"

# Opus 4.6 pricing per 1M tokens
PRICE_IN = 15.0
PRICE_OUT = 75.0
PRICE_CACHE_W = 18.75
PRICE_CACHE_R = 1.50

def decode_cwd(dir_name):
    s = re.sub(r"^([A-Za-z])--", r"\1:\\", dir_name)
    s = s.replace("-", "\\")
    return s

PROJECT_TAGS = [
    # Add (substring_in_path_lowercase, project_id) tuples for your projects.
    ("neural-graph", "proj_neural_graph"), ("memory", "proj_neural_graph"),
]
def infer_project(cwd, raw_dir_name=None):
    haystacks = []
    if cwd:
        haystacks.append(cwd.lower().replace("\\", "/"))
    if raw_dir_name:
        haystacks.append(raw_dir_name.lower().replace("--", "/").replace("-", "/"))
    for hay in haystacks:
        for tag, pid in PROJECT_TAGS:
            if tag in hay:
                return pid
    return None

def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def collect_day(target_date):
    """Walk all jsonl files, collect messages whose timestamp is on target_date."""
    start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    digest = {
        "date": target_date.isoformat(),
        "sessions": [],   # per session summary
        "tokens": {"in": 0, "out": 0, "cache_create": 0, "cache_read": 0},
        "cost_usd": 0.0,
        "tools": {},      # tool_name -> count
        "tool_errors": {},
        "files_touched": [],  # unique paths
        "prompts": [],        # user text prompts
        "projects": {},       # project_id -> session count
        "subagents": [],      # agent name list
    }

    if not PROJECTS_ROOT.exists():
        return digest

    files_set = set()

    def process_jsonl(path, project_id, agent_name=None, parent_sid=None):
        session = {
            "id": path.stem,
            "project_id": project_id,
            "agent_name": agent_name,
            "parent_sid": parent_sid,
            "msg_count": 0,
            "tok_in": 0, "tok_out": 0, "cache_create": 0, "cache_read": 0,
            "tools": {},
            "errors": 0,
            "started": None,
            "ended": None,
        }
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        m = json.loads(line)
                    except Exception:
                        continue
                    ts_str = m.get("timestamp") or m.get("ts") or m.get("created_at")
                    ts = parse_iso(ts_str) if ts_str else None
                    if not ts:
                        continue  # skip messages without timestamps (snapshots, system rows)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < start or ts >= end:
                        continue
                    # upgrade project_id from per-message cwd if dir-level inference failed
                    if not session["project_id"]:
                        cwd_field = m.get("cwd") or (m.get("message") or {}).get("cwd")
                        if cwd_field:
                            pid_from_cwd = infer_project(cwd_field)
                            if pid_from_cwd:
                                session["project_id"] = pid_from_cwd
                    session["started"] = session["started"] or ts.isoformat()
                    session["ended"] = ts.isoformat()
                    session["msg_count"] += 1

                    msg = m.get("message") or {}
                    if m.get("type") == "user":
                        content = msg.get("content")
                        if isinstance(content, str) and len(content) > 5:
                            digest["prompts"].append({"sid": path.stem, "text": content[:300]})
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    t = c.get("text", "")
                                    if len(t) > 5:
                                        digest["prompts"].append({"sid": path.stem, "text": t[:300]})
                                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                                    session["errors"] += 1
                    elif m.get("type") == "assistant":
                        usage = msg.get("usage") or {}
                        session["tok_in"] += usage.get("input_tokens", 0) or 0
                        session["tok_out"] += usage.get("output_tokens", 0) or 0
                        session["cache_create"] += usage.get("cache_creation_input_tokens", 0) or 0
                        session["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
                        content = msg.get("content")
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "tool_use":
                                    name = c.get("name", "?")
                                    session["tools"][name] = session["tools"].get(name, 0) + 1
                                    digest["tools"][name] = digest["tools"].get(name, 0) + 1
                                    inp = c.get("input") or {}
                                    fp = inp.get("file_path") or inp.get("path")
                                    if fp and name in ("Read", "Edit", "Write", "NotebookEdit"):
                                        files_set.add(fp)
        except Exception as e:
            print(f"WARN reading {path}: {e}", file=sys.stderr)

        if session["msg_count"] == 0:
            return  # no msgs from this date in this file
        if agent_name:
            digest["subagents"].append({"name": agent_name, "parent": parent_sid, "cost": calc_cost(session)})
        digest["sessions"].append(session)
        digest["tokens"]["in"] += session["tok_in"]
        digest["tokens"]["out"] += session["tok_out"]
        digest["tokens"]["cache_create"] += session["cache_create"]
        digest["tokens"]["cache_read"] += session["cache_read"]
        for tn, err in [(t, session["errors"]) for t in session["tools"]]:
            pass  # error attribution complicated; keep at session level
        digest["cost_usd"] += calc_cost(session)
        if project_id:
            digest["projects"][project_id] = digest["projects"].get(project_id, 0) + 1

    for proj_dir in PROJECTS_ROOT.iterdir():
        if not proj_dir.is_dir():
            continue
        cwd = decode_cwd(proj_dir.name)
        project_id = infer_project(cwd, raw_dir_name=proj_dir.name)
        for child in proj_dir.iterdir():
            if child.is_file() and child.suffix == ".jsonl":
                process_jsonl(child, project_id)
            elif child.is_dir():
                sub_dir = child / "subagents"
                if sub_dir.exists():
                    for sf in sub_dir.iterdir():
                        if sf.suffix != ".jsonl":
                            continue
                        m = re.match(r"^agent-(.+?)-[a-f0-9]+\.jsonl$", sf.name)
                        agent_name = m.group(1) if m else sf.stem
                        process_jsonl(sf, project_id, agent_name=agent_name, parent_sid=child.name)

    digest["files_touched"] = sorted(files_set)[:100]
    # condense prompts: max 50, sorted by length desc
    digest["prompts"] = sorted(digest["prompts"], key=lambda p: -len(p["text"]))[:50]
    return digest

def calc_cost(s):
    return (
        s["tok_in"] * PRICE_IN
        + s["tok_out"] * PRICE_OUT
        + s["cache_create"] * PRICE_CACHE_W
        + s["cache_read"] * PRICE_CACHE_R
    ) / 1_000_000

def render_structured(digest):
    lines = []
    d = digest
    lines.append(f"# Reflection — {d['date']}")
    lines.append("")
    lines.append(f"**Sessions:** {len(d['sessions'])}  ·  **Subagents:** {len(d['subagents'])}")
    lines.append(f"**Cost:** ${d['cost_usd']:.2f}  ·  **Tokens in/out:** {d['tokens']['in']:,} / {d['tokens']['out']:,}  ·  **Cache w/r:** {d['tokens']['cache_create']:,} / {d['tokens']['cache_read']:,}")
    lines.append("")
    if d["projects"]:
        lines.append("## Projects touched")
        for pid, n in sorted(d["projects"].items(), key=lambda x: -x[1]):
            lines.append(f"- `{pid}` × {n} sessions")
        lines.append("")
    if d["tools"]:
        lines.append("## Tool usage")
        top = sorted(d["tools"].items(), key=lambda x: -x[1])[:15]
        for name, cnt in top:
            lines.append(f"- `{name}` × {cnt}")
        lines.append("")
    if d["subagents"]:
        lines.append("## Subagents spawned")
        agent_counts = {}
        for a in d["subagents"]:
            agent_counts[a["name"]] = agent_counts.get(a["name"], 0) + 1
        for name, cnt in sorted(agent_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- `{name}` × {cnt}")
        lines.append("")
    if d["files_touched"]:
        lines.append(f"## Files touched ({len(d['files_touched'])})")
        for f in d["files_touched"][:40]:
            lines.append(f"- `{f}`")
        if len(d["files_touched"]) > 40:
            lines.append(f"- ...+{len(d['files_touched']) - 40} more")
        lines.append("")
    if d["prompts"]:
        lines.append("## Notable prompts")
        for p in d["prompts"][:10]:
            short = p["text"].replace("\n", " ")[:200]
            lines.append(f"- {short}")
        lines.append("")
    return "\n".join(lines)

def call_claude_summary(digest):
    """Use Claude Code CLI (uses user's existing auth — no API key needed).

    Pipes a system+digest prompt to `claude -p` and returns text response.
    """
    import subprocess
    ctx = {
        "date": digest["date"],
        "sessions_count": len(digest["sessions"]),
        "cost_usd": round(digest["cost_usd"], 2),
        "tokens": digest["tokens"],
        "projects": digest["projects"],
        "top_tools": sorted(digest["tools"].items(), key=lambda x: -x[1])[:10],
        "subagents_count": len(digest["subagents"]),
        "files_touched_sample": digest["files_touched"][:30],
        "prompts_sample": [p["text"][:200] for p in digest["prompts"][:15]],
    }
    prompt = (
        "Reflect on this developer's day of Claude Code work. Write a concise 5-10 bullet "
        "retrospective in markdown covering: (1) primary themes of work, (2) projects that "
        "received most attention, (3) apparent struggles or repeated errors, (4) noteworthy new "
        "techniques/tools first encountered, (5) suggestions for tomorrow. Be specific. Be terse. "
        "Do not summarize raw data — derive insight.\n\n"
        f"DIGEST:\n{json.dumps(ctx, ensure_ascii=False, indent=2)[:18000]}"
    )
    try:
        r = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="ignore",
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        print(f"claude CLI returned {r.returncode}: {r.stderr[:200]}", file=sys.stderr)
    except FileNotFoundError:
        print("claude CLI not on PATH — skipping narrative", file=sys.stderr)
    except Exception as e:
        print(f"claude CLI call failed: {e}", file=sys.stderr)
    return None

def main():
    args = sys.argv[1:]
    target_date = datetime.now(timezone.utc).date()
    if "--date" in args:
        target_date = datetime.fromisoformat(args[args.index("--date") + 1]).date()
    use_llm = "--no-llm" not in args

    print(f"Collecting transcripts for {target_date}...", flush=True)
    digest = collect_day(target_date)
    if not digest["sessions"]:
        print("No sessions found for date. Skipping.", flush=True)
        return

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DAILY_DIR / f"{target_date.isoformat()}.md"

    structured = render_structured(digest)
    narrative = call_claude_summary(digest) if use_llm else None

    parts = ["---",
             f"name: Daily reflection {target_date.isoformat()}",
             f"description: Auto-generated daily digest — sessions, costs, themes, files",
             "type: reference",
             "---",
             ""]
    if narrative:
        parts += ["## Narrative", "", narrative.strip(), "", "---", ""]
    parts.append(structured)
    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {out_path}", flush=True)
    # also dump raw json next to it for downstream tools
    (DAILY_DIR / f"{target_date.isoformat()}.json").write_text(
        json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__":
    main()
