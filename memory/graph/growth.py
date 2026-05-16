"""Growth observability aggregator (Phase 11).

Walks:
- memory/*.md  -> memory creation events (by mtime, by type from filename prefix)
- ~/.claude/plugins/marketplaces/**/*.skill, **/skills/*.md -> skill events
- memory/daily/YYYY-MM-DD.json -> per-day session/cost stats
- memory/graph/activity/health.json (optional) -> active project list
- memory/graph/embeddings.json (optional) -> total node count

Groups events into ISO-week buckets. Detects "first contact" with new
tool/MCP/domain by scanning daily JSON tools[] dicts ordered by date.

Output: memory/graph/activity/growth.json
{
  "updated_at": <epoch>,
  "week_count": N,
  "totals": {"memories": M, "skills": S, "sessions": Z, "cost": $X, "tools": T},
  "weeks": [
    {
      "week_start": "2026-05-11",
      "memories_added": [{"name":"...", "type":"feedback"}],
      "memories_count": 3,
      "memories_by_type": {"feedback": 1, "project": 2},
      "skills_added": ["..."],
      "sessions": N,
      "cost": $X,
      "tokens_in": N,
      "tokens_out": N,
      "top_projects": [["proj_x", N], ...],
      "first_contacts": ["mcp__foo__bar", ...]
    }
  ],
  "daily_cost": [{"d":"2026-05-15","c":2385.0}],
  "first_contact_log": [{"name":"X","date":"YYYY-MM-DD","kind":"tool|mcp"}]
}
"""

import json
import os
import sys
import time
import glob
from datetime import datetime, timezone, timedelta, date
from collections import defaultdict, Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
GRAPH_DIR = SCRIPT_DIR
MEM_DIR = SCRIPT_DIR.parent
DAILY_DIR = MEM_DIR / "daily"
ACTIVITY_DIR = SCRIPT_DIR / "activity"
HOME_CLAUDE = Path(os.path.expandvars(r"%USERPROFILE%")) / ".claude"
PLUGINS = HOME_CLAUDE / "plugins" / "marketplaces"

ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)


def iso_week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday


def memory_type_from_name(name: str) -> str:
    base = name.lower()
    for t in ("feedback_", "project_", "user_", "reference_"):
        if base.startswith(t):
            return t.rstrip("_")
    return "other"


def collect_memories():
    items = []
    for p in MEM_DIR.glob("*.md"):
        if p.name == "MEMORY.md":
            continue
        st = p.stat()
        # use ctime if newer than mtime fallback (Windows ctime ~ creation)
        created = min(st.st_ctime, st.st_mtime)
        items.append({
            "name": p.stem,
            "type": memory_type_from_name(p.name),
            "created": created,
            "path": str(p),
        })
    return items


def collect_skills():
    """Real skills only: *.skill files OR SKILL.md inside */skills/*/ dirs."""
    items = []
    if not PLUGINS.exists():
        return items
    seen = set()
    # *.skill (caveman-style packaged)
    for p in PLUGINS.rglob("*.skill"):
        key = p.stem.lower()
        if key in seen:
            continue
        seen.add(key)
        st = p.stat()
        items.append({"name": p.stem, "created": min(st.st_ctime, st.st_mtime), "path": str(p)})
    # SKILL.md files (Anthropic skill convention)
    for p in PLUGINS.rglob("SKILL.md"):
        # name = parent directory
        name = p.parent.name
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        st = p.stat()
        items.append({"name": name, "created": min(st.st_ctime, st.st_mtime), "path": str(p)})
    return items


def load_daily():
    """Returns list of (date_str, json_dict) sorted ascending."""
    out = []
    if not DAILY_DIR.exists():
        return out
    for p in sorted(DAILY_DIR.glob("*.json")):
        try:
            with p.open("r", encoding="utf-8") as f:
                out.append((p.stem, json.load(f)))
        except Exception as e:
            print(f"skip {p.name}: {e}", file=sys.stderr)
    return out


# Opus 4.6 pricing (per million tokens)
PRICE_IN = 15.0
PRICE_OUT = 75.0
PRICE_CACHE_CREATE = 18.75
PRICE_CACHE_READ = 1.50


def session_cost(sess: dict) -> float:
    return (
        sess.get("tok_in", 0) * PRICE_IN
        + sess.get("tok_out", 0) * PRICE_OUT
        + sess.get("cache_create", 0) * PRICE_CACHE_CREATE
        + sess.get("cache_read", 0) * PRICE_CACHE_READ
    ) / 1_000_000.0


def main():
    memories = collect_memories()
    skills = collect_skills()
    daily = load_daily()

    weeks = defaultdict(lambda: {
        "week_start": None,
        "memories_added": [],
        "memories_by_type": Counter(),
        "skills_added": [],
        "sessions": 0,
        "cost": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "projects": Counter(),
        "first_contacts": [],
    })

    def wk(ts):
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        return iso_week_start(d).isoformat()

    for m in memories:
        w = wk(m["created"])
        b = weeks[w]
        b["week_start"] = w
        b["memories_added"].append({"name": m["name"], "type": m["type"]})
        b["memories_by_type"][m["type"]] += 1

    for s in skills:
        w = wk(s["created"])
        b = weeks[w]
        b["week_start"] = w
        b["skills_added"].append(s["name"])

    # daily aggregation
    daily_cost = []
    seen_tools = set()
    first_contact_log = []
    for date_str, dj in daily:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        w = iso_week_start(d).isoformat()
        b = weeks[w]
        b["week_start"] = w
        sessions = dj.get("sessions", [])
        day_cost = 0.0
        day_in = 0
        day_out = 0
        day_tools_today = set()
        for sess in sessions:
            b["sessions"] += 1
            c = session_cost(sess)
            day_cost += c
            day_in += sess.get("tok_in", 0)
            day_out += sess.get("tok_out", 0)
            pid = sess.get("project_id")
            if pid:
                b["projects"][pid] += 1
            for tname in sess.get("tools", {}).keys():
                day_tools_today.add(tname)
        b["cost"] += day_cost
        b["tokens_in"] += day_in
        b["tokens_out"] += day_out
        # first-contact: tool seen for very first time
        for tname in sorted(day_tools_today):
            if tname not in seen_tools:
                seen_tools.add(tname)
                kind = "mcp" if tname.startswith("mcp__") else "tool"
                first_contact_log.append({"name": tname, "date": date_str, "kind": kind})
                b["first_contacts"].append(tname)
        daily_cost.append({"d": date_str, "c": round(day_cost, 4)})

    weeks_sorted = sorted(weeks.values(), key=lambda x: x["week_start"] or "")
    # finalize: convert counters to lists
    for w in weeks_sorted:
        w["memories_count"] = len(w["memories_added"])
        w["memories_by_type"] = dict(w["memories_by_type"])
        w["top_projects"] = w["projects"].most_common(5)
        del w["projects"]
        w["cost"] = round(w["cost"], 4)
        w["skills_count"] = len(w["skills_added"])

    totals = {
        "memories": len(memories),
        "skills": len(skills),
        "sessions": sum(w["sessions"] for w in weeks_sorted),
        "cost": round(sum(w["cost"] for w in weeks_sorted), 4),
        "tools": len(seen_tools),
    }

    out = {
        "updated_at": int(time.time()),
        "week_count": len(weeks_sorted),
        "totals": totals,
        "weeks": weeks_sorted,
        "daily_cost": daily_cost,
        "first_contact_log": first_contact_log[-50:],  # last 50
    }

    out_path = ACTIVITY_DIR / "growth.json"
    tmp = out_path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)
    print(f"growth.json: {len(weeks_sorted)} weeks, "
          f"{totals['memories']}M / {totals['skills']}S / "
          f"{totals['sessions']}sess / ${totals['cost']:.2f}")


if __name__ == "__main__":
    main()
