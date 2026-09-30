# Analyst UI and API (task 004)

Run: `uv run python -m heimdall.api.demo` (synthetic data, port 8000) and `cd web && npm run dev`
(port 5173, proxies `/api`). Production: `npm run build`; FastAPI serves `web/dist` at `/`.
The API binds 127.0.0.1 by default (`api.host`).

## Views
1. **Map** - MapLibre + deck.gl with a layer switcher (top-right "Layers"). Heimdall layers are a
   separate deck.gl overlay canvas (`interleaved: false`) so they stay on top of every base and
   overlay. Choices persist in localStorage (`hm.base`, `hm.overlays`). See "Map layers" below. H3 r4 hexes coloured by degraded ratio
   (green <2%, amber 2-10%, red >10%), alert markers coloured by kind, orbit tracks and emitter
   ellipses for the selected alert, AOI outlines (control AOIs grey), coverage toggle (quality
   aircraft per cell), time slider (as-of now .. 24 h ago).
2. **Queue** - sorted by severity x confidence; filters AOI/kind/status/grade; live via SSE
   (`/api/stream`), 60 s polling fallback.
3. **Evidence card** - rules table (value / op / threshold / baseline / pass), coverage and
   persistence, Admiralty grade, corroboration matrix (uncorroborated is shown as such, never
   "false"), counterfactual, gaps, entities and records, metric-vs-baseline chart.
4. **Actions board** - only ids returned by `/api/playbook` for the alert kind. Propose -> Approve /
   Reject; analyst Confirm / Dismiss with a reason. Approval is a stub: it records, runs nothing.
5. **Data health** - per source freshness, records/hour, error rate, reliability grade.

Layout: 3 columns at 1280 px, detail overlay below 1100 px, single panel with tab bar at 390 px.
Dark theme default, light via toggle (persisted) or `prefers-color-scheme`. All text from the API is
inserted with `textContent` (untrusted-text rule).

## Keyboard shortcuts
Press `?` in the app for this table plus the legend, Admiralty meanings and detector explainers.
Shortcuts are ignored while typing in a field; `Esc` always closes the topmost thing.

| Key | Action |
|---|---|
| `/` | Search alerts |
| `j` / `k` | Next / previous alert |
| `Enter` | Open highlighted alert |
| `a` / `r` / `c` | Acknowledge / Under review / Confirm |
| `d` / `s` | Dismiss / Shelve (both ask for a reason; shelve also asks until when) |
| `n` | Add analyst note |
| `f` | Fly to selection |
| `1`-`5` | Jump to AOI 1-5 |
| `l` | Layers drawer |
| `t` | Show / hide timeline |
| `Space` | Play / pause timeline playback |
| `Esc` | Close dialog, form or inspector |
| `?` | Help |

## Timeline, map tools and handover
- Timeline: alerts opened per 30 min over 24 h, stacked by kind. Drag to set the global time window
  (queue and map follow), click to clear. Playback advances 1 min of data per second at 1x (10x, 60x);
  "Now" returns to the live view.
- Map: cursor lat/lon and MGRS, nautical-mile scale bar, Measure (click points) and Range ring (click
  centre, then radius), grey outline where fewer than 3 quality aircraft were seen, selection halo,
  clustering below zoom 5, hover mini-cards.
- Shift summary (status bar "Shift"): opened / closed / shelved / still open by AOI for the last 4-24 h,
  downloadable as a local Markdown file (nothing is uploaded). "Closed" uses last-seen time.
- Status bar alert rate compares the last hour with the 24 h hourly average (flood indicator).

## API contract
| Method | Path | Notes |
|---|---|---|
| GET | `/api/alerts?status=&aoi=&since=` | `status` comma list; `since` ISO-8601 |
| GET | `/api/alerts/{id}` | `{alert, evidence[], feedback[], actions[], playbook[]}` |
| GET | `/api/evidence/{id}` | Evidence |
| GET | `/api/cells?res=4&since=` | latest evidence per H3 cell: `ratio, score, coverage, geometry` (default last 24 h) |
| GET | `/api/tracks/{hex}?since=` | aircraft track points |
| GET | `/api/metrics/{key}/{metric}?since=` | baseline series (key = h3 cell) |
| GET | `/api/aois`, `/api/health`, `/api/playbook` | config / source health / allow-list |
| POST | `/api/alerts/{id}/feedback` | `{verdict: confirm|dismiss, reason}`; sets alert status |
| POST | `/api/alerts/{id}/actions` | `{action_id}`; 422 unless in playbook and `applies_to` matches; status `proposed`; returns `id` |
| POST | `/api/actions/{id}/approve`, `/reject` | id = `ActionRequest.id` (uuid4 hex, ADR-002); only for `proposed`; approve sets `approved` (recording stub, runs nothing) |
| GET | `/api/stream` | SSE: `snapshot` (all alerts) on connect, then `alert` per change |
| GET | `/api/ui-config` | `{basemap: openfreemap|carto|none, overlays: [...]}` |

`GET /api/aois` includes a display `label` per AOI.

## Security and storage
- `TrustedHostMiddleware` allows only `127.0.0.1` / `localhost`; POSTs whose `Origin` does not
  match `Host` get 403.
- DuckDB is single-writer: observations/evidence/alerts come from a read-only store; analyst
  feedback and actions go to a separate analyst store via `CompositeStore`
  (`heimdall/adapters/composite_store.py`). Alerts are never copied: only a status override
  (alert_id -> status) is applied over the pipeline alert. It is re-derived after a restart from the
  latest journal status note, falling back to the latest feedback verdict. Pipeline upserts go to
  the main store and never overwrite analyst status. Shelving expires to acknowledged with a
  `system` status note in the journal. `heimdall.run` uses `data/analyst.duckdb` for analyst state.
- SSE store calls run in `asyncio.to_thread`.
- `create_app(store)` requires a store (the in-process API in `heimdall.run` passes its own);
  the standalone entry is `python -m heimdall.api.demo` only. `auth_check` middleware is a no-op
  while `api.auth == none`.

## Map layers
Config: `ui.basemap: openfreemap|carto|none` (default `openfreemap`) and `ui.overlays`
(default-on). `none` is the fully private option: blank local style, only the local graticule
overlay, zero third-party requests. Otherwise the layer panel shows "Map tiles from third parties
see viewed areas".

| Layer | Source | License / attribution |
|---|---|---|
| Dark, Light, Detailed (liberty), Bright | OpenFreeMap vector styles (`tiles.openfreemap.org/styles/...`), no key | OpenFreeMap, OpenMapTiles schema, data (c) OpenStreetMap contributors (ODbL) |
| CARTO dark/light (if `carto`) | basemaps.cartocdn.com | CARTO, OSM contributors |
| Satellite | EOX Sentinel-2 cloudless 2024 WMTS | Sentinel-2 cloudless by EOX, CC BY-NC-SA 4.0 (non-commercial) |
| Country borders + labels | OpenFreeMap `boundary` (admin_level 2 bright, disputed dashed) and `place` country labels | OSM (ODbL) |
| Military areas + airports | OpenFreeMap `landuse` class=military (red hatch), `aeroway` runways, `aerodrome_label` (military red) | OSM (ODbL) |
| EEZ boundaries | Marine Regions WMS `MarineRegions:eez_boundaries` | Marine Regions (VLIZ), CC-BY |
| Seamarks | OpenSeaMap `tiles.openseamap.org/seamark` | OpenSeaMap contributors, ODbL |
| Graticule | local | - |

Every source sets its `attribution` so MapLibre's attribution control lists all active sources.
Overlay text layers are skipped on CARTO bases (different glyph fonts). Overlays draw their own
boundary layers from an OpenFreeMap vector source, so they also work over the satellite base.

## Windows, toolbar and live data (task 012)
The map fills the screen. Everything else floats above it in windows you can move, resize, minimise
and close (layout saved in `hm.wm.v1`; below 768 px they become bottom sheets). The left toolbar opens
them: Alerts `q`, Areas `b`, Layers & filters `l`, Data sources `u`, Actions `x`, Timeline `t`,
Shift report `h`, Help `?`. The alert details window opens beside the selected alert's marker
(or beside the alert list) and remembers where you move it.

- **Live traffic** (`web/src/traffic.ts`): aircraft and ships from `/api/traffic/air|sea`, only for
  the visible box. Air refreshes every 5 s, ships every 30 s; polling pauses in a hidden tab and
  requests never overlap. Aircraft are silhouettes turned to their track and tinted by altitude
  (grey on the ground, teal low, violet high). Military aircraft are hollow diamonds. A thin amber ring
  means a GPS problem; a red halo means the aircraft or ship is part of an alert. Ships are arrows along
  their course (a dot when stationary), coloured by type. Below zoom 5 one marker per screen cell is shown
  with a count. Hover for a one-line tooltip; click for a card (details, 30 min / 2 h path, Follow,
  Show related alert).
- **Layers & filters** (`filters.ts`): toggles for aircraft, ships, alerts, warnings/NOTAMs;
  per-source tick boxes with live counts; military only, altitude band, ship type chips,
  only things in alerts, callsign/name/MMSI search. Saved in `hm.filters.v1`. Also holds the base map,
  overlays, coverage and look-back controls.
- **Data sources** (`sources.ts`): status, last update, records per hour and an On/Off switch
  (`POST /api/sources/{key}`; switching off asks "Stop collecting from X?"). Refreshes every 10 s.
  Keys are never shown, only "Add X_KEY to .env".
- **Warnings and NOTAMs** (`notices.ts`): hatched areas from `/api/notices`; click for reference,
  type, valid times (Z), altitude band and the original text in an "unverified" box.
- **Actions** (`actionsboard.ts`): Proposed, Approved, Done, Rejected columns from `/api/actions`.
  Approve and reject appear on proposed cards only; high-risk items ask for a second confirmation.
