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
Processes (MVP): `heimdall.run` (connectors + writer + scheduler + fusion) and `uvicorn heimdall.api.app:app`.
Only `heimdall.run` writes DuckDB; the API opens it read-only (DuckDB single-writer rule) — hosted Store removes this constraint.
See ADR-001 for scale-out mapping.
