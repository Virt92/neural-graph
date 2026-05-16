"""Project health monitor.

For each project memory file with frontmatter 'paths', probe local working tree:
- git status (clean/dirty), uncommitted file count, untracked count
- last commit (hash, age, subject)
- open TODO/FIXME count (grep -r)

For URLs mentioned in body, optionally probe with HEAD request (timeout 3s).

Output: memory/graph/activity/health.json
{
  "updated_at": <epoch>,
  "projects": {
    "proj_example": {
      "name": "Example",
      "paths": [
        {"path": "C:/path/to/project",
         "exists": true,
         "git": {"branch": "main", "dirty": 0, "untracked": 0, "last_commit": "abc - 1h ago - msg"},
         "todos": 0}
      ],
      "urls": [{"url": "https://example.com", "status": 200, "ms": 87}],
      "overall": "ok|warn|err"
    }
  }
}
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

HERE = Path(__file__).resolve().parent
MEM_DIR = HERE.parent
ACTIVITY_DIR = HERE / "activity"
ACTIVITY_DIR.mkdir(exist_ok=True)
OUT_PATH = ACTIVITY_DIR / "health.json"

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
PATH_RE = re.compile(r"`([A-Za-z]:[\\/][^\s`]+|[\\/][^\s`]{3,})`")
URL_RE = re.compile(r"https?://[a-zA-Z0-9_\-./]+(?::\d+)?(?:/[^\s)`\]]*)?")

# Add (substring_in_path_lowercase, project_id) tuples for your projects.
PROJECT_TAGS = [
]

def infer_project(name_or_path):
    low = name_or_path.lower().replace("\\", "/")
    for tag, pid in PROJECT_TAGS:
        if tag in low:
            return pid
    return None

def parse_frontmatter(text):
    m = FM_RE.match(text)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip().lower()] = v.strip()
    return fm, m.group(2)

def run_git(cwd, args, timeout=5):
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="ignore",
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None

def git_info(path):
    """Return dict with branch/dirty/untracked/last_commit or None if not a git repo."""
    # check if .git exists
    if not (Path(path) / ".git").exists():
        # try parent (for monorepos)
        if not run_git(path, ["rev-parse", "--show-toplevel"]):
            return None
    branch = run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"]) or "?"
    status_lines = run_git(path, ["status", "--porcelain"]) or ""
    lines = status_lines.splitlines()
    dirty = sum(1 for ln in lines if ln and not ln.startswith("??"))
    untracked = sum(1 for ln in lines if ln.startswith("??"))
    last = run_git(path, ["log", "-1", "--format=%h|%cr|%s"])
    return {
        "branch": branch,
        "dirty": dirty,
        "untracked": untracked,
        "last_commit": last or "(no commits)",
    }

def count_todos(path, limit=2000):
    """Count TODO/FIXME/XXX/HACK markers in tracked source files. Capped for speed."""
    try:
        # use git grep — only scans tracked files
        r = subprocess.run(
            ["git", "grep", "-cI", "-E", r"\b(TODO|FIXME|XXX|HACK)\b"],
            cwd=path, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="ignore",
        )
        if r.returncode not in (0, 1):
            return None
        total = 0
        for ln in r.stdout.splitlines():
            if ":" in ln:
                try:
                    total += int(ln.rsplit(":", 1)[1])
                except Exception:
                    pass
            if total >= limit:
                return total
        return total
    except Exception:
        return None

def http_head(url, timeout=3):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"url": url, "status": resp.status, "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"url": url, "status": None, "ms": int((time.time() - t0) * 1000), "error": str(e)[:80]}

def collect_project_data():
    """Walk memory/*.md, group by inferred project, harvest paths + urls."""
    projects = {}  # pid -> {name, paths: set, urls: set, memories: [filenames]}
    for md in sorted(MEM_DIR.glob("*.md")):
        if md.name == "MEMORY.md":
            continue
        try:
            raw = md.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        fm, body = parse_frontmatter(raw)
        # try infer project from filename first
        pid = infer_project(md.stem)
        if not pid:
            continue  # skip non-project memories
        proj = projects.setdefault(pid, {
            "name": pid.replace("proj_", "").replace("_", " "),
            "paths": set(),
            "urls": set(),
            "memories": [],
        })
        proj["memories"].append(md.name)
        # paths from backtick-quoted absolute paths
        for m in PATH_RE.findall(body):
            # normalize to Path-like
            p = m.strip().rstrip("/").rstrip("\\")
            if len(p) < 4 or "*" in p:
                continue
            proj["paths"].add(p)
        # urls from body
        for u in URL_RE.findall(body):
            # skip docs-y urls
            if any(skip in u for skip in ("github.com/anthropics", "anthropic.com/docs", "/d/", "huggingface.co", "unpkg.com")):
                continue
            # keep root or first-path-only URL
            proj["urls"].add(u.rstrip("/.,)"))
    return projects

def overall_status(paths_data, urls_data):
    """Roll-up status: ok / warn / err."""
    if any(u.get("status") and u["status"] >= 500 for u in urls_data):
        return "err"
    if any(u.get("status") is None for u in urls_data):
        return "warn"
    if any((p.get("git") or {}).get("dirty", 0) > 20 for p in paths_data):
        return "warn"
    return "ok"

def process_path(path_str):
    p = Path(path_str)
    rec = {"path": path_str, "exists": p.exists()}
    if not rec["exists"]:
        return rec
    g = git_info(str(p))
    if g:
        rec["git"] = g
        td = count_todos(str(p))
        if td is not None:
            rec["todos"] = td
    return rec

def main():
    projects = collect_project_data()
    out = {"updated_at": time.time(), "projects": {}}

    # parallelize URL probes (path/git is local-fast)
    url_jobs = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for pid, p in projects.items():
            paths_list = list(p["paths"])[:5]  # cap
            paths_data = [process_path(ps) for ps in paths_list]
            urls_list = list(p["urls"])[:5]
            url_futures = [pool.submit(http_head, u) for u in urls_list]
            urls_data = [f.result() for f in url_futures]
            out["projects"][pid] = {
                "name": p["name"],
                "paths": paths_data,
                "urls": urls_data,
                "memory_count": len(p["memories"]),
                "overall": overall_status(paths_data, urls_data),
            }

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH}", flush=True)
    print(f"Projects: {len(out['projects'])}")
    for pid, p in out["projects"].items():
        url_status = ",".join(str(u.get("status") or "?") for u in p["urls"]) or "no-urls"
        path_status = ",".join(f"{p2.get('git',{}).get('dirty','-')}d/{p2.get('git',{}).get('untracked','-')}u" for p2 in p["paths"] if p2.get("exists")) or "no-paths"
        print(f"  {pid} [{p['overall']}] paths={path_status} urls={url_status}")

if __name__ == "__main__":
    main()
