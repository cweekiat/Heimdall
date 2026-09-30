# Architecture
```
Connectors (ingest/*)  ──► Bus 'obs.*' / 'events' ──► Writer ──► Lake (Parquet, truth) + Store (DuckDB, hot)
                                                          │
Scheduler (every step_min) ──► Detectors (detect/*, pure) ─┴► Evidence[bands, RfEffect] ──► Fusion (fuse/*) ──► Alerts[bands]
                                                                         │  Admiralty, corroboration,
                                                                         │  persistence, severity, playbook
API (FastAPI, api/*) ◄── Store ─────────────────────────────────────────┘
  REST + SSE ──► Web UI (web/): map · alert queue · evidence card · actions · trends · SITREP · data health
Analyst feedback ──► Store.feedback ──► precision stats ──► proposed threshold changes (human-approved)
```
Bands (ADR-008): every source is a receiver on a band (HF, VHF marine, L 1090, L1 GNSS, L 1.4 GHz, C SAR). Detectors
emit `RfEffect`s with receiver coverage; fusion corroborates across bands. Two tempos: **live** (ADS-B, AIS, and later
PSK/WSPR; seconds to minutes) and **daily sweep** (GNSS day map, CYGNSS, SMAP, Sentinel-1; hours to a day), shown as
separate layer groups with their data date.
Process (MVP): a single `heimdall.run` hosts connectors + writer + scheduler + fusion **and** the API
(same asyncio loop, one shared Store), because DuckDB locks the file per process (ADR-002).
Analyst state (feedback, actions, status overrides) lives in `data/analyst.duckdb`. A hosted server store lifts this constraint.
See ADR-001 for scale-out mapping.
