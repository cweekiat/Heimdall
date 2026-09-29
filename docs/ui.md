# Analyst UI and API (task 004)

Run: `uv run python -m heimdall.api.demo` (synthetic data, port 8000) and `cd web && npm run dev`
(port 5173, proxies `/api`). Production: `npm run build`; FastAPI serves `web/dist` at `/`.
The API binds 127.0.0.1 by default (`api.host`).

## Views
1. **Map** - MapLibre (CARTO dark/light styles) + deck.gl. H3 r4 hexes coloured by degraded ratio
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
| POST | `/api/actions/{id}/approve` , `/reject` | only for `proposed`; approve is a recording stub |
| GET | `/api/stream` | SSE, event `alert` with the updated Alert JSON |

Auth: `create_app(auth_check=...)` middleware is a no-op while `api.auth == none`.
Store: `create_app(store)`; default tries `heimdall.adapters.duckdb_store.DuckDBStore` read-only,
falling back to `MemoryStore`.
