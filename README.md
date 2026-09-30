# Heimdall

**Multi-domain OSINT monitor for electronic warfare and military activity, Southeast Asia first.**

Heimdall watches free, open data (ADS-B today; AIS, navigational warnings, NOTAMs, satellite and
news sources in later phases) and raises **explainable, cross-checked alerts** for:

- **GNSS jamming**: cells where aircraft lose GPS integrity (NIC/NACp), compared with that cell's own baseline
- **GNSS spoofing**: five independent signals (teleports, altitude divergence, MLAT gap, convergence, impossible tracks)
- **Military loitering**: tanker, AEW and ISR orbits and racetracks, flagged when new or near jamming

It covers these areas of interest: Myanmar, the South China Sea, the Taiwan/Luzon Strait and the
Malacca/Singapore Strait. The **Baltic** is a calibration control. The results appear in a
C4I-style analyst console with a live operational map, an alert board, evidence, and a triage
workflow.

> Status: **Phase 1 (airspace + UI) nearing completion.** See [CLAUDE.md](CLAUDE.md) for the phase table and
> [docs/CHANGELOG.md](docs/CHANGELOG.md) for history.

---

## 1. Install

**Requirements:** macOS or Linux, Python 3.12, [uv](https://docs.astral.sh/uv/), and Node.js 20+ (for the web UI). Docker is optional.

```bash
git clone <your-local-path-or-remote> Heimdall && cd Heimdall
uv sync                       # Python deps (creates .venv)
cd web && npm ci && npm run build && cd ..   # builds web/dist, served by the API
```

Optional API keys are needed from Phase 2 onward:

```bash
cp env.example .env           # then fill in only the keys you have; never commit .env
```

Phase 1 needs **no keys**: adsb.lol, adsb.fi and airplanes.live are free and keyless.

## 2. Run

### Demo (synthetic data, no network)
The quickest way to see the UI. It uses in-memory synthetic alerts: jamming, spoofing and a tanker orbit.
```bash
uv run python -m heimdall.api.demo
```
Open **http://127.0.0.1:8000**.

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

### Backtest and calibration (task 006, in progress)
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
| P1 | ADS-B ingest, jamming/spoofing/orbit detectors, Baltic calibration, C4I UI |
| P2 | AIS (AISStream, Global Fishing Watch), NGA / China MSA warnings, FAA NOTAMs, AIS spoofing and dark gaps, cross-domain fusion |
| P3 | FIRMS fires, weather, internet outages (IODA, Cloudflare), night lights, Sentinel-1/2 with pass prediction |
| P4 | GDELT, Taiwan MND, Japan JSO, ReliefWeb; surges, change points, hot spots, lead-lag |
| P5 | Cited Claude briefs, daily SITREP, feedback-driven tuning |

## Known limitations
- ADS-B and AIS coverage over open SE Asian seas is sparse (for example, Myanmar sees about 3–10 aircraft within 250 NM). Confidence is lowered to match, and sparse-region defaults are being calibrated in task 006.
- Thresholds are priors until the Baltic backtest (task 006) calibrates them.
- Data is for situational awareness from open sources only; verify before acting.
