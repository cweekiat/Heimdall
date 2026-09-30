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
| L-band from orbit | SMAP 1.4 GHz interference flags (needs EARTHDATA_TOKEN); CYGNSS L1 noise floor | SMAP connector live; CYGNSS planned |
| C-band 5.4 GHz | Sentinel-1 radar interference annotations (Planetary Computer) | connector live; detector planned |
| HF 3–30 MHz | WSPR and PSK Reporter spots at path midpoints; SatNOGS satellite passes | connectors live; detectors planned (experimental) |

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
```

The API serves these files at `/basemap/...` (Range supported); `/api/basemap/status` reports whether they are present.

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
| **Status bar** (top) | UTC clock, data freshness per source, alert rate against the 24 h average, critical banner, **Shift** summary |
| **AOI board** (left) | One tile per area with its worst alert, counts and trend. Click a tile or press `1`–`5` to fly there. |
| **Map** (centre) | Jamming hexes (green <2 %, amber 2–10 %, red >10 %), alert markers, orbit tracks, emitter ellipses, and grey outlines where coverage is thin. **Layers** (`l`) lets you change the base map and overlays: borders, military areas, EEZ, sea marks, graticule. |
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

**Windows and live traffic.** The map is the whole screen; the toolbar on the left opens floating windows
(Alerts `q`, Areas `b`, Layers & filters `l`, Data sources `u`, Actions `x`, Timeline `t`). Aircraft and ships
appear live for the area you are looking at; click one for its card, or use Layers & filters to show military
only, an altitude band, ship types, or just things involved in alerts. **Data sources** shows what is
collecting and lets you switch a source on or off. Warnings and NOTAMs appear as hatched areas.

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
| P5 | Space RF: CYGNSS, SMAP, Sentinel-1 interference annotations |
| P6 | HF: WSPR, PSK Reporter, KiwiSDR directory |
| P7 | Cross-band fusion, GDELT, cited Claude briefs, daily SITREP, feedback-driven tuning |

Details: [docs/roadmap-rf.md](docs/roadmap-rf.md).

## Known limitations
- ADS-B and AIS coverage over open SE Asian seas is sparse (for example, Myanmar sees about 3–10 aircraft within 250 NM). Confidence is lowered to match, and sparse-region defaults are being calibrated in task 006.
- Thresholds are priors until the Baltic backtest (task 006) calibrates them.
- Every sensor is a volunteer or satellite receiver: where there are no receivers there is no data, and that is
  never read as "no interference". The planned HF detectors will be experimental and never name an emitter type.
- Internet-outage, night-light, Sentinel-2 and Global Fishing Watch sources are being removed in task 027 (ADR-008).
- Data is for situational awareness from open sources only; verify before acting.

### Drawing areas of operation

In the UI, **Draw areas** (pen icon) lets you draw circles or polygons, name them and turn monitoring on or off. They persist in `data/state/aois.json`; config-file areas stay read-only. API: `GET/POST /api/aois/user`, `PATCH/DELETE /api/aois/user/{id}` (same-origin only). Details: `docs/tasks/015-draw-ao.md`.
