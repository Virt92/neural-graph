"""External signal watcher (Phase 4b).

Sources:
- Sitemap diff for sites listed in SITEMAPS - detect new/removed pages
- HN Algolia search - keyword mentions (recent)
- Reddit JSON search - keyword mentions (recent)
- GitHub releases - critical deps configured in GH_REPOS

Output: memory/graph/activity/signals.json
{
  "updated_at": <epoch>,
  "items": [
    {"src":"sitemap|hn|reddit|gh", "kind":"new_page|mention|release",
     "title":"...", "url":"...", "ts":<epoch>, "tag":"your_tag", "extra":{}}
  ],
  "summary": {"sitemap":N, "hn":N, "reddit":N, "gh":N}
}

State (for diff baselines): memory/graph/activity/signals_state.json
{ "sitemap":{"example.com":["url","url"]}, "gh_seen":["repo@tag"], ... }

CLI:
  py signals.py              # full pass
  py signals.py --sources gh,hn   # subset
  py signals.py --reset           # wipe state (next run = fresh baseline)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
ACTIVITY_DIR = HERE / "activity"
ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
OUT_PATH = ACTIVITY_DIR / "signals.json"
STATE_PATH = ACTIVITY_DIR / "signals_state.json"

UA = "ClaudeNeuralGraph/1.0 (+local signal watcher)"
TIMEOUT = 8

# Add your sites here: {tag: sitemap_url}
# Example: {"myapp": "https://example.com/sitemap.xml"}
SITEMAPS = {
}

# (display tag, search query) for HN/Reddit
# Example: [("myapp", "example.com")]
KEYWORDS = [
]

# (repo, tag) — repos for which to watch releases
GH_REPOS = [
    ("maplibre/maplibre-gl-js", "maplibre"),
    ("expo/expo", "expo"),
    ("facebook/react-native", "rn"),
    ("electron/electron", "electron"),
    ("mrdoob/three.js", "threejs"),
    ("anthropics/anthropic-sdk-python", "anthropic"),
]


def http_get(url, timeout=TIMEOUT, headers=None):
    headers = headers or {}
    headers.setdefault("User-Agent", UA)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            ct = r.headers.get("Content-Type", "")
            return data, ct
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}"
    except Exception as e:
        return None, f"err {e}"


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"sitemap": {}, "gh_seen": [], "hn_seen": [], "reddit_seen": []}


def save_state(s):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def fetch_sitemap(url):
    """Recursive: handles sitemap-index. Returns set of URLs."""
    data, ct = http_get(url)
    if not data:
        return set(), ct
    out = set()
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        return set(), f"parse {e}"
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    # sitemap-index
    sub = [s.text for s in root.findall(".//sm:sitemap/sm:loc", ns) if s.text]
    if sub:
        for s in sub[:20]:  # cap
            child, _ = fetch_sitemap(s)
            out.update(child)
        return out, "ok"
    # url-set
    for u in root.findall(".//sm:url/sm:loc", ns):
        if u.text:
            out.add(u.text.strip())
    return out, "ok"


def diff_sitemaps(state):
    items = []
    sm_state = state.get("sitemap", {})
    for tag, url in SITEMAPS.items():
        urls, status = fetch_sitemap(url)
        if not urls:
            continue
        prev = set(sm_state.get(tag, []))
        new = urls - prev
        if prev:  # only emit on diffs after baseline established
            for u in sorted(new)[:20]:
                items.append({
                    "src": "sitemap", "kind": "new_page", "tag": tag,
                    "title": u.replace("https://", "").replace("http://", ""),
                    "url": u, "ts": int(time.time()),
                })
        sm_state[tag] = sorted(urls)
    state["sitemap"] = sm_state
    return items


def hn_search(query, hours=72):
    """Algolia HN — public, no auth."""
    since = int(time.time()) - hours * 3600
    qs = urllib.parse.urlencode({
        "query": query,
        "tags": "(story,comment)",
        "numericFilters": f"created_at_i>{since}",
        "hitsPerPage": 10,
    })
    url = f"https://hn.algolia.com/api/v1/search?{qs}"
    data, _ = http_get(url)
    if not data:
        return []
    try:
        j = json.loads(data)
    except Exception:
        return []
    hits = j.get("hits", [])
    return hits


def _contains_keyword(text: str, kw: str) -> bool:
    """Strict-ish: exact substring (case-insensitive). Filters Reddit/HN noise."""
    return kw.lower() in (text or "").lower()


def fetch_hn(state):
    items = []
    seen = set(state.get("hn_seen", []))
    for tag, q in KEYWORDS:
        for h in hn_search(q):
            oid = str(h.get("objectID"))
            if oid in seen:
                continue
            title = h.get("title") or h.get("comment_text") or ""
            title = re.sub(r"<[^>]+>", "", title)[:140]
            url = h.get("url") or ""
            # post-filter: ignore unless full keyword appears in title/url
            haystack = title + " " + url
            if not _contains_keyword(haystack, q):
                continue
            seen.add(oid)
            items.append({
                "src": "hn", "kind": "mention", "tag": tag,
                "title": title,
                "url": url or f"https://news.ycombinator.com/item?id={oid}",
                "ts": int(time.time()),
            })
    state["hn_seen"] = list(seen)[-500:]
    return items


def reddit_search(query):
    """Reddit JSON search — public, but UA-required."""
    qs = urllib.parse.urlencode({"q": query, "sort": "new", "limit": 10, "t": "week"})
    url = f"https://www.reddit.com/search.json?{qs}"
    data, _ = http_get(url, headers={"User-Agent": UA})
    if not data:
        return []
    try:
        j = json.loads(data)
    except Exception:
        return []
    return j.get("data", {}).get("children", [])


def fetch_reddit(state):
    items = []
    seen = set(state.get("reddit_seen", []))
    for tag, q in KEYWORDS:
        for c in reddit_search(q):
            d = c.get("data", {})
            rid = d.get("id")
            if not rid or rid in seen:
                continue
            title = d.get("title", "")
            body = d.get("selftext", "")[:500]
            haystack = title + " " + body + " " + d.get("url", "")
            if not _contains_keyword(haystack, q):
                continue
            seen.add(rid)
            items.append({
                "src": "reddit", "kind": "mention", "tag": tag,
                "title": title[:140],
                "url": "https://reddit.com" + d.get("permalink", ""),
                "ts": int(d.get("created_utc", time.time())),
                "extra": {"sub": d.get("subreddit"), "score": d.get("score")},
            })
    state["reddit_seen"] = list(seen)[-500:]
    return items


def fetch_gh_releases(state):
    items = []
    seen = set(state.get("gh_seen", []))
    is_first = not seen  # baseline mode
    for repo, tag in GH_REPOS:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=3"
        data, _ = http_get(url, headers={"Accept": "application/vnd.github+json"})
        if not data:
            continue
        try:
            arr = json.loads(data)
        except Exception:
            continue
        for r in arr[:3]:
            rname = r.get("tag_name") or r.get("name", "")
            key = f"{repo}@{rname}"
            if key in seen:
                continue
            seen.add(key)
            if is_first:
                continue  # baseline; don't emit on first run
            items.append({
                "src": "gh", "kind": "release", "tag": tag,
                "title": f"{repo} {rname}",
                "url": r.get("html_url", ""),
                "ts": int(time.time()),
                "extra": {"published_at": r.get("published_at"),
                          "name": r.get("name", ""),
                          "prerelease": r.get("prerelease", False)},
            })
    state["gh_seen"] = list(seen)[-500:]
    return items


SOURCES = {
    "sitemap": diff_sitemaps,
    "hn": fetch_hn,
    "reddit": fetch_reddit,
    "gh": fetch_gh_releases,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sitemap,hn,reddit,gh")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.reset:
        for p in (STATE_PATH, OUT_PATH):
            if p.exists():
                p.unlink()
        print("state + output wiped")
        return

    state = load_state()
    all_items = []
    summary = {}
    requested = [s.strip() for s in args.sources.split(",") if s.strip()]
    for src in requested:
        fn = SOURCES.get(src)
        if not fn:
            continue
        try:
            items = fn(state)
        except Exception as e:
            print(f"{src} failed: {e}", file=sys.stderr)
            items = []
        summary[src] = len(items)
        all_items.extend(items)

    # merge with existing recent items (last 14 days), dedupe by url
    existing = []
    if OUT_PATH.exists():
        try:
            prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            cutoff = int(time.time()) - 14 * 86400
            existing = [i for i in prev.get("items", []) if i.get("ts", 0) > cutoff]
        except Exception:
            existing = []
    by_url = {i.get("url", "") + "|" + i.get("title", ""): i for i in existing}
    for i in all_items:
        by_url[i.get("url", "") + "|" + i.get("title", "")] = i
    merged = sorted(by_url.values(), key=lambda x: x.get("ts", 0), reverse=True)[:200]

    out = {
        "updated_at": int(time.time()),
        "summary": summary,
        "new_count": len(all_items),
        "items": merged,
    }
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, OUT_PATH)
    save_state(state)
    parts = " · ".join(f"{k}={v}" for k, v in summary.items())
    print(f"signals.json: {len(merged)} total, {len(all_items)} new ({parts})")


if __name__ == "__main__":
    main()
