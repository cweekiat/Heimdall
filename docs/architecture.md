# Architecture
```
Connectors (ingest/*)  ──► Bus 'obs.*' / 'events' ──► Writer ──► Lake (Parquet, truth) + Store (DuckDB, hot)
                                                          │
Scheduler (every step_min) ──► Detectors (detect/*, pure) ─┴► Evidence ──► Fusion (fuse/*) ──► Alerts
                                                                         │  Admiralty, corroboration,
                                                                         │  persistence, severity, playbook
API (FastAPI, api/*) ◄── Store ─────────────────────────────────────────┘
  REST + SSE ──► Web UI (web/): map · alert queue · evidence card · actions · trends · SITREP · data health
Analyst feedback ──► Store.feedback ──► precision stats ──► proposed threshold changes (human-approved)
```
Process (MVP): a single `heimdall.run` hosts connectors + writer + scheduler + fusion **and** the API
(same asyncio loop, one shared Store), because DuckDB locks the file per process (ADR-002).
Analyst state (feedback, actions, status overrides) lives in `data/analyst.duckdb`. A hosted server store lifts this constraint.
See ADR-001 for scale-out mapping.
