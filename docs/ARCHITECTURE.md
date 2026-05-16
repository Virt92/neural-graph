# Architecture

## Data flow

```
Claude Code CLI
   │
   │ hooks (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionEnd)
   ▼
hook_handler.py
   │
   ├──► memory/graph/activity/sessions.json    (live session state)
   ├──► memory/graph/activity/traffic.jsonl    (per-event log)
   ├──► memory/graph/activity/events.log       (free-form log)
   └──► memory/graph/activity/processes.json   (process_watcher snapshot)
            │
            │ polled every 250-500ms
            ▼
Electron viewer (memory/graph/index.html)
   │
   │ reads memory/graph/graph_data.js
   ▼
3D force-directed graph (3d-force-graph + THREE.js)
```

## Node types

| Type | ID prefix | Source | Color |
|---|---|---|---|
| Core (static) | `core` | seeded by `build_graph.py` | hidden by default |
| Session core | `core_<sid8>` | created per Claude session | warm white |
| Project | `proj_<slug>` | scanned dirs / inferred from CLAUDE_PROJECT_DIR | blue/purple |
| Skill | `skill_<name>` | `memory/skills/*/SKILL.md` | green |
| MCP | `mcp_<name>` | hook events | orange |
| Memory | `mem_<file>` | `memory/*.md` | yellow |
| Decision | `dec_<id>` | `memory/graph/decisions/*.json` | pink |
| Goal | `goal_<id>` | `memory/graph/goals/active.yaml` | red |

## Edge types

| Type | Created by | Visibility |
|---|---|---|
| `static` | `build_graph.py` (project ↔ skill, project ↔ memory etc.) | TOPOLOGY only |
| `hot` | `hook_handler.py` recent activity | ACTIVITY + TRAFFIC |
| `traffic` (animated tube) | `traffic.jsonl` events | TRAFFIC |
| Session-core ↔ Project | runtime, via `SESS_CORE_FX` map | ACTIVITY + TRAFFIC |

## Daemons

| Daemon | Cadence | Purpose |
|---|---|---|
| `process_watcher.py` | 5s | track running Claude/Electron/Node processes |
| infra (Electron tick) | 30s | re-poll sessions/health/traffic |
| `health.py` | 1h | per-project health score (recency × activity × goal progress) |
| `growth.py` | 4h | growth signals (new files, edits, churn) |
| `signals.py` | 6h | anomaly detection on traffic/cost baselines |
| `nightly.bat` | daily 03:13 | full reflect → embed → build_graph chain |

## Modes

- **TOPOLOGY** — static graph, all nodes/edges visible, force layout
- **ACTIVITY** — only currently-hot edges + their endpoints visible (default)
- **TRAFFIC** — TOPOLOGY + animated tubes per traffic event

## Persistence

- `localStorage['neural_graph_layout_v1']` — node positions per browser
- `memory/graph/activity/*` — runtime data, written by hooks
- `memory/graph/state/<date>.json` — daily state snapshots
- `memory/graph/snapshots/*` — graph history (compared diff over time)
