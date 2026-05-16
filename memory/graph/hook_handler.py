"""
Hook handler — receives JSON via stdin from Claude Code hooks,
updates memory/graph/activity/sessions.json with live session state.

Registered events (in ~/.claude/settings.json):
  - SessionStart       : create session record
  - UserPromptSubmit   : update last_prompt + last_event_at
  - PreToolUse         : set current_tool/current_input, append to recent_tools
  - PostToolUse        : clear current_tool
  - SessionEnd / Stop  : mark inactive

Hook payload (stdin JSON) per CC docs typically includes:
  session_id, transcript_path, cwd, hook_event_name, tool_name, tool_input

Output file: memory/graph/activity/sessions.json
Format:
  {
    "updated_at": <epoch>,
    "sessions": {
      "<session_id>": {
        "id": ..., "cwd": ..., "project_id": ...,
        "started_at": ..., "last_event_at": ...,
        "current_tool": ..., "current_target": ...,
        "recent_tools": [...], "recent_files": [...],
        "recent_mcps": [...], "active_skills": [...],
        "prompt_count": N, "tool_count": N,
        "active": true/false
      }, ...
    }
  }

Uses simple file-lock via os.replace for atomic write.
Never blocks Claude — exits fast even on error.
"""
import json
import os
import sys
import time
import tempfile
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACTIVITY_DIR = HERE / "activity"
ACTIVITY_DIR.mkdir(exist_ok=True)
STATE_FILE = ACTIVITY_DIR / "sessions.json"
LOG_FILE = ACTIVITY_DIR / "events.log"
SCRATCHPAD = HERE.parent / "_scratchpad.md"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
TRAFFIC_FILE = ACTIVITY_DIR / "traffic.jsonl"
WEB_TOOLS = {"WebFetch", "WebSearch"}

# Add (substring_in_cwd_lowercase, project_id) tuples for your projects.
# Example: ("myapp", "proj_myapp"), ("client_acme", "proj_acme"),
PROJECT_BY_PATH_TAGS = [
]

MCP_TAG_TO_ID = {
    "playwright": "mcp_playwright",
    "native-devtools": "mcp_native_devtools",
    "native_devtools": "mcp_native_devtools",
    "telegram": "mcp_telegram",
    "figma": "mcp_figma",
    "canva": "mcp_canva",
    "gmail": "mcp_gmail",
    "calendar": "mcp_calendar",
    "tldraw": "mcp_tldraw",
    "mermaid": "mcp_mermaid",
    "context7": "mcp_context7",
    "filesystem": "mcp_filesystem",
    "github": "mcp_github",
    "markitdown": "mcp_markitdown",
    "tavily": "mcp_tavily",
    "fetch": "mcp_fetch",
}


def infer_project_from_path(p: str | None) -> str | None:
    if not p:
        return None
    low = p.lower().replace("\\", "/")
    for tag, pid in PROJECT_BY_PATH_TAGS:
        if tag in low:
            return pid
    return None


def infer_mcp_from_tool(tool_name: str) -> str | None:
    if not tool_name:
        return None
    # MCP tools follow pattern mcp__<server>__<tool>
    m = re.match(r"mcp__([a-zA-Z0-9_\-]+)__", tool_name)
    if m:
        server = m.group(1).lower().replace("-", "_").replace(".", "_")
        return MCP_TAG_TO_ID.get(server) or f"mcp_{server}"
    return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"updated_at": 0, "sessions": {}}


def save_state(state: dict):
    state["updated_at"] = time.time()
    # Atomic write: write to temp, then replace
    fd, tmppath = tempfile.mkstemp(dir=str(ACTIVITY_DIR), prefix=".sessions_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmppath, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmppath)
        except Exception:
            pass
    # Also write JS file so visualizer can load via <script> tag (file:// blocks fetch)
    try:
        js_path = ACTIVITY_DIR / "sessions.js"
        js_text = "window.LIVE_SESSIONS = " + json.dumps(state, ensure_ascii=False) + ";\nif(window.__onLiveSessions)window.__onLiveSessions(window.LIVE_SESSIONS);\n"
        js_path.write_text(js_text, encoding="utf-8")
    except Exception:
        pass


def trim(lst, n):
    return lst[-n:] if len(lst) > n else lst


def append_traffic(rec: dict):
    """Append one traffic event for Phase-traffic visualization."""
    try:
        with TRAFFIC_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def parse_url_host(url: str) -> str | None:
    if not url:
        return None
    try:
        m = re.match(r"https?://([^/]+)", url, re.I)
        if m:
            host = m.group(1).lower()
            # strip port
            return host.split(":")[0]
    except Exception:
        pass
    return None


def log_event(event: dict):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), **{k: v for k, v in event.items() if k != "tool_input"}}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def check_conflict_lock(my_sid: str, target_path: str) -> str | None:
    """Phase 7 — return warning text if another session has WORKING: lock matching target_path.

    Soft warning only (printed to stderr). Never blocks.
    """
    try:
        if not SCRATCHPAD.exists() or not target_path:
            return None
        my_short = (my_sid or "")[:8]
        norm = target_path.lower().replace("\\", "/")
        for ln in SCRATCHPAD.read_text(encoding="utf-8").splitlines():
            if "WORKING:" not in ln:
                continue
            # parse [YYYY-MM-DD HH:MM session_short_id] WORKING: text
            m = re.match(r"\[\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+(\S+?)\s*\]\s*WORKING:\s*(.*)$", ln)
            if not m:
                continue
            sess_short, msg = m.group(2), m.group(3).lower()
            if sess_short == my_short or sess_short == "?":
                continue
            # match: any path-ish token in msg appears in target
            for tok in re.findall(r"[a-z0-9_/.-]{3,}", msg):
                if "/" in tok or "." in tok or "_" in tok:
                    if tok in norm:
                        return f"WORKING-LOCK by sess={sess_short}: {m.group(3)[:120]}"
        return None
    except Exception:
        return None


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        evt = json.loads(raw)
    except Exception:
        return 0  # never block on malformed input

    sid = evt.get("session_id") or "unknown"
    event = evt.get("hook_event_name") or "Unknown"
    cwd = evt.get("cwd") or ""
    tool_name = evt.get("tool_name") or ""
    tool_input = evt.get("tool_input") or {}

    # Phase 7: warn on edit conflict with another session's WORKING: lock
    if event == "PreToolUse" and tool_name in EDIT_TOOLS and isinstance(tool_input, dict):
        target = tool_input.get("file_path") or tool_input.get("path") or ""
        if target:
            warn = check_conflict_lock(sid, target)
            if warn:
                sys.stderr.write(f"[neural-graph] {warn}\n")

    # Traffic events: web fetches/searches mint domain ghost + traffic edge
    if event == "PreToolUse" and tool_name in WEB_TOOLS and isinstance(tool_input, dict):
        url = tool_input.get("url") or tool_input.get("query") or ""
        host = parse_url_host(url) if tool_name == "WebFetch" else None
        if not host and tool_name == "WebSearch":
            # WebSearch: no url; tag generic search hub
            host = "websearch"
        if host:
            append_traffic({
                "ts": time.time(),
                "session_id": sid,
                "kind": "web",
                "tool": tool_name,
                "host": host,
                "target_node": "ext_web_" + re.sub(r"[^a-z0-9]+", "_", host),
            })
    # Generic file/tool traffic events
    elif event == "PreToolUse" and tool_name and isinstance(tool_input, dict):
        target = tool_input.get("file_path") or tool_input.get("path") or ""
        if target:
            append_traffic({
                "ts": time.time(),
                "session_id": sid,
                "kind": "file",
                "tool": tool_name,
                "target": target[:200],
            })

    log_event(evt)

    state = load_state()
    sess = state["sessions"].get(sid)
    now = time.time()

    if event == "SessionStart" or sess is None:
        sess = {
            "id": sid,
            "cwd": cwd,
            "project_id": infer_project_from_path(cwd),
            "started_at": now,
            "last_event_at": now,
            "current_tool": None,
            "current_target": None,
            "recent_tools": [],
            "recent_files": [],
            "recent_mcps": [],
            "active_skills": [],
            "prompt_count": 0,
            "tool_count": 0,
            "active": True,
            "last_prompt": "",
        }

    sess["last_event_at"] = now
    sess["active"] = True
    if cwd and not sess.get("cwd"):
        sess["cwd"] = cwd
        sess["project_id"] = infer_project_from_path(cwd)

    if event == "UserPromptSubmit":
        prompt = evt.get("prompt") or evt.get("user_prompt") or ""
        sess["last_prompt"] = (prompt or "")[:200]
        sess["prompt_count"] = sess.get("prompt_count", 0) + 1
        # heuristic: detect project mentions in prompt
        pid = infer_project_from_path(prompt)
        if pid:
            sess["project_id"] = pid

    elif event == "PreToolUse":
        sess["current_tool"] = tool_name
        # target = file path / command summary
        target = ""
        if isinstance(tool_input, dict):
            target = (
                tool_input.get("file_path")
                or tool_input.get("path")
                or tool_input.get("pattern")
                or tool_input.get("command", "")[:80]
                or tool_input.get("url", "")[:80]
                or ""
            )
        sess["current_target"] = target
        sess["recent_tools"] = trim(sess.get("recent_tools", []) + [tool_name], 10)
        sess["tool_count"] = sess.get("tool_count", 0) + 1
        # only file-oriented tools contribute to recent_files
        if tool_name in ("Read", "Edit", "Write", "NotebookEdit"):
            fp = tool_input.get("file_path") if isinstance(tool_input, dict) else None
            if fp and any(fp.lower().endswith(ext) for ext in (".md", ".py", ".js", ".ts", ".html", ".json", ".tsx", ".jsx", ".css", ".vue", ".go", ".rs", ".java", ".kt")):
                sess["recent_files"] = trim(sess.get("recent_files", []) + [fp], 8)
        mcp_id = infer_mcp_from_tool(tool_name)
        if mcp_id:
            sess["recent_mcps"] = trim([m for m in sess.get("recent_mcps", []) if m != mcp_id] + [mcp_id], 5)
        # project from target path
        pid = infer_project_from_path(target)
        if pid:
            sess["project_id"] = pid

    elif event == "PostToolUse":
        sess["current_tool"] = None
        sess["current_target"] = None

    elif event in ("SessionEnd", "Stop"):
        sess["active"] = False

    state["sessions"][sid] = sess
    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
