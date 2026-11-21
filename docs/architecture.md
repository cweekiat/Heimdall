# Architecture
```
Connectors (ingest/*)  ──► Bus 'obs.*' / 'events' ──► Writer ──► Lake (Parquet, truth) + Store (DuckDB, hot)
                                                          │
Scheduler (every step_min) ──► Detectors (detect/*, pure) ─┴► Evidence[bands, RfEffect] ──► Fusion (fuse/*) ──► Alerts[bands]
                                                                         │  Admiralty, corroboration,
                                                                         │  persistence, severity, playbook
API (FastAPI, api/*) ◄── Store ─────────────────────────────────────────┘
  REST + SSE ──► Web UI (web/): map · alert queue · evidence card · actions · trends · SITREP · data health
Analyst feedback ──► Store.feedback ──► tuning.py (precision, what-if) ──► proposed threshold changes (human-approved)
```
Bands: every source is a receiver on a band (HF, VHF marine, L 1090, L1 GNSS, L 1.4 GHz, C SAR). Detectors
emit `RfEffect`s with receiver coverage; fusion corroborates across bands. Two tempos: **live** (ADS-B, AIS,
PSK/WSPR; seconds to minutes) and **daily sweep** (GNSS day map, CYGNSS, SMAP, Sentinel-1; hours to a day), shown as
separate layer groups with their data date.
Process (MVP): a single `heimdall.run` hosts connectors + writer + scheduler + fusion **and** the API
(same asyncio loop, one shared Store), because DuckDB locks the file per process.
Analyst state (feedback, actions, status overrides) lives in `data/analyst.duckdb`. A hosted server store lifts this constraint.
Alert workflow: new → acknowledged → confirmed → resolved, or dismissed / shelved / expired; transitions in
`heimdall/api/triage.py`, applied as status overrides by `CompositeStore`.
Shift brief: `heimdall/brief/facts.py` builds a fact sheet from stored alerts (own fields only) and trend lines;
`heimdall/brief/local_llm.py` may narrate it through a local Ollama model (loopback URLs only, refs checked).
Demo packs: precomputed `*.demo.json` files served read-only by the live API (`heimdall/api/demo_packs.py`);
no replay store or pipeline runs on the live server.
HF (045, 060): `pipeline.HfCache` keeps each baseline-day 10-min bin once and runs one `prepare` per window end,
shared by all areas; one instance per detector module (`hf` path midpoints, `hf_rx` receiver cells).
Tuning: `heimdall/tuning.py` reads alerts, verdicts and evidence from the Store and reports precision and
threshold what-ifs (`/api/tuning`, `python -m heimdall.tuning`); it never writes config.
Air force watch (056/058): `heimdall/airforce.py` (pure) over lake positions read by `api/airforce.LakeFleetReader`,
one hour-partition cache shared by every configured fleet.
 for scale-out mapping.
