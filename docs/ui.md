# Analyst UI and API (task 004)

Run: `uv run python -m heimdall.api.demo` (synthetic data, port 8000) and `cd web && npm run dev`
(port 5173, proxies `/api`). Production: `npm run build`; FastAPI serves `web/dist` at `/`.
The API binds 127.0.0.1 by default (`api.host`).

## Current layout (tasks 035–037; supersedes older notes below where they differ)
- **Top bar**: brand, clock (display time zone, default Singapore; Settings → Time zone), live/freshness on the left;
  search centred (areas, alerts, loaded flights/ships, `lat, lon`; local data only); **Alerts**, Timeline and
  **Shift report** on the right.
- **Left rail** (widens on hover): Area of Ops `o`, Data sources `u`, Settings `,`, Help `?`, About `i`. No actions board.
- **Bottom left**: map-type picker (Google-Maps style), **Filters** button, minimised legend chip.
- **Bottom right**: recenter (SE Asia), compass, 3D/2D, zoom. Zooming out past ~2.7 turns the map into an offline globe
  with live satellites (`GET /api/satellites`) drawn at altitude with past/next paths.
- **Areas of Ops**: none by default; drawn circles/polygons limited to 250 nm; monitored areas outlined green.
- **Alerts**: two categories, **Critical** (red, small pop-up card on the map from zoom 3) and **Warning**. Details are one
  factual view: detected time, area, position + MGRS, first/last seen, sources, confidence %, affected aircraft/vessels,
  and "Why it was flagged" (value vs alert threshold and normal).
- **Alert workflow (task 042, `web/src/workflow.ts`)**: the Alerts window has four tabs with counts. **Needs action**
  (new; inline **Acknowledge** = "I own this") → **In progress** (acknowledged / under review / shelved: run the checklist
  for the alert type, then **Confirm real**, **False alarm** with a reason, or **Shelve** 2 / 8 / 24 h) → **Confirmed**
  (real, being tracked; **Close: ended** once there is no fresh evidence for 60 min) → **Closed** (resolved, false alarm,
  expired; **Reopen**). Details open with a "Next step" box; cards show whether the activity is still ongoing.
- **Military roles and aircraft list (task 046)**: military planes are coloured by role (tanker, transport,
  surveillance, command post, bomber, fighter, helicopter, drone, VIP, trainer). Role chips (Filters → Quick filters,
  the Military panel's role bars, the list) are one shared filter. **Aircraft list** (rail, key `y`): All / In view /
  Selected tabs, sortable columns, text filters, select + Hide others, CSV / KML export.
- **Aircraft investigation (task 047)**: "Investigate" on a military aircraft card, a list row or a Military panel row
  opens what it is (type, operator, country, reference capabilities), where it took off (OurAirports + its own track,
  or "first seen"), what it may be doing now (likely / possible, with the measured reasons and context) and its 72 h
  track coloured by altitude. `GET /api/military/aircraft/{hex}`.
- **Context layers and news (task 049)**: Filters → *Context (open sources)*: **Military bases** (OpenStreetMap) and
  **Conflict events, 2+ sources** (GDELT machine-coded news; hover shows source count and the caveat). **News**
  (rail, key `n`): BBC World and Al Jazeera headlines, display only.
- **Regions (task 050)**: type a region or place in the search box ("Gulf", "Taiwan", "hormuz", "Global") to jump to
  one of 18 preset views.
- **Theatre activity (task 051)**: top of the Military panel. Each theatre's level (Quiet / Watch / Elevated / High) is
  the number of components above that theatre's own normal; click a theatre to see every component with its numbers.
- **Air force watch (task 056, rail, key `a`)**: one air force's visible fleet by status (airborne, home base,
  overseas detachment, elsewhere at home, abroad), notes, movements between countries, flights per day; click an
  aircraft to investigate. Ten air forces (058): RSAF, RMAF, TNI-AU, PAF, RTAF, RoCAF, JASDF, ROKAF, RAAF, RNZAF
  (`config/air_forces.yaml`). `GET /api/airforces`, `GET /api/airforces/{key}?days=`; the first load reads the lake
  for all fleets at once (~10 s), later switches are instant.
- **Detector tuning (task 061, rail, key `k`)**: per detector, alerts, reviews, share confirmed real with a 95 %
  range, false-alarm reasons, and threshold proposals that keep every confirmed alert (with the `config/config.yaml`
  line and a Copy button). Read-only; proposals appear after 5 false alarms and 3 confirmed alerts. `GET /api/tuning?days=`.
- **Demo (task 043, rail, key `p`)**: plays a precomputed demo pack (`data/replay/*/*.demo.json`) on the live server.
  The snapshot layer replaces live traffic and live alert markers until **Exit demo**; demo alerts open read-only.
  Nothing is replayed server-side, so live detection is unaffected.
- **Shift report (`h`, task 044)**: rule-based **Trends** (this period vs the previous one, by type, area and band,
  spectrum-board anomalies) always; **Summarise with local AI** when a local Ollama model is running (loopback only),
  with cited alert refs and any unsupported refs flagged.
- **Filters** only show/hide data (never change ingestion): Data type (source) rows such as Aircraft (ADS-B), GNSS and
  radio spectrum, Earth observation, alerts/notices, detections (ship gone dark, military aircraft circling, …), quick filters.
- **Data sources**: plain names, On/Off switch with no confirmation; click a keyed source for an API key pop-up.
- **Offline basemap**: regional `world.pmtiles` (z0–10) plus optional whole-world `overview.pmtiles` (z0–6, `WORLD=1`).
- Loading screen at start-up; busy pill for slower actions. Guided tour: 10 short steps with example scenes.

## Views (original task 004 design)
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
4. **Actions board** - removed from the UI in task 037 (`/api/actions` and `/api/playbook` remain).
5. **Data health** - per source freshness, records/hour, error rate, reliability grade.

Layout: 3 columns at 1280 px, detail overlay below 1100 px, single panel with tab bar at 390 px.
Dark theme default, light via toggle (persisted) or `prefers-color-scheme`. All text from the API is
inserted with `textContent` (untrusted-text rule).

## Keyboard shortcuts
Press `?` in the app for this table plus the legend, Admiralty meanings and detector explainers.
Shortcuts are ignored while typing in a field; `Esc` always closes the topmost thing.

| Key | Action |
|---|---|
| `/` | Search (top centre) |
| `j` / `k` | Next / previous alert |
| `Enter` | Open highlighted alert |
| `a` | Acknowledge |
| `d` | Clear |
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
| POST | `/api/alerts/{id}/status` | `{status, reason?, shelve_until?}`; transitions in `heimdall/api/triage.py` (`resolved` = real, activity over; reason needed for dismissed / shelved) |
| GET | `/api/demo/packs`, `/api/demo/packs/{name}` | demo packs (steps, snapshots, alert list); `/api/alerts/{id}` falls back to packs (`demo: true`) |
| GET | `/api/brief/status`, `/api/brief/trends?hours=` | local model availability; fact sheet + trend lines |
| POST | `/api/brief/summary` | `{hours}`; local-model narrative with `cited` / `unsupported` refs; 503 if no local model |
| GET | `/api/cygnss/days`, `/api/cygnss/day?date=` | CYGNSS L1 noise day map (059): cells worth a look with excess, rise vs own baseline, `flag` level/change |
| GET | `/api/tuning?days=` | detector precision from verdicts and threshold proposals (061, read-only) |
| GET | `/api/jammers` | merged jammer fits (GeoJSON), see Map layers |
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
  (grey on the ground, teal low, violet high). Military aircraft use the same plane icon in amber. A thin amber ring
  means a GPS problem; a red halo means the aircraft or ship is part of an alert. Ships are arrows along
  their course (a dot when stationary), coloured by type. Below zoom 5 one marker per screen cell is shown
  with a count. Hover for a one-line tooltip; click for a card (details, 30 min / 2 h path, Follow,
  Show related alert).
- **Layers & filters** (`filters.ts`): toggles for aircraft, ships, alerts, warnings/NOTAMs;
  per-source tick boxes with live counts; military only, altitude band, ship type chips,
  only things in alerts, callsign/name/MMSI search. Saved in `hm.filters.v1`. Also holds the base map,
  overlays, coverage and look-back controls.
- **Data sources** (`sources.ts`): grouped plain-named sources with status, last update and an On/Off switch
  (`POST /api/sources/{key}`, no confirmation). Refreshes every 10 s. Clicking a keyed source opens a key pop-up
  (`keysui.ts`); keys are never shown back.
- **Warnings and NOTAMs** (`notices.ts`): hatched areas from `/api/notices`; click for reference,
  type, valid times (Z), altitude band and the original text in an "unverified" box.

## RF band ladder and spectrum layers (task 030, ADR-008)
- **Band ladder** (left edge of the map; a dropdown under 600 px): HF · VHF marine · L 1090 · L1 GNSS · L 1.4 GHz · C SAR,
  in frequency order. Each row: open-alert count for that band (from `Alert.bands`), and a status dot from the
  sources that feed it (grey = no source yet, amber = unhealthy or needs a key, green = receiving). Click filters the
  alert queue and the map's alert markers to that band; click again clears. Deep link `#band=l1_gnss`. Hover shows the
  feeding sources and, for L1 GNSS / L 1090 / VHF marine, receivers in view over the last hour (`/api/coverage`).
- **Layers panel** is split by tempo: **Live** (aircraft, ships, alerts, notices, HF spots, SatNOGS passes) and
  **Daily sweep** (GNSS interference map, CYGNSS GPS noise, Sentinel-1 C-band RFI, SMAP L-band RFI, heat spots, image footprints, passes).
  Event layers show the time of their newest data, so day-old data never looks live.
- **GNSS interference (daily, GPSJam method)**, on by default: H3 r4 hexes coloured with GPSJam's legend (green < 2 %,
  yellow 2–10 %, red > 10 % of aircraft degraded, after the −1 de-noise); cells without aircraft are not drawn. Date
  picker (Last 24 h or a finished UTC day) with ‹ › buttons, deep link `#gnss=YYYY-MM-DD`, "incomplete" when the day
  has < 22 h of data. Hover: "GNSS: 23 of 24 aircraft degraded · 91.67% …". Descriptive only: never an alert.
- **HF propagation spots**: circles at path midpoints (WSPR + PSK Reporter, last hour), size by spot count.
  **Satellite passes**: SatNOGS ground stations, red when the pass was bad/failed. **C-band interference**:
  Sentinel-1 footprints, red outline when ESA's RFI annotation flags interference. **L-band interference**: SMAP
  cells shaded by the share of flagged footprints. **GPS noise from orbit (CYGNSS, task 059)**: H3 r3 cells for one
  UTC day (newest by default, picker for earlier days), amber by closeness to the CYGNSS detector's thresholds, red
  outline when a cell meets its alert rules (level or rise over the cell's own baseline). Quiet cells are not drawn
  (open ocean < 1 dB, land / coast < 2 dB unless risen). 38° S – 38° N only, 2–3 days delay.

## Spectrum watch, the watch-floor entry point (task 033, key `w`, open on first run)
- **Board**: areas (rows) × bands (columns: L1 GNSS, L 1090, VHF marine, HF, Sat downlinks, L 1.4 GHz, C SAR). Each
  cell shows the state, the current value and a 24 h sparkline. States: **Anomalous** (red, ≥ 3 spreads from typical in
  the direction that means trouble), **Elevated** (warning tone, ≥ 2), **Normal**, **Learning** (baseline too short)
  and **No coverage** (too few receivers; never read as quiet). Each cell is judged against that area's own history.
- **Investigate** (click a cell): value vs typical, spreads, receivers/observations and history length, the 24 h trend,
  natural explanations checked (space weather for HF and GNSS), open alerts in that area and band, and a next-steps
  checklist per band. The click also filters alerts to the band (`#band=`) and flies the map to the area.
- **Band ladder**: a left-edge colour marks each band's worst state across areas.
- **Status bar**: space-weather chip (Kp, G/R/S scales, NOAA SWPC), outlined when a storm or radio blackout can explain
  HF or GPS effects.

## More map layers from existing data (Live group)
- **Estimated jammer locations**: one ellipse and centre per estimated emitter (`GET /api/jammers`). Since 041 only
  radio-horizon fits are drawn (≥ 5 degraded aircraft, ≥ 70 % agreement, ellipse ≤ 300 km; no centroid fallback) and
  fits within 150 km are merged, listing every alert behind them; hover shows the share of aircraft consistent with it.
- **Receivers heard**: hexes of receivers per cell over the last hour for the band picked in the ladder (L1 GNSS,
  L 1090 or VHF marine; `GET /api/coverage`), which shows where "no data" is really "no receivers".
- **HF spots**: band selector (3–28 MHz) and brightness by median SNR.
- **Only official GPS-interference notices** (notices drawn in the attention colour) and **Only aircraft with GPS
  problems** (quick filter).
