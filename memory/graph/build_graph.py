"""
Auto-build knowledge graph from Claude memory.
Scans ../*.md, parses frontmatter, infers edges, writes ../graph.json.

Node types:
  - core    : Claude itself (center)
  - project : project_*.md
  - feedback: feedback_*.md
  - user    : user_*.md
  - skill   : from ~/.claude/plugins/marketplaces/*/skills/* and ~/.claude/skills/
  - mcp     : from ~/.claude.json mcpServers
  - memory  : misc *.md (fallback)

Edges:
  - memory file -> mentioned projects/skills (regex on content)
  - cross-project mentions
  - project -> skill (manual hints + content match)
  - project -> mcp (content match)
"""
import json
import re
import os
import time
from pathlib import Path

MEM_DIR = Path(__file__).resolve().parent.parent
GRAPH_DIR = Path(__file__).resolve().parent
CLAUDE_DIR = Path.home() / ".claude"
SETTINGS_FILE = Path.home() / ".claude.json"

# Map cwd/filename substrings (lowercase) to project group ids.
# Add your projects here. Keys are case-insensitive substrings; values are the
# synthetic project node id. Multiple keys can map to the same project.
# Example:
#   "myapp": "proj_myapp",
#   "client_acme": "proj_acme",
PROJECT_TAGS = {
    # "example": "proj_example",
}

MCP_HINTS = {
    "playwright": "mcp_playwright",
    "native-devtools": "mcp_native_devtools",
    "telegram": "mcp_telegram",
    "figma": "mcp_figma",
    "canva": "mcp_canva",
    "gmail": "mcp_gmail",
    "calendar": "mcp_calendar",
    "tldraw": "mcp_tldraw",
    "mermaid": "mcp_mermaid",
}

SKILL_HINTS = {
    "frontend-design": "skill_frontend_design",
    "figma-use": "skill_figma_use",
    "caveman": "skill_caveman",
    "simplify": "skill_simplify",
    "claude-api": "skill_claude_api",
    "loop": "skill_loop",
    "schedule": "skill_schedule",
}


def parse_frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_raw = parts[1].strip()
    body = parts[2]
    fm = {}
    for line in fm_raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, body


def project_label(filename):
    """Map filename -> human label + project group id."""
    base = filename.replace("project_", "").replace(".md", "")
    return base


def infer_project_group(filename):
    """Map project_*.md -> project group node id via PROJECT_TAGS."""
    fl = filename.lower()
    for key, pid in PROJECT_TAGS.items():
        if key in fl:
            return pid
    return None


PATH_RE = re.compile(r"([A-Za-z]:[\\\/][^\s\)\]\"'`<>,;]+)")
DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
URL_RE = re.compile(r"https?://[^\s\)\]\"'`<>]+")
IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?::\d+)?\b")
HOST_RE = re.compile(r"\b([a-z0-9][a-z0-9\-]{0,30}\.(?:com|net|org|io|app|dev|city|llc|me|co|tech|cloud|xyz|info|ru|ua|de))\b", re.I)
SSH_RE = re.compile(r"ssh\s+([a-z_][\w\-]*)@([\w\.\-]+)", re.I)


def extract_meta(body):
    paths = list(dict.fromkeys(PATH_RE.findall(body)))[:5]
    dates = list(dict.fromkeys(DATE_RE.findall(body)))[:5]
    urls = list(dict.fromkeys(URL_RE.findall(body)))[:5]
    return paths, dates, urls


def extract_infra(body):
    """Return list of infrastructure refs: ips, hosts, ssh targets."""
    ips = list(dict.fromkeys(IP_RE.findall(body)))
    # filter local/private/api-version dummy patterns
    ips = [ip for ip in ips if not (ip.startswith("127.") or ip.startswith("0.") or ip.startswith("192.168.") or ip == "1.1.1.1")]
    hosts = list(dict.fromkeys([h.lower() for h in HOST_RE.findall(body)]))
    # drop obvious noise
    hosts = [h for h in hosts if not any(b in h for b in ("github.com", "anthropic.com", "google.com", "npmjs.com", "unpkg.com", "claude.ai"))]
    sshs = [{"user": u, "host": h} for u, h in SSH_RE.findall(body)]
    return ips[:5], hosts[:8], sshs[:5]


def scan_memory():
    nodes = {}
    edges = []

    # Core node
    nodes["core"] = {
        "id": "core",
        "type": "core",
        "label": "Claude",
        "description": "you (centre of graph)",
    }

    # Project group nodes (synthetic). Auto-derived from PROJECT_TAGS values.
    # To customize labels, override here. Default label = project id with
    # 'proj_' prefix stripped and underscores → spaces.
    project_groups = {pid: pid.replace("proj_", "").replace("_", " ").title()
                      for pid in set(PROJECT_TAGS.values())}
    for pid, label in project_groups.items():
        nodes[pid] = {
            "id": pid,
            "type": "project",
            "label": label,
            "description": "project group",
            "memory_files": [],
        }

    # Walk memory files
    for md in sorted(MEM_DIR.glob("*.md")):
        if md.name == "MEMORY.md":
            continue
        text = md.read_text(encoding="utf-8", errors="ignore")
        fm, body = parse_frontmatter(text)
        node_id = "mem_" + md.stem
        ntype = fm.get("type", "memory").lower()
        label = fm.get("name", md.stem)
        desc = fm.get("description", "")

        paths, dates, urls = extract_meta(body)
        ips, hosts, sshs = extract_infra(body)
        # short preview (first 280 chars stripped)
        preview = re.sub(r"\s+", " ", body.strip())[:280]
        try:
            mtime = md.stat().st_mtime
        except Exception:
            mtime = 0

        nodes[node_id] = {
            "id": node_id,
            "type": ntype if ntype in ("project", "feedback", "user", "reference") else "memory",
            "label": label,
            "description": desc,
            "file": md.name,
            "path": str(md),
            "preview": preview,
            "paths": paths,
            "dates": dates,
            "urls": urls,
            "mtime": mtime,
            "size": md.stat().st_size,
            "ips": ips,
            "hosts": hosts,
            "sshs": sshs,
        }

        # add infra nodes + edges from this memory
        for ip in ips:
            infra_id = "infra_ip_" + ip.replace(".", "_")
            if infra_id not in nodes:
                nodes[infra_id] = {
                    "id": infra_id, "type": "infra", "label": ip,
                    "description": "server IP", "infra_kind": "ip",
                }
                edges.append({"from": "core", "to": infra_id, "weight": 1, "reason": "reachable infra"})
            edges.append({"from": node_id, "to": infra_id, "weight": 1, "reason": "mentions IP"})
        for host in hosts:
            infra_id = "infra_host_" + re.sub(r"[^a-z0-9_]", "_", host)
            if infra_id not in nodes:
                nodes[infra_id] = {
                    "id": infra_id, "type": "infra", "label": host,
                    "description": "hostname / domain", "infra_kind": "host",
                }
                edges.append({"from": "core", "to": infra_id, "weight": 1, "reason": "reachable host"})
            edges.append({"from": node_id, "to": infra_id, "weight": 1, "reason": "mentions host"})

        # If project memory -> attach to project group
        if md.name.startswith("project_"):
            grp = infer_project_group(md.name)
            if grp:
                edges.append({"from": grp, "to": node_id, "weight": 1, "reason": "memory of project"})
                nodes[grp]["memory_files"].append(md.name)

        # Scan body for cross-references (project tags, mcps, skills)
        body_low = body.lower()
        for tag, pid in PROJECT_TAGS.items():
            if tag in body_low and pid in nodes:
                # don't self-link
                self_grp = infer_project_group(md.name)
                if self_grp != pid:
                    edges.append({"from": node_id, "to": pid, "weight": 1, "reason": f"mentions {tag}"})

        for tag, mid in MCP_HINTS.items():
            if tag in body_low:
                if mid not in nodes:
                    nodes[mid] = {"id": mid, "type": "mcp", "label": tag, "description": "MCP server"}
                    edges.append({"from": "core", "to": mid, "weight": 1, "reason": "available MCP"})
                edges.append({"from": node_id, "to": mid, "weight": 1, "reason": f"uses {tag}"})

        for tag, sid in SKILL_HINTS.items():
            if tag in body_low:
                if sid not in nodes:
                    nodes[sid] = {"id": sid, "type": "skill", "label": tag, "description": "skill"}
                    edges.append({"from": "core", "to": sid, "weight": 1, "reason": "available skill"})
                edges.append({"from": node_id, "to": sid, "weight": 1, "reason": f"uses {tag}"})

    return nodes, edges


def scan_skills():
    """Walk ~/.claude/plugins/marketplaces/*/skills and ~/.claude/skills/"""
    extra_nodes = {}
    extra_edges = []
    candidates = [
        CLAUDE_DIR / "skills",
        CLAUDE_DIR / "plugins" / "marketplaces",
    ]
    for root in candidates:
        if not root.exists():
            continue
        for sk_md in root.rglob("SKILL.md"):
            name = sk_md.parent.name
            sid = "skill_" + re.sub(r"[^a-z0-9_]", "_", name.lower())
            text = sk_md.read_text(encoding="utf-8", errors="ignore")
            fm, _ = parse_frontmatter(text)
            label = fm.get("name", name)
            desc = fm.get("description", "")[:200]
            extra_nodes[sid] = {
                "id": sid,
                "type": "skill",
                "label": label,
                "description": desc,
                "path": str(sk_md.parent),
            }
            extra_edges.append({"from": "core", "to": sid, "weight": 1, "reason": "available skill"})
    return extra_nodes, extra_edges


def scan_learned_skills():
    """Walk memory/skills/learned/*.md - Voyager-pattern saved procedures."""
    extra_nodes = {}
    extra_edges = []
    learned_dir = MEM_DIR / "skills" / "learned"
    if not learned_dir.exists():
        return extra_nodes, extra_edges
    for md in learned_dir.glob("*.md"):
        text = md.read_text(encoding="utf-8", errors="ignore")
        fm, _ = parse_frontmatter(text)
        name = fm.get("name", md.stem)
        sid = "learned_" + re.sub(r"[^a-z0-9_]", "_", name.lower())
        desc = fm.get("description", "")[:200]
        try:
            uses = int(fm.get("uses", 0) or 0)
            success = int(fm.get("success", 0) or 0)
            fail = int(fm.get("fail", 0) or 0)
        except (TypeError, ValueError):
            uses = success = fail = 0
        rate = (success / max(1, success + fail)) if (success + fail) else 0.0
        extra_nodes[sid] = {
            "id": sid,
            "type": "learned_skill",
            "label": name,
            "description": desc,
            "path": str(md),
            "uses": uses,
            "success": success,
            "fail": fail,
            "success_rate": rate,
        }
        extra_edges.append({"from": "core", "to": sid, "weight": 1, "reason": "learned skill"})
        # link to projects mentioned in frontmatter
        proj_raw = fm.get("projects", "")
        if proj_raw:
            for p in re.findall(r"[a-z_]+", proj_raw.lower()):
                if p.startswith("proj_"):
                    extra_edges.append({"from": sid, "to": p, "weight": 2, "reason": "learned for project"})
    return extra_nodes, extra_edges


def scan_goals():
    """Walk memory/goals/active.yaml - Phase 8b active goal nodes."""
    extra_nodes = {}
    extra_edges = []
    yaml_path = MEM_DIR / "goals" / "active.yaml"
    if not yaml_path.exists():
        return extra_nodes, extra_edges
    try:
        import yaml as _yaml
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
    except Exception:
        return extra_nodes, extra_edges
    for gid, g in (data.get("goals") or {}).items():
        # only show non-dropped, non-done
        if g.get("status") in ("dropped", "done"):
            continue
        nid = "goal_n_" + re.sub(r"[^a-z0-9_]", "_", gid.lower())
        extra_nodes[nid] = {
            "id": nid,
            "type": "goal",
            "label": g.get("title", gid)[:50],
            "description": g.get("title", "") + " — " + (g.get("why", "") or "")[:200],
            "status": g.get("status"),
            "priority": g.get("priority"),
            "progress": g.get("progress", 0),
            "project_id": g.get("project"),
            "blockers": g.get("blockers") or [],
            "due": g.get("due"),
        }
        extra_edges.append({"from": "core", "to": nid, "weight": 3, "reason": "active goal"})
        proj = g.get("project")
        if proj:
            extra_edges.append({"from": nid, "to": proj, "weight": 3, "reason": "goal targets project"})
    return extra_nodes, extra_edges


def scan_causal():
    """Phase 8c — load activity/causal_edges.json and inject as edges with kind=causal."""
    out_edges = []
    path = GRAPH_DIR / "activity" / "causal_edges.json"
    if not path.exists():
        return out_edges
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return out_edges
    for e in data.get("edges", []):
        out_edges.append({
            "from": e["from"],
            "to": e["to"],
            "weight": max(1, int(round((e.get("confidence", 0.3) or 0.3) * 4))),
            "reason": e.get("kind") or "causal",
            "causal": True,
            "confidence": e.get("confidence"),
            "gap_minutes": e.get("gap_minutes"),
        })
    return out_edges


def scan_gaps():
    """Walk activity/gaps.jsonl - Phase 9 uncertainty markers grouped by project."""
    extra_nodes = {}
    extra_edges = []
    path = GRAPH_DIR / "activity" / "gaps.jsonl"
    if not path.exists():
        return extra_nodes, extra_edges
    # group by project_id, count + last marker
    groups = {}
    for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            g = json.loads(ln)
        except Exception:
            continue
        pid = g.get("project_id") or "_unassigned"
        slot = groups.setdefault(pid, {"count": 0, "markers": {}, "last_ts": 0, "last_snippet": ""})
        slot["count"] += 1
        m = g.get("marker") or "?"
        slot["markers"][m] = slot["markers"].get(m, 0) + 1
        ts_val = g.get("ts") or 0
        if isinstance(ts_val, str):
            try:
                ts_val = int(time.mktime(time.strptime(ts_val[:19], "%Y-%m-%dT%H:%M:%S")))
            except Exception:
                ts_val = 0
        if ts_val > slot["last_ts"]:
            slot["last_ts"] = ts_val
            slot["last_snippet"] = (g.get("snippet") or "")[:100]
    for pid, info in groups.items():
        if info["count"] < 2:
            continue
        nid = "gap_" + pid
        top_marker = max(info["markers"], key=info["markers"].get)
        extra_nodes[nid] = {
            "id": nid,
            "type": "gap",
            "label": f"gaps ({info['count']})",
            "description": info["last_snippet"],
            "project_id": pid if pid != "_unassigned" else None,
            "count": info["count"],
            "top_marker": top_marker,
        }
        if pid != "_unassigned":
            extra_edges.append({"from": pid, "to": nid, "weight": 1, "reason": "uncertainty markers"})
        else:
            extra_edges.append({"from": "core", "to": nid, "weight": 1, "reason": "ungrouped gaps"})
    return extra_nodes, extra_edges


def scan_decisions():
    """Walk activity/decisions.jsonl - Phase 5a decision log nodes."""
    extra_nodes = {}
    extra_edges = []
    path = GRAPH_DIR / "activity" / "decisions.jsonl"
    if not path.exists():
        return extra_nodes, extra_edges
    seen = {}
    for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        did = d.get("id")
        if not did:
            continue
        # only show last 50 decisions to keep graph from bloating
        seen[did] = d
    items = list(seen.values())[-50:]
    for d in items:
        nid = "dec_" + d["id"][:10]
        outcome = (d.get("outcome") or "").lower()
        text = (d.get("decision_text") or "")[:140]
        extra_nodes[nid] = {
            "id": nid,
            "type": "decision",
            "label": text[:60],
            "description": text,
            "date": d.get("date"),
            "project_id": d.get("project_id"),
            "outcome": outcome or None,
            "match_pattern": d.get("match_pattern"),
            "session_id": d.get("session_id"),
        }
        proj = d.get("project_id")
        if proj:
            extra_edges.append({"from": proj, "to": nid, "weight": 1, "reason": "decision in project"})
        else:
            extra_edges.append({"from": "core", "to": nid, "weight": 1, "reason": "decision (unassigned)"})
    return extra_nodes, extra_edges


def scan_mcps():
    extra_nodes = {}
    extra_edges = []
    if not SETTINGS_FILE.exists():
        return extra_nodes, extra_edges
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return extra_nodes, extra_edges
    mcps = data.get("mcpServers", {})
    for name in mcps:
        mid = "mcp_" + re.sub(r"[^a-z0-9_]", "_", name.lower())
        extra_nodes[mid] = {
            "id": mid,
            "type": "mcp",
            "label": name,
            "description": "MCP server",
        }
        extra_edges.append({"from": "core", "to": mid, "weight": 1, "reason": "configured MCP"})
    return extra_nodes, extra_edges


def dedup_edges(edges):
    seen = {}
    for e in edges:
        key = (e["from"], e["to"])
        if key in seen:
            seen[key]["weight"] += e.get("weight", 1)
        else:
            seen[key] = dict(e)
    return list(seen.values())


def add_semantic_edges(nodes, edges):
    """Load embeddings.json (if exists) and add semantic edges between memory/skill nodes.
    Each node gets edges to top-3 most similar OTHER nodes (cosine > 0.45).
    Edge type: 'semantic' — visualizer can render distinctly.
    """
    emb_path = GRAPH_DIR / "embeddings.json"
    if not emb_path.exists():
        return 0
    try:
        data = json.loads(emb_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    items = data.get("items", {})
    if not items:
        return 0
    # Build matrix of node_id -> vec (only for nodes that exist in graph)
    ids = [nid for nid in items if nid in nodes]
    if len(ids) < 2:
        return 0
    vecs = [items[nid]["vec"] for nid in ids]
    # Pairwise cosine (vectors already normalized in embed.py)
    n = len(vecs)
    added = 0
    THRESHOLD = 0.45
    TOP_K = 3
    for i, nid_a in enumerate(ids):
        sims = []
        va = vecs[i]
        for j, nid_b in enumerate(ids):
            if i == j:
                continue
            vb = vecs[j]
            # cosine = dot (vectors normalized)
            s = sum(x * y for x, y in zip(va, vb))
            if s >= THRESHOLD:
                sims.append((s, nid_b))
        sims.sort(reverse=True)
        for s, nid_b in sims[:TOP_K]:
            edges.append({
                "from": nid_a,
                "to": nid_b,
                "weight": round(s, 3),
                "reason": "semantic similarity",
                "type": "semantic",
            })
            added += 1
    return added


def run_embed():
    """Refresh embeddings.json before computing semantic edges. Best-effort."""
    import subprocess
    embed_script = GRAPH_DIR / "embed.py"
    if not embed_script.exists():
        return
    try:
        # short timeout: cached pass usually <2s; cold pass ~30s
        subprocess.run(["py", str(embed_script)], cwd=GRAPH_DIR, timeout=120, check=False)
    except Exception as e:
        print(f"embed refresh failed: {e}")

def main():
    # refresh embeddings first so semantic edges are current
    run_embed()
    nodes, edges = scan_memory()
    sk_nodes, sk_edges = scan_skills()
    mcp_nodes, mcp_edges = scan_mcps()
    ln_nodes, ln_edges = scan_learned_skills()
    dec_nodes, dec_edges = scan_decisions()
    goal_nodes, goal_edges = scan_goals()
    gap_nodes, gap_edges = scan_gaps()

    for n in list(sk_nodes.values()) + list(mcp_nodes.values()) + list(ln_nodes.values()) + list(dec_nodes.values()) + list(goal_nodes.values()) + list(gap_nodes.values()):
        if n["id"] not in nodes:
            nodes[n["id"]] = n
    edges += sk_edges + mcp_edges + ln_edges + dec_edges + goal_edges + gap_edges
    edges += scan_causal()

    # connect project groups -> core
    for nid, n in nodes.items():
        if n["type"] == "project" and nid != "core":
            edges.append({"from": "core", "to": nid, "weight": 2, "reason": "active project"})

    # semantic edges before dedup so dedup can keep max-weight version
    sem_added = add_semantic_edges(nodes, edges)
    if sem_added:
        print(f"semantic edges added: {sem_added}")

    edges = dedup_edges(edges)
    # drop edges referring missing nodes
    nset = set(nodes.keys())
    edges = [e for e in edges if e["from"] in nset and e["to"] in nset and e["from"] != e["to"]]

    out = {
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "by_type": {},
        },
    }
    for n in nodes.values():
        t = n["type"]
        out["stats"]["by_type"][t] = out["stats"]["by_type"].get(t, 0) + 1

    out_path = GRAPH_DIR.parent / "graph.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    # also write JS file so index.html can load without HTTP server (file:// blocks fetch)
    js_path = GRAPH_DIR / "graph_data.js"
    js_path.write_text("window.GRAPH_DATA = " + json.dumps(out, ensure_ascii=False) + ";\n", encoding="utf-8")
    print(f"wrote {out_path}")
    print(f"wrote {js_path}")
    print(f"nodes: {len(nodes)}  edges: {len(edges)}")
    print(f"by type: {out['stats']['by_type']}")


if __name__ == "__main__":
    main()
