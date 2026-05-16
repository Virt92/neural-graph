"""Phase 6c — process watcher for external CLI agents.

Polls psutil every N seconds, finds AI/dev tool processes, captures:
  - cmdline (= which agent: cursor, aider, claude, codex, continue, ollama, ...)
  - cwd (= which project they're touching)
  - network connections (= which hosts/ports they talk to)
  - open file count
  - cpu/mem snapshot

Output: memory/graph/activity/processes.json (snapshot, replaced each poll)
{
  "ts": ...,
  "agents": [
     {pid, name, agent_kind, cwd, cmdline_short, cpu_pct, rss_mb,
      project_id, network: [{host, port, status}], file_count}
  ]
}

CLI:
  py process_watcher.py            # one-shot snapshot
  py process_watcher.py --loop 5   # poll every 5s
  py process_watcher.py --once --json   # for piping
"""

import argparse
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
ACT_DIR = HERE / "activity"
ACT_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = ACT_DIR / "processes.json"

# Process name → agent kind. Add new agents here as they emerge.
AGENT_PATTERNS = [
    # name pattern (substring, lowercase) → kind
    (("claude", "cli"), "claude_code"),
    (("cursor",), "cursor"),
    (("aider",), "aider"),
    (("codex",), "codex"),
    (("continue",), "continue"),
    (("ollama",), "ollama"),
    (("anthropic",), "anthropic_sdk"),
    (("openai",), "openai_sdk"),
    (("playwright",), "playwright"),
    (("chrome",), "chrome"),
    (("electron",), "electron_app"),
    (("py",), "python"),  # generic python — only if cmdline matches AGENT_HINTS
    (("python",), "python"),
    (("node",), "node"),  # generic node — same
]

# If process is generic (python/node), look for these in cmdline to confirm it's an agent.
AGENT_CMDLINE_HINTS = [
    "claude", "anthropic", "openai", "agent", "llm", "ollama",
    "playwright", "selenium", "cursor", "aider", "codex",
    "embed", "embedding", "rag", "langchain", "llamaindex",
    "reflect.py", "embed.py", "build_graph.py", "process_watcher.py",
    "decisions.py", "anomalies.py", "growth.py", "signals.py",
]

# Project tag map (mirror decisions.py).
# Add (substring_in_path_lowercase, project_id) tuples for your projects.
PROJECT_TAGS = [
    ("neural-graph", "proj_neural_graph"), ("memory", "proj_neural_graph"),
]


def infer_project(cwd, cmdline):
    haystacks = []
    if cwd:
        haystacks.append(cwd.lower().replace("\\", "/"))
    if cmdline:
        haystacks.append(cmdline.lower().replace("\\", "/"))
    for hay in haystacks:
        for tag, pid in PROJECT_TAGS:
            if tag in hay:
                return pid
    return None


def detect_agent_kind(name_lower, cmdline_lower):
    for patterns, kind in AGENT_PATTERNS:
        if any(p in name_lower for p in patterns):
            if kind in ("python", "node"):
                # only count if cmdline mentions an agent hint
                if any(h in cmdline_lower for h in AGENT_CMDLINE_HINTS):
                    return kind
                continue
            return kind
    return None


def reverse_dns_safe(ip):
    try:
        return socket.getfqdn(ip) or ip
    except Exception:
        return ip


def gather():
    out = {"ts": int(time.time()), "agents": []}
    own_pid = os.getpid()
    for proc in psutil.process_iter(attrs=["pid", "name", "cmdline", "cwd", "username"]):
        try:
            if proc.info["pid"] == own_pid:
                continue
            name = (proc.info.get("name") or "").lower()
            cmdline_list = proc.info.get("cmdline") or []
            cmdline = " ".join(cmdline_list)
            cmdline_lower = cmdline.lower()
            kind = detect_agent_kind(name, cmdline_lower)
            if not kind:
                continue
            cwd = proc.info.get("cwd")
            try:
                cpu = proc.cpu_percent(interval=None)
                mem = proc.memory_info().rss / (1024 * 1024)
            except Exception:
                cpu, mem = 0.0, 0.0
            net_list = []
            try:
                conns = proc.net_connections(kind="inet")
                seen_targets = set()
                for c in conns:
                    if c.status not in ("ESTABLISHED", "SYN_SENT"):
                        continue
                    if not c.raddr:
                        continue
                    key = (c.raddr.ip, c.raddr.port)
                    if key in seen_targets:
                        continue
                    seen_targets.add(key)
                    if len(net_list) >= 10:
                        break
                    net_list.append({
                        "host": c.raddr.ip,
                        "port": c.raddr.port,
                        "status": c.status,
                    })
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
            try:
                file_count = len(proc.open_files())
            except Exception:
                file_count = 0
            short_cmd = cmdline[:160] + ("..." if len(cmdline) > 160 else "")
            project_id = infer_project(cwd, cmdline)
            out["agents"].append({
                "pid": proc.info["pid"],
                "name": name,
                "agent_kind": kind,
                "cwd": cwd,
                "project_id": project_id,
                "cmdline_short": short_cmd,
                "cpu_pct": round(cpu, 1),
                "rss_mb": round(mem, 1),
                "network": net_list,
                "file_count": file_count,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # collapse parent/child noise: keep only top-cpu agent per (kind, cwd)
    bucket = {}
    for a in out["agents"]:
        key = (a["agent_kind"], a["cwd"] or "")
        existing = bucket.get(key)
        if not existing or a["cpu_pct"] > existing["cpu_pct"]:
            bucket[key] = a
    out["agents"] = sorted(bucket.values(), key=lambda x: -x["cpu_pct"])
    out["counts_by_kind"] = {}
    for a in out["agents"]:
        k = a["agent_kind"]
        out["counts_by_kind"][k] = out["counts_by_kind"].get(k, 0) + 1
    return out


def write_out(snapshot):
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, OUT_PATH)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="poll every N seconds (0=once)")
    ap.add_argument("--json", action="store_true", help="print snapshot to stdout")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    def one():
        snap = gather()
        write_out(snap)
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            print(f"[{time.strftime('%H:%M:%S')}] {len(snap['agents'])} agents:", snap["counts_by_kind"])
            for a in snap["agents"][:8]:
                proj = a["project_id"] or "-"
                nets = ",".join(f"{n['host']}:{n['port']}" for n in a["network"][:3])
                print(f"  {a['agent_kind']:<14} pid={a['pid']:<6} {proj:<18} cpu={a['cpu_pct']:>4}% mem={a['rss_mb']:>5.0f}MB conn=[{nets}]")

    if args.loop and args.loop > 0:
        # warm-up cpu_percent
        for p in psutil.process_iter():
            try: p.cpu_percent(interval=None)
            except Exception: pass
        time.sleep(0.5)
        while True:
            try:
                one()
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"poll err: {e}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        one()


if __name__ == "__main__":
    main()
