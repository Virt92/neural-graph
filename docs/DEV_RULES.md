# Dev Rules

Rules for working on `memory/graph/` (Electron neural-graph visualizer). Read before changing anything.

## Rule 1 - Additive only, never break existing modes

**Why:** Three distinct visual modes (TOPOLOGY / ACTIVITY / TRAFFIC) are tested in sequence. Multi-step plans have failed before because intermediate steps broke prior modes.

**How to apply:**
- Each phase is additive: new feature ON TOP of existing without removing old behavior
- New code paths gated by `MODE === 'traffic'` etc., never replace default
- Test old mode after each change before moving to next
- If a new feature requires a data structure change, keep old field as fallback

## Rule 2 - Per-session core agents, NOT a global core

**Why:** Honest representation. If the program is running but no Claude session is active, there is no core. A static `core` lies — it claims Claude is present when no session is running.

**How to apply:**
- Use `coreSessId(sid)` helper = `'core_' + sid.slice(0,8)` for HOT_EDGES anchor
- Static `core` from `graph.json` is hidden via `nodeVisibility` unless `MODE==='topology'`
- Each session ghost in `SESSION_NODES[sid]` = its own labeled core (warm-white sprite, big)
- `CORE_SESS_MAP[core_xxx] = full_sid` lookup table
- `findNodePos` resolves `core_*` IDs from `CORE_SESS_MAP` → `SESSION_NODES[sid].position`
- 30s fade-out on SessionEnd via `GHOST_FADING` map, then destruction
- Cores spread far apart: base radius 110, each new session +30 stagger

## Rule 3 - Custom THREE.Line for session-core hot edges

**Why:** 3d-force-graph only renders edges in `GRAPH.edges`. Session-core hot edges (`core_xxx | proj_yyy`) don't exist in graph data — they would be invisible without custom rendering.

**How to apply:**
- `SESS_CORE_FX` Map<ckey, {curve, line, head, head_t, state, ...}> manages animated entries
- `refreshSessCoreLines()` (every 250ms) builds/updates entries from HOT_EDGES
- `animateSessCoreFX()` (every RAF frame) advances snake `head_t` along curve
- `buildCurveBetween(p1, p2)` = bezier with world-Y projection perpendicular × 0.3
- Direction = always FROM `core_xxx` TO target node
- States: `forward` (loop while hot) → `retract` (target → source over 30s when no longer in HOT_EDGES) → destroyed

## Rule 4 - Curve direction match: world-Y projection, NOT cross product

**Why:** First attempt used `cross(dir, Y)` which gives a horizontal vector — wrong. 3d-force-graph uses Y-up projected onto the plane perpendicular to link direction. Without matching, snake goes one way and gray static line goes another.

**How to apply:**
- Wrong: `perp = cross(dir, worldY)` — produces axis perpendicular to BOTH
- Right: `perp = worldY - dir_normalized * (worldY · dir_normalized)` — removes parallel component, keeps Y-bias
- Fallback for vertical edges: substitute world-X
- Use this formula in BOTH `buildCurveBetween` (for SESS_CORE_FX) AND `makeTube` (for traffic tubes)

## Rule 5 - Hot-edge gating to prevent stale-session noise

**Why:** Originally every idle session in `sessions.json` re-fired `updateHotEdges` on every poll cycle, replaying old `recent_files`/`recent_mcps` as live activity. Real "now" was indistinguishable from history.

**How to apply:**
- `updateHotEdges`: only call if `sess.active && age<30s`
- `handleTranscripts`: only fire hot edges if `delta_tokens > 0 && lastAge < 30s` (real new tokens consumed since last poll)
- `handleTraffic`: only fires on actual NEW jsonl appends (size-delta tail with byte offset)
- `getActiveNodeIds`: don't force-add `'core'` (each session uses its own `core_<sid8>`)

## Rule 6 - Position persistence

**Why:** Time invested arranging projects/cores spatially. Layout must survive restart.

**How to apply:**
- Save to `localStorage['neural_graph_layout_v1']` = `{nodeId: {x,y,z,pinned:bool}}`
- Triggers: `onNodeDragEnd` (pin + save), `setInterval 30s` (snapshot all), `beforeunload` (final save)
- On `renderGraph`: `applySavedPositions(nodes)` — pinned set `fx/fy/fz`, others set initial `x/y/z`
- Pinned nodes won't move from force layout; non-pinned use saved as starting point but engine adjusts

## Rule 7 - Don't redesign without confirming

**Why:** Conceptual changes (per-session cores, snake animation, project filter) emerged from user observation, not from default plans. Always discuss before coding.

**How to apply:**
- Big visual/architectural changes: respond with concrete plan + 2-3 design questions BEFORE coding
- Phased approach (A: visual → B: registry → C: persistence) — let user verify each before next
- Small tweaks (color, opacity, radius numbers): just do it
