# Heimdall

**Multi-band RF / electronic-warfare surveillance engine built on free, open data, Southeast Asia first.**

Heimdall watches receivers that already listen to the spectrum, such as aircraft and ship transponders, amateur HF
stations and satellites. It infers interference and emitters from how those receivers degrade, and raises
**explainable, cross-checked alerts**:

| Band | What Heimdall watches | Status |
|---|---|---|
| L-band 1090 MHz → GNSS L1 | Aircraft ADS-B integrity (NIC/NACp): **GNSS jamming** cells with an emitter estimate, **GNSS spoofing** (five independent signals), military tanker/AEW/ISR orbits | live |
| VHF marine 162 MHz | Ship AIS: **AIS spoofing** (impossible speeds, positions on land), **dark gaps** | live |
| GNSS L1, daily | A GPSJam-style daily interference map rebuilt from Heimdall's own ingest, plus a rolling last-24 h view | live |
| L-band from orbit | SMAP 1.4 GHz interference flags (needs EARTHDATA_TOKEN); CYGNSS L1 noise floor | SMAP and CYGNSS live; CYGNSS map layer (Filters → GPS noise from orbit) |
| C-band 5.4 GHz | Sentinel-1 radar interference annotations (Planetary Computer) | connector live; detector planned |
| HF 3–30 MHz | WSPR and PSK Reporter spots at path midpoints: **HF fade** far below the usual for that time of day (flares, storms and feed drops ruled out); SatNOGS satellite passes | experimental |

Official notices (FAA NOTAMs, NGA navigational warnings) corroborate alerts. Space weather and weather are checked as
natural explanations. NASA FIRMS fire data verifies physical events.

It covers these areas of interest: Myanmar, the South China Sea, the Taiwan/Luzon Strait and the
Malacca/Singapore Strait. The **Baltic** is a calibration control. The results appear in a
C4I-style analyst console with a live operational map, an alert board, evidence, and a triage
workflow.

> Status: **P3 done (v0.3.0); v0.3.1 is the pre-pivot baseline. P4 (spectrum core) is next.** The pivot is recorded in
> [ADR-008](docs/decisions/ADR-008-rf-spectrum-pivot.md), the plan in [docs/roadmap-rf.md](docs/roadmap-rf.md).
> See [CLAUDE.md](CLAUDE.md) for the phase table and [docs/CHANGELOG.md](docs/CHANGELOG.md) for history.

---

## 1. Install

**Requirements:** macOS or Linux, Python 3.12, [uv](https://docs.astral.sh/uv/), and Node.js 20+ (for the web UI). Docker is optional.

```bash
git clone <your-local-path-or-remote> Heimdall && cd Heimdall
uv sync                       # Python deps (creates .venv)
cd web && npm ci && npm run build && cd ..   # builds web/dist, served by the API
```

Offline basemap (one-off download, several GB; needs internet once):

```bash
bash scripts/fetch_basemap.sh   # Protomaps extract (SE Asia + Middle East, z0-10), fonts, sprites -> data/basemap/
WORLD=1 bash scripts/fetch_basemap.sh   # also a whole-world overview (z0-6, ~45 MB) for the globe, Europe, Americas
```

The API serves these files at `/basemap/...` (Range supported); `/api/basemap/status` reports whether they are present. `SHA256SUMS` lists both files.

Optional API keys (Phase 2+). Each keyed source switches itself off and shows "needs key" in Data health until its key is set:

```bash
cp env.example .env           # then fill in only the keys you have; never commit .env
```

Keys can also be entered in the UI (**Data sources**), which stores them locally in `data/secrets/keys.json`.

| Source | Key(s) | Free sign-up | Without a key |
|---|---|---|---|
| AISStream (live ships) | `AISSTREAM_KEY` | aisstream.io | shows **needs key**, makes no requests |
| OpenSky (global aircraft) | `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET` | opensky-network.org → Account → API client | needs key; AOI aircraft still come from adsb.lol/adsb.fi |
| FAA NOTAMs | `FAA_NMS_KEY`, `FAA_NMS_SECRET` | FAA NMS API (approval) | needs key |
| NASA FIRMS (fires) | `FIRMS_MAP_KEY` | firms.modaps.eosdis.nasa.gov | needs key |
| NASA Earthdata (CYGNSS, SMAP from P5) | `EARTHDATA_TOKEN` | urs.earthdata.nasa.gov | needs key |
| NGA MSI nav warnings | none | — | runs (portal was under maintenance at last check → shows **down**) |

Global Fishing Watch, Cloudflare Radar and China MSA are being removed in task 027 (ADR-008); their keys are no longer needed.

Phase 1 needs **no keys**: adsb.lol and adsb.fi are free and keyless. airplanes.live now grants access by approval only, so it is off (`enabled: false` in `config/sources.yaml`) until you have it.

## 2. Run

### HF baseline (one-off, optional)
Both HF detectors compare each hour with the same time on the previous 14 days: the path detector (where signals
reflect) and the receiver detector (060: how well receivers in each area hear, against their neighbours). On a new
install, fill that history from the WSPR archive (stop `heimdall.run` first; DuckDB allows one writer):
```bash
uv run python scripts/hf_backfill.py --start 2026-09-19 --end 2026-10-02 --store data/heimdall.duckdb
```
```bash
uv run python scripts/hf_backfill.py --rx --start 2026-09-19 --end 2026-10-02 --store data/heimdall.duckdb
```
A saved download can be inserted again with `--from-file data/replay/hf/wspr_rx.jsonl --store data/heimdall.duckdb`.

### Demo (synthetic data, no network)
The quickest way to see the UI. It uses in-memory synthetic alerts: jamming, spoofing and a tanker orbit.
```bash
uv run python -m heimdall.api.demo
```
Open **http://127.0.0.1:8000**.

### Real-data replay (Strait of Hormuz, 17 June 2025)
Replays one real day of adsb.lol data through the actual detectors. Fetch it once (about 3.4 GB download,
filtered locally and then deleted; ODbL), then serve it on its own port so it can run next to the live app:
```bash
./scripts/fetch_adsblol_day.sh 2025-06-17
uv run python -m heimdall.api.demo --replay data/replay/hormuz/2025-06-17.parquet --port 8020
```
Open **http://127.0.0.1:8020**. The map opens on Hormuz with a REPLAY banner. Times are shifted onto
today's clock so traffic moves; alert IDs keep the real date (`HMD-20250617-...`).

### Demo: Operation Midnight Hammer (real data, 16-22 June 2025)
```bash
uv run python -m heimdall.api.demo --replay data/replay/midnight_hammer/midnight_hammer.parquet --port 8030
```
A **Demo** window steps through the public pre-strike indicators and shows, on the real military ADS-B traffic of that
week, what was visible and which alerts Heimdall raised. How to rebuild the data: [docs/demo-midnight-hammer.md](docs/demo-midnight-hammer.md).

### Live monitoring
One process runs ingest, detection and the API (see ADR-002; DuckDB is single-writer):
```bash
uv run python -m heimdall.run
```
- UI and API: **http://127.0.0.1:8000** (localhost only)
- Prometheus metrics: http://127.0.0.1:9108/metrics
- Data: `data/lake/` (Parquet, the source of truth), `data/heimdall.duckdb` (hot store), and `data/analyst.duckdb` (your triage and notes)
- Stop with `Ctrl-C`. If it exits with code 2, another process holds the DuckDB lock; stop it first.

### Web UI development (hot reload)
```bash
uv run python -m heimdall.api.demo      # or heimdall.run
cd web && npm run dev                    # http://localhost:5173, proxies /api to :8000
```

### Docker
```bash
docker compose -f deploy/docker-compose.yml up                  # live worker on 127.0.0.1:8000
docker compose -f deploy/docker-compose.yml --profile demo up   # demo on 127.0.0.1:8001
```

### Backtest and calibration
```bash
uv run python -m heimdall.backtest --aoi baltic --date YYYY-MM-DD --traces data/raw/<dir> --gpsjam data/raw/<file>.csv
```
This writes `reports/backtest-<aoi>-<date>.md` with precision, recall and F1 against GPSJam, and a
threshold sweep. The archives are downloaded manually into `data/raw/`; Heimdall never fetches them on its own.

## 3. Using the console

The layout follows C4I practice: one persistent common operational picture with linked views.
The design rationale is in [docs/ui-design.md](docs/ui-design.md).

| Area | What it shows |
|---|---|
| **Top bar** | Left: clock (Singapore time by default; set the zone in Settings), live dot, source health, space weather. Centre: **search** (areas, alerts, flights, ships or `lat, lon`, `/`) Right: open Critical / Warning counts, **Alerts** (`q`, badge = unacknowledged), **Timeline** (`t`), **Shift report** (`h`). |
| **Left rail** | Widens on hover: **Area of Ops** (`o`), **Data sources** (`u`), **Settings** (`,`, theme, time zone, map overlays), **Help** (`?`), **About** (`i`). |
| **Map** (centre) | Jamming hexes (green <2 %, amber 2–10 %, red >10 %), alert markers, satellites with ground tracks, emitter ellipses, and grey outlines where coverage is thin. Bottom left: **map type** picker, **Filters** and the **Legend** chip. Bottom right: measure / range ring, recenter on South-East Asia (`0`), compass, **3D / 2D** (`g`), zoom. Zooming out past the region turns the map into an offline 3D globe; zooming in flattens it. |
| **Inspector** (right) | **Now** (what and where), **Why** (rules fired, values against thresholds and baselines, counterfactual, Admiralty grade), **Trend** (metric against baseline), **Log** (triage journal) |
| **Timeline** (bottom, `t`) | 24 h of alerts by kind. Drag to filter, `Space` to replay. |

**Triage workflow** (keyboard first, press `?` for everything):

| Key | Action |
|---|---|
| `j` / `k`, `Enter` | Move through alerts, open one |
| `a` | Acknowledge ("I've seen it") |
| `r` | Under review |
| `c` | Confirm |
| `d` | Dismiss (reason required) |
| `s` | Shelve until a time (it returns to acknowledged automatically) |
| `n` | Add a note |
| `f` | Fly to selection |
| `/` | Search |

Every status change is written to the alert's **Log**. The pipeline never overwrites your decision.
A dismissed alert stays quiet for the rest of that episode. It re-alerts only if the activity escalates or returns after a clear gap.

**How to read an alert**
- **Admiralty grade**, e.g. `B2`: source reliability (A–F) × credibility (1–6). Credibility rises when independent sources agree.
  "Uncorroborated" means *no other source saw it*, not that it is false.
- **Confidence** is reduced where few aircraft, or only one aggregator, cover the cell. A quiet cell with poor coverage is **not** a safe cell.
- **Counterfactual** tells you what would clear the alert, e.g. "clears if ratio < 0.06".
- **Actions** come only from the allow-listed [config/playbook.yaml](config/playbook.yaml). Approving one records it and runs nothing external.

**Windows and live traffic.** The map is the whole screen; pages open as floating windows. Aircraft and ships
appear live for the area you are looking at; click one for its card. **Filters** only show or hide data
(aircraft, ships, GNSS, C-band, L-band, heat spots, satellites, live and daily layers, quick filters such as
military only, altitude band, ship types); they never change what is collected. **Data sources** is the only
place that switches collection on or off. Warnings and NOTAMs appear as hatched areas. Spectrum watch and the
area status board open from **Area of Ops**.

**Alert workflow.** The Alerts window has four tabs: **Needs action** (new; press Acknowledge to take ownership),
**In progress** (run the checks shown in the alert, then **Confirm real**, **False alarm** with a reason, or **Shelve**
for 2/8/24 h), **Confirmed** (real, being tracked; **Close: ended** when the activity stops) and **Closed** (history,
Reopen). Cards show whether the activity is still ongoing.

**Demo** (rail button or `p`). Steps through a recorded case on the live server, e.g. Operation Midnight Hammer:
the map shows the recorded military aircraft for each step and hides live data until you press Exit demo. The
demo is a precomputed file (`data/replay/midnight_hammer/midnight_hammer.demo.json`, built by
`scripts/midnight_hammer.py`), so live monitoring is not affected.

**Shift report** (`h`) includes rule-based Trends and a **Daily SITREP (24 h)** download: theatres above normal, the alerts that matter, military air activity and natural factors, built only from Heimdall's own data. For a written summary, install [Ollama](https://ollama.com) and
`ollama pull qwen2.5:7b-instruct`: the report then offers *Summarise with … (local)*. The model runs on this machine
(non-local URLs are refused), sees only Heimdall's alert facts and must cite alert refs.

**Military air activity** (rail button or `m`). Every aircraft the ADS-B networks flag as military, worldwide, refreshed
each minute: contacts, roles (tankers, bombers, surveillance, command posts, transport, fighters, government/VIP),
types, operator countries, high-interest aircraft and per-theatre counts; click an aircraft or theatre to fly there.
Identities come from the community plane-alert-db list (ODbL). Build-up indicators raise alerts per theatre: aircraft
**flowing in** (e.g. 6+ tankers arriving from outside within 48 h, as before the June 2025 strike on Iran), **more
than usual** of a role (against that theatre's own 14-day baseline), **high-interest aircraft** airborne (bombers,
E-4B/E-6B, heads of state) and **possible build-up** when three or more independent indicators coincide (tankers,
transport, bombers, surveillance, notices, GPS jamming). Theatres are presets in `config/config.yaml`
(`detectors.military.theatres`) plus every area you draw. These are indicator checklists: they never predict a
target or a strike, and aircraft flying with transponders off are not seen.

**Investigate a military aircraft**: click it, then **Investigate** (also from the list or the Military panel). You
see the type and what it is built for (range, crew, refuelling, capabilities, with a source link), operator and
country, where it took off (or where it was first seen when the take-off was not covered), what it may be doing now
(for example "likely air-refuelling track" or "transit towards the Europe staging theatre") with the measured reasons,
and its 72-hour track coloured by altitude. These are estimates from public ADS-B: intent is never known.

**Theatre activity** (top of the Military panel). Instead of a single threat score, each theatre lists what is
above its own normal: strategic military aircraft, military indicator alerts, GPS interference, radio band anomalies,
conflict events in the news and new notices. The level (Quiet, Watch, Elevated, High) is just how many are above
normal, and every component shows its numbers. It describes activity; it does not predict anything.

**Regions.** Type a region in the search box (Global, Gulf and Hormuz, Taiwan Strait, Ukraine and Black Sea, Baltic,
High North, …) to jump there. HF coverage now spans Eurasia east of 20°E and Australia.

**Context layers and news.** Filters → Context: **military bases** worldwide (OpenStreetMap) and **conflict events**
(fighting, strikes, shows of force coded from world news by GDELT every 15 minutes; only events reported by two or
more sources are drawn, and the hover says they can be wrong or old). **News** (rail or `n`): BBC World and Al
Jazeera headlines for context. None of these raise alerts or feed the AI brief.

**Detector tuning** (rail button or `k`). How often each detector was right, from your own verdicts (Confirm real /
False alarm with a reason), with an honest range, the false-alarm reasons, and threshold changes those verdicts
support: each keeps every confirmed alert and shows the `config/config.yaml` line to change. Nothing is applied
automatically; proposals appear after 5 false alarms and 3 confirmed alerts for a detector. Terminal version:
`uv run python -m heimdall.tuning --days 30` (live server stopped, or `--store` a copy).

**Air force watch** (rail button or `a`). Pick an air force (Singapore RSAF, Malaysia RMAF, Indonesia TNI-AU,
Philippines PAF, Thailand RTAF, Taiwan RoCAF, Japan JASDF, South Korea ROKAF, Australia RAAF, New Zealand RNZAF;
edit `config/air_forces.yaml` to add more) to see where its visible aircraft are: airborne, at home bases, at known overseas training detachments (for the RSAF,
for example RAAF Pearce), elsewhere at home or abroad, with movements between countries, flights per day and notes
such as "tanker abroad: possible deployment, exercise or transport mission". Click an aircraft to investigate it.
Fighters usually do not broadcast, so this shows support, transport and training fleets.

**Military roles on the map and the aircraft list** (rail button or `y`). Military planes are coloured by role. Pick
roles (tanker, surveillance, transport, helicopter, …) in Filters → Quick filters, in the Military panel or in the
list to show only those. The list has All / In view / Selected tabs, sortable columns, filters for callsign, type,
hex and operator, "Hide others" for the aircraft you ticked, and CSV / KML export.

New here? The console offers a guided tour on first visit (relaunch it from the `?` icon), and [docs/walkthrough.md](docs/walkthrough.md) walks the same monitor, triage, verify, decide, log workflow in prose.

The full API and UI reference is in [docs/ui.md](docs/ui.md).

## 4. Configure

| File | Purpose |
|---|---|
| [config/config.yaml](config/config.yaml) | AOIs (add a region = add a line), detector thresholds (each cites its provenance), base map, privacy switches |
| [config/sources.yaml](config/sources.yaml) | Data source registry: reliability grade, rate limit, auth env var, verification status |
| [config/playbook.yaml](config/playbook.yaml) | The only actions the UI can offer |
| `.env` | API keys (from `env.example`) |

**Base map:** `ui.basemap: openfreemap` (default) or `carto`. Both are third-party tiles, so those
servers see which map areas you view. Use `none` for a fully private blank map.

## 5. Privacy and safety

- The API binds to **127.0.0.1**; host and origin checks reject cross-site requests.
- Outbound traffic is limited to **GET requests to the listed data sources** and the base-map tiles.
- `privacy.allow_llm_egress: false`: no evidence is sent to any LLM until you switch it on (Phase 5).
- Untrusted text such as news and warnings is treated as data and rendered as plain text.
- A pre-commit hook scans for secrets. Install it with `ln -sf ../../scripts/pre-commit .git/hooks/pre-commit`.

## 6. Develop

```bash
uv run pytest -q                                          # tests (offline, recorded fixtures)
uv run ruff check . && uv run ruff format --check .       # lint
uv run mypy heimdall                                      # types
cd web && npm run build                                   # UI type-check + bundle
```

**Architecture:** ports and adapters (Bus, Store, Lake, Connector). Detectors are pure Python and
return `Evidence`, and there is no alert without evidence. All data is keyed by H3 cell and UTC. See
[docs/architecture.md](docs/architecture.md), [docs/detectors.md](docs/detectors.md) and
[docs/sources.md](docs/sources.md).

**Scaling:** swap adapters rather than rewriting. Use Redis or NATS for the bus, ClickHouse for the store,
S3/Iceberg for the lake, and detector workers sharded by AOI.

### Agent engineering team
Heimdall is built by a team of Claude Code agents (`.claude/agents/`). Each task gets a spec in
`docs/tasks/`, parallel builders work in git worktrees, then QA, then review, then the lead merges and the scribe updates the docs.
Run one agent headlessly with hard USD and turn caps:
```bash
scripts/agent.sh detection-scientist docs/tasks/006-backtest-calibration.md
```
The guardrails are a sandbox, a network allowlist, tool budgets, loop detection and no pushing or sharing without
your permission. See [docs/guardrails.md](docs/guardrails.md).

## 7. Roadmap

| Phase | Scope |
|---|---|
| P1–P3 (done) | ADS-B and AIS ingest, jamming/spoofing/orbit/AIS detectors, notices, fusion with Admiralty grading, FIRMS, weather, space weather, Sentinel-1 footprints and passes, global traffic, C4I console |
| P4 | Spectrum core: remove non-RF sources, band on every alert, jammer localisation, AIS circle spoofing, ship-to-ship transfers, interference notices, band ladder UI, GPSJam-style daily map |
| P5 (done) | Space RF: CYGNSS GNSS interference from orbit, SMAP, Sentinel-1 interference annotations |
| Military (039) | Worldwide military aircraft (readsb `/v2/mil`), plane-alert-db identities, deployment / surge / high-interest / build-up indicators per theatre |
| P6 (done) | HF: WSPR and PSK Reporter path-midpoint and receiver-cell detectors with flare/storm explain-away (KiwiSDR directory blocked) |
| P7 (done except cited Claude briefs) | Cross-band fusion, GDELT, daily SITREP, feedback-driven tuning; briefs by a local model |

Details: [docs/roadmap-rf.md](docs/roadmap-rf.md).

## Known limitations
- ADS-B and AIS coverage over open SE Asian seas is sparse (for example, Myanmar sees about 3–10 aircraft within 250 NM). Confidence is lowered to match, and sparse-region defaults are being calibrated in task 006.
- Thresholds are priors until the Baltic backtest (task 006) calibrates them.
- Every sensor is a volunteer or satellite receiver: where there are no receivers there is no data, and that is
  never read as "no interference". The HF detectors are experimental, need 5+ days of history (backfill: `scripts/hf_backfill.py`, see below) and never names an emitter type.
- Internet-outage, night-light, Sentinel-2 and Global Fishing Watch sources are being removed in task 027 (ADR-008).
- Military tracking only sees aircraft that broadcast ADS-B and are flagged military; strike packages often fly
  dark and use decoys. Theatre thresholds are priors and the surge baseline needs 3 days of data.
- Data is for situational awareness from open sources only; verify before acting.

### Drawing areas of operation

The live app starts with **no areas**: open **Area of Ops** (top of the left rail) to draw circles or polygons (up to 250 nm radius; a polygon must fit in a 250 nm circle), name them and turn monitoring on or off. They persist in `data/state/aois.json`. The `aois:` list in `config/config.yaml` is kept as presets for the demo, replay, backtests and tests (`use_config_aois: false` keeps it out of the live runtime). API: `GET/POST /api/aois/user`, `PATCH/DELETE /api/aois/user/{id}` (same-origin only). Details: `docs/tasks/015-draw-ao.md`.
