# Data sources (maintained by source-scout)
Registry: `config/sources.yaml`. Status legend: verified · needs-key · broken · unverified.

---

## adsb_lol

**Status:** verified (Baltic: OK, Myanmar: OK; military endpoint: 503)

**Base URL:** `https://api.adsb.lol`

**Endpoints:**
- `/v2/point/{lat}/{lon}/{radius_nm}` — Aircraft within radius (nautical miles)
- `/v2/mil/point/{lat}/{lon}/{radius_nm}` — Military aircraft (503 Service Unavailable)
- `/v2/closest/{lat}/{lon}` — Nearest aircraft
- `/v2/hex/{icao_hex}` — Single aircraft by hex code

**Authentication:** None (public)

**Rate Limit:** 1 request/second (RPS)

**Fields of Interest:** `hex`, `type`, `alt_baro`, `alt_geom`, `nic`, `nac_p`, `nac_v`, `sil`, `gva`, `sda`, `dbFlags`, `mlat`, `tisb`, `track`, `gs`, `squawk`

**Latency:** 5 seconds (real-time ADS-B aggregation)

**License:** ODbL 1.0 (data), OSM-compatible

**Fixture Path:** `tests/fixtures/adsb_lol/point_56_20.5_250_baltic.json`, `point_20.5_96.5_250_myanmar.json` (50 aircraft samples)

**Gotchas:** 
- Military endpoint returns 503 
- Response format mirrors ADSBexchange v2 API
- Max radius typically 250 nm

**Source:** [adsb.lol API docs](https://www.adsb.lol/docs/open-data/api/), [GitHub](https://github.com/adsblol/api)

---

## adsb_fi

**Status:** verified (Baltic: OK, Myanmar: OK)

**Base URL:** `https://opendata.adsb.fi/api`

**Endpoints:**
- `/v3/lat/{lat}/lon/{lon}/dist/{dist_nm}` — Proximity query (preferred v3)
- `/v2/lat/{lat}/lon/{lon}/dist/{dist_nm}` — Deprecated v2 (backward compat)
- `/v2/hex/{icao_hex}` — Single aircraft by hex
- `/v2/icao/{icao_codes}` — Multiple aircraft by ICAO
- `/v2/callsign/{callsign}` — Search by callsign
- `/v2/registration/{reg}` — Search by registration
- `/v2/squawk/{code}` — Search by squawk code
- `/v2/mil` — Military aircraft list

**Authentication:** None (public); feeder IP auto-whitelisted for enhanced limits

**Rate Limit:** 1 request/second (public); 1 request/30 seconds (feeder endpoints)

**Fields of Interest:** `hex`, `type`, `alt_baro`, `alt_geom`, `nic`, `nac_p`, `nac_v`, `sil`, `gva`, `sda`, `track`, `gs`, `squawk`

**Latency:** 5 seconds

**License:** Personal, non-commercial use only; must cite adsb.fi

**Fixture Path:** `tests/fixtures/adsb_fi/point_56_20.5_250_baltic.json`, `point_20.5_96.5_250_myanmar.json` (50 aircraft samples)

**Gotchas:** 
- v2 endpoints deprecated; migrate to v3 for new integrations
- Non-commercial restriction enforced
- ADSBexchange v2 compatible format
- Feeder contributors get enhanced rate limits

**Source:** [adsb.fi open data README](https://github.com/adsbfi/opendata/blob/main/README.md), [adsb.fi home](https://adsb.fi/)

---

## airplanes_live

**Status:** broken (requires contact/approval)

**Base URL:** `https://api.airplanes.live`

**Endpoints:**
- `/api/v2/point/{lat}/{lon}/{radius_nm}` — Aircraft within radius
- `/api/v2/hex/{icao_hex}` — Single aircraft
- `/api/v2/closest/{lat}/{lon}` — Nearest aircraft

**Authentication:** Contact required; responds with: "Please contact us at contact@airplanes.live. Your email MUST include any links, a description of the project, and any information you deem appropriate."

**Rate Limit:** 1 request/second (stated)

**Fields of Interest:** ADSBexchange v2 compatible: `hex`, `type`, `alt_baro`, `alt_geom`, `nic`, `nac_p`, `track`, `gs`, `squawk`

**Latency:** 5 seconds

**License:** Requires prior approval; terms unclear

**Fixture Path:** None (access denied)

**Gotchas:** 
- No self-serve access; contact required for all use
- Responses indicate active vetting of requesters
- May have undisclosed ToS/commercial restrictions

**Source:** [Airplanes.live API](https://airplanes.live/api/), [GitHub](https://github.com/airplanes-live/api), [API docs](https://airplanes.live/api-docs/) (403 Forbidden)

---

## adsb_lol_history

**Status:** unverified (archived repository)

**Base URL:** `https://github.com/adsblol/` (GitHub releases)

**Repository Structure:**
- Separate repos per year: `globe_history_2023`, `globe_history_2024`, `globe_history_2025`, `globe_history_2026`
- Each release = 1 day of flight data (gzip-compressed JSON)
- Files: `/traces/` (per-aircraft), `/heatmap/` (replay data)

**File Format:** 
- Gzip-compressed JSON (readsb output)
- Per-aircraft JSON files for date
- Format documented in [wiedehopf/readsb README-json.md](https://github.com/wiedehopf/readsb/blob/dev/README-json.md#trace-jsons)

**Release Naming:** Daily releases, e.g., `2025-01-15`; RELEASES.md index provided

**Rate Limit:** N/A (GitHub releases, no API rate limit for reads)

**License:** ODbL 1.0 + CC0 1.0 (dual-licensed)

**Fixture Path:** None (archive-only; files typically >50 MB per day)

**Gotchas:** 
- `globe_history_2025` archived Jan 5, 2026; read-only
- Readsb format; JSON schema in wiedehopf repo
- Individual day files can exceed 50 MB; plan accordingly
- No live API; static releases only

**Source:** [adsblol/globe_history_2025](https://github.com/adsblol/globe_history_2025), [Release list](https://github.com/adsblol/globe_history_2025/releases), [Historical docs](https://www.adsb.lol/docs/open-data/historical/)

---

## opensky

**Status:** needs-key (verified auth flow)

**Base URL:** `https://opensky-network.org/api`

**Endpoints:**
- `/states/all` — All aircraft (requires authentication or limited by rate limit)
- `/flights/aircraft` — Aircraft flight history
- `/tracks/all` — Aircraft tracks with detailed points

**Authentication:** OAuth 2.0 client credentials
- Register at opensky-network.org → create API client
- Exchange `client_id` + `client_secret` for Bearer token
- Pass token in `Authorization: Bearer <token>` header
- Environment variable: `OPENSKY_CLIENT_ID` (secret stored separately)

**Rate Limit:**
- Anonymous: 100 calls/day
- Authenticated: 4,000 calls/day
- Data feeder contributors: 8,000 calls/day
- Applies to: `/states/all`, `/flights/*`, `/tracks/all`

**Fields of Interest:** `icao24`, `callsign`, `velocity`, `heading`, `vertrate`, `geo_altitude`, `baro_altitude`, `category`, `sil`, `nac_p`

**Latency:** 10 seconds (API documentation)

**License:** Non-commercial; commercial use requires separate license

**Fixture Path:** N/A (keyed source; no live fixture)

**Gotchas:** 
- Rate limits strict; unauthenticated access highly restricted
- Token must be refreshed periodically (check `expires_in`)
- `/states/all` returns time-windowed snapshot (~10s latency)
- Coordinate system: WGS84

**Source:** [OpenSky REST API docs](https://openskynetwork.github.io/opensky-api/rest.html), [GitHub API](https://github.com/openskynetwork/opensky-api), [FAQ](https://opensky-network.org/about/faq)

---

## gpsjam

**Status:** unverified (data format unclear)

**Base URL:** `https://gpsjam.org`

**Data Access:**
- **Web interface:** Interactive map at https://gpsjam.org/
- **Daily data:** Reported as 1 CSV file per day (format undocumented)
- **Hexagonal aggregation:** Interference mapped to hexagonal cells
- **Date range:** Feb 14, 2022 to present; manual updates ~midnight UTC

**Data Format:**
- CSV (1 file/day per docs); exact columns not documented
- Aggregates 24-hour ADS-B position accuracy data from ADS-B Exchange
- Hex cells color-coded: green (0-2% errors), yellow (2-10%), red (>10%), gray (insufficient data)

**Authentication:** None (public web interface)

**Rate Limit:** N/A for web view; no API documented

**Fields of Interest:** Date, hex grid ID, accuracy percentiles, aircraft count

**Latency:** 24 hours (daily update)

**License:** Not explicitly stated; research-friendly use suggested

**Fixture Path:** None (daily download mechanism not confirmed)

**Gotchas:** 
- No documented public data download API or CSV URL pattern
- Data updates manual (not real-time)
- Measures aircraft interference at altitude; ground-level impacts differ
- Limited historical depth (Feb 2022 onwards)
- Some dates incomplete or missing

**Source:** [GPSJam GitHub](https://github.com/guofengji/gpsjam.org), [Bellingcat Toolkit docs](https://bellingcat.gitbook.io/toolkit/more/all-tools/gpsjam), [GPSJam.org](https://gpsjam.org/)

---

## faa_nms_notam

**Status:** needs-key (access restricted)

**Base URL:** `https://nms.aim.faa.gov`

**Endpoints:**
- No published OpenAPI/Swagger schema for unauthenticated users
- Requires FAA approval to access; request at notams@faa.gov

**Authentication:** 
- OAuth 2.0 client credentials (client_id + client_secret in header)
- Environment variable: `FAA_NMS_KEY`
- Access must be requested from FAA; not self-serve

**Rate Limit:** Not publicly documented; ~338,000 API calls/day reported on portal (aggregate)

**Fields of Interest:** NOTAM ID, effective date, geometry, hazard type, altitude, airspace class

**Latency:** ~300 seconds (5 minutes; notice distribution delay)

**License:** Restricted government data; non-commercial use presumed

**Fixture Path:** N/A (keyed, restricted access)

**Gotchas:** 
- No self-serve registration; contact notams@faa.gov required
- API documentation only for authorized users
- Endpoint details not public; reverse-engineer via SDK/client if needed
- Part of NMS modernization (initial rollout Sept 30, 2025)
- Commercial/operational use may require special terms

**Source:** [FAA NMS FAQs](https://www.faa.gov/about/initiatives/notam/faqs), [NMS Portal](https://nms.aim.faa.gov/), [SDK: FPAssistant](https://fpassistant.com/sdk-faa-nms-notams/)

---

## aisstream

**Status:** needs-key (verified auth flow)

**Base URL:** `wss://stream.aisstream.io/v0/stream` (WebSocket)

**Connection:**
- WebSocket (WSS only; no unencrypted WS)
- Server-side only (no browser connections)
- Persistent connection; async subscription model

**Authentication:**
- Pass API key in JSON subscription message field `APIKey` (not HTTP header)
- Create/rotate keys from Account panel
- Subscription message required within 3 seconds of connect
- Environment variable: `AISSTREAM_KEY`

**Rate Limit:**
- 3 subscribed connections per account
- 3 connections per IP address
- 1 subscription update per second max
- MMSI filter: 200 per subscription
- Uncompressed bandwidth throttling (Sept 2026+): messages exceeding per-user limits dropped

**Message Types:** 25 AIS message types (position, static data, safety info)

**Fields of Interest:** `MMSI`, `ShipName`, `IMO`, `CallSign`, `Latitude`, `Longitude`, `SOG`, `COG`, `Heading`, `VesselType`

**Latency:** 5 seconds (real-time streaming)

**License:** Commercial license available; no published free-tier ToS

**Fixture Path:** N/A (keyed WebSocket stream; no REST fixture)

**Gotchas:** 
- No REST API; WebSocket-only (requires persistent connection)
- Subscription required within 3 seconds; connection closed if omitted
- 3-connection account limit can be restrictive at scale
- No SLA or uptime guarantee stated
- Events not durably replayed; dropped on service restart
- Bandwidth throttling announced for Sept 2026

**Source:** [AISStream Documentation](https://aisstream.io/documentation), [API GitHub](https://github.com/aisstream/issues), [Bandwidth limits issue](https://github.com/aisstream/issues/issues/302)

---

## gfw (Global Fishing Watch)

**Status:** needs-key (verified auth flow)

**Base URL:** `https://gateway.api.globalfishingwatch.org/v3`

**Endpoints:**
- `/datasets` — Metadata on available fishing/vessel datasets
- `/searches` — Query vessel positions and activities
- `/events` — Fishing/port/loitering events
- RESTful query API; multiple data domains (fishing, vessels, events)

**Authentication:** Bearer token (REST API)
- Create up to 5 tokens per account
- Rate limits shared across all tokens for same account
- Pass in `Authorization: Bearer <token>` header
- Environment variable: `GFW_TOKEN`
- 401/403 errors: See official docs for troubleshooting

**Rate Limit:** 
- Daily and monthly request limits (exact values not disclosed in public docs)
- Shared across all tokens for account
- Check response headers for usage/remaining

**Fields of Interest:** Vessel positions, fishing effort, port visits, loitering behavior, vessel type, MMSI

**Latency:** 24 hours (daily aggregated data for fishing)

**License:** Commercial; free tier with usage limits; requires agreement

**Fixture Path:** N/A (keyed source; no live fixture)

**Gotchas:** 
- Rate limits per account (shared across tokens), not per token
- Multiple token limit enforced (max 5)
- Fishing data is 24hr-delayed aggregation (not real-time)
- License/commercial terms strict; review ToS carefully
- Dataset versioning; older data may be replaced/updated

**Source:** [Global Fishing Watch API Docs](https://globalfishingwatch.org/our-apis/documentation/), [License & Rate Limits](https://globalfishingwatch.org/our-apis/documentation/docs/license-rate-limits), [Python client](https://github.com/GlobalFishingWatch/gfw-api-python-client)

---

## nga_msi

**Status:** broken (Swagger endpoint returns 503)

**Base URL:** `https://msi.nga.mil/api`

**Intended Endpoints:**
- `/swagger-ui.html` — Interactive API docs (503 Service Unavailable)
- `/publications/download` — PDF/data downloads (functional)
- `/in-force-ntm` — In-force Notices to Mariners (presumed; not confirmed)

**Authentication:** None (public navigation warnings)

**Rate Limit:** Not documented; assume standard REST limits

**Fields of Interest:** Notice ID, effective date, area (NAVAREA), coordinates, hazard type, chart corrections

**Latency:** 1 hour (Notice publication/distribution)

**License:** U.S. Government public data (no copyright)

**Fixture Path:** None (API unreachable; Swagger returns 503)

**Gotchas:** 
- Swagger UI currently unavailable (503)
- Navigation warnings (HYDROPAC, NAVAREA XI) may be in PDF format only
- No JSON endpoint confirmed; may require scraping HTML notices
- Commercial use restrictions may apply to derived products
- U.S. domestic notices; international NAVAREA data may be sparse

**Source:** [NGA MSI Home](https://msi.nga.mil/home), [Swagger UI](https://msi.nga.mil/api/swagger-ui.html) (503), [Maritime Safety Products](https://www.nga.mil/resources/Maritime_Safety_Products_and_Services.html)

---

## china_msa

**Status:** unverified (limited content; HTML sample minimal)

**Base URL:** `https://www.msa.gov.cn`

**Endpoints:**
- `/msacncms_wap/pages/info_warn.do?channelId=9C219298-B27F-460E-995A-99401B3FF6AF` — Navigation warnings (航行警告)
- Regional MSA offices: Shanghai, Tianjin, Guangdong, Fujian; separate channels

**Authentication:** None (public)

**Content Format:** HTML pages (no JSON API documented)

**Navigation Warning Categories:**
- 航行警告 (Navigation warnings): Military exercises, construction, hazards
- Regional coverage: East China Sea, Yangtze River, South China Sea, coastal waters
- Issuing offices: Shanghai MSA, Tianjin MSA, Guangdong MSA, Fujian MSA

**Latency:** 3600 seconds (daily/weekly updates)

**License:** Chinese government public data

**Fixture Path:** `tests/fixtures/china_msa/sample_nav_warnings.html` (HTML sample, 207 bytes; minimal content)

**Gotchas:** 
- No JSON/API endpoint; HTML only (requires scraping)
- Page returned minimal content (207 bytes); may require session/authentication
- Regional warnings scattered across multiple MSA office URLs
- Content in Simplified Chinese; no English mirror
- Geopolitical sensitivity (South China Sea territorial claims); data may be restricted
- Update frequency unclear; no RSS/API for monitoring

**Source:** [China MSA Navigation Warnings](https://www.msa.gov.cn/), [Sansha/Hainan/Guangdong/Fujian regions](https://en.msa.gov.cn/), [MSA Wikipedia](https://en.wikipedia.org/wiki/China_Maritime_Safety_Administration)

---

## Summary Table

| Source | Status | Key Gotcha |
|--------|--------|-----------|
| adsb_lol | verified | Military endpoint 503 |
| adsb_fi | verified | Non-commercial only; v2 deprecated |
| airplanes_live | broken | Requires contact approval |
| adsb_lol_history | unverified | GitHub releases; >50 MB/day files |
| opensky | needs-key | 4,000 calls/day (auth); strict limits |
| gpsjam | unverified | No documented data download API |
| faa_nms_notam | needs-key | FAA approval required; no public API docs |
| aisstream | needs-key | WebSocket-only; 3-conn/acct limit |
| gfw | needs-key | Rate limits shared; commercial license |
| nga_msi | broken | Swagger 503; no JSON endpoint confirmed |
| china_msa | unverified | HTML-only; no API; geopolitical sensitivities |
