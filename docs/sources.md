# Data sources (maintained by source-scout)
Registry: `config/sources.yaml`. Status legend: verified · needs-key · broken · unverified.

---

## adsb_lol

**Status:** verified (general endpoints OK; military endpoint: 503)

**Base URL:** `https://api.adsb.lol`

**Endpoints:**
- `/v2/point/{lat}/{lon}/{radius_nm}` — Aircraft within radius (nautical miles) ✓
- `/v2/mil/point/{lat}/{lon}/{radius_nm}` — Military aircraft (503 Service Unavailable)
- `/v2/closest/{lat}/{lon}` — Nearest aircraft
- `/v2/hex/{icao_hex}` — Single aircraft by hex code

**Military Aircraft Fallback:**
- `/v2/point` + filter by `dbFlags` (bit mask for military designation)
- Slower than dedicated endpoint but functional
- Requires client-side filtering; check flag value for military indicators

**Authentication:** None (public)

**Rate Limit:** 1 request/second (RPS)

**Fields of Interest:** `hex`, `type`, `alt_baro`, `alt_geom`, `nic`, `nac_p`, `nac_v`, `sil`, `gva`, `sda`, `dbFlags`, `mlat`, `tisb`, `track`, `gs`, `squawk`

**Latency:** 5 seconds (real-time ADS-B aggregation)

**License:** ODbL 1.0 (data), OSM-compatible

**Fixture Path:** `tests/fixtures/adsb_lol/point_56_20.5_250_baltic.json`, `point_20.5_96.5_250_myanmar.json` (50 aircraft samples)

**Gotchas:** 
- `/v2/mil` endpoint consistently returns 503; use `/v2/point` with `dbFlags` filtering as workaround
- Response format mirrors ADSBexchange v2 API
- Max radius typically 250 nm
- Military designation via `dbFlags` not as comprehensive as dedicated `/v2/mil`

**Source:** [adsb.lol API docs](https://www.adsb.lol/docs/open-data/api/), [GitHub](https://github.com/adsblol/api), [dbFlags reference](https://github.com/adsblol/api#dbflags)

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

**Status:** needs-key (contact approval required; no self-serve registration)

**Base URL:** `https://api.airplanes.live`

**Endpoints:**
- `/v2/point/{lat}/{lon}/{radius_nm}` — Aircraft within radius
- `/v2/hex/{icao_hex}` — Single aircraft
- `/v2/closest/{lat}/{lon}` — Nearest aircraft

**Authentication:** 
- No API key mechanism; contact approval only
- Email contact@airplanes.live with: project description, intended use, links, organization info
- Response: "Please contact us at contact@airplanes.live. Your email MUST include any links, a description of the project, and any information you deem appropriate."

**Rate Limit:** 1 request/second (stated; subject to approval)

**Fields of Interest:** ADSBexchange v2 compatible: `hex`, `type`, `alt_baro`, `alt_geom`, `nic`, `nac_p`, `track`, `gs`, `squawk`

**Latency:** 5 seconds (estimated)

**License:** Requires prior approval; terms provided post-contact

**Fixture Path:** None (access requires approval)

**Gotchas:** 
- No self-serve access; mandatory contact/vetting process
- Approval turnaround time unclear (days? weeks?)
- Commercial use restrictions likely; clarify before committing
- API endpoint documentation behind approval wall (403 Forbidden)
- Alternative: use adsb_lol or adsb.fi for unrestricted data

**Source:** [Airplanes.live](https://airplanes.live/), [Contact form](https://airplanes.live/contact), [GitHub](https://github.com/airplanes-live/api)

---

## adsb_lol_history

**Status:** verified (2026 releases confirmed; split-archive format)

**Base URL:** `https://github.com/adsblol/` (GitHub releases)

**Repository Structure:**
- Separate repos per year: `globe_history_2023`, `globe_history_2024`, `globe_history_2025`, `globe_history_2026`
- Each release = 1 day of flight data (readsb output)
- Releases named `v{YYYY}.{MM}.{DD}-planes-readsb-{env}-0` (e.g., `v2026.09.28-planes-readsb-prod-0`)

**File Format & Delivery:**
- Split into multi-part tar archives (.tar.aa, .tar.ab, etc.) to fit GitHub asset size limits
- Latest (2026-09-28): 2 GB (tar.aa) + 1.9 GB (tar.ab)
- Each part must be downloaded and concatenated: `cat *.tar.a* | tar xz`
- Readsb JSON format; schema in [wiedehopf/readsb README-json.md](https://github.com/wiedehopf/readsb/blob/dev/README-json.md#trace-jsons)
- Per-aircraft traces + static data

**Release Variants:**
- `staging`: Pre-production data
- `prod`: Production (recommended)
- `mlatonly`: MLAT-only positions

**Range Requests:** Not supported (GitHub redirects to CDN; 302 on Range header)

**Rate Limit:** N/A (GitHub releases, no API rate limit for reads)

**License:** ODbL 1.0 + CC0 1.0 (dual-licensed)

**Fixture Path:** None (files >50 MB; document asset naming/sizes only)

**Gotchas:** 
- Multi-part downloads required; must concatenate before extraction
- Individual day files exceed 50 MB; bandwidth-heavy (~4 GB/day for prod+staging)
- Range requests not supported; full downloads only
- No live API; static releases only
- Readsb format requires decompression + JSON parsing

**Source:** [adsblol/globe_history_2026](https://github.com/adsblol/globe_history_2026), [Latest release](https://github.com/adsblol/globe_history_2026/releases/latest), [Readsb docs](https://github.com/wiedehopf/readsb/blob/dev/README-json.md)

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

**Status:** verified (daily CSV pattern confirmed; data accessible)

**Base URL:** `https://gpsjam.org`

**Data Access:**
- **Web interface:** Interactive map at https://gpsjam.org/
- **Daily data URL pattern:** `https://gpsjam.org/data/{YYYY}-{MM}-{DD}-h3_4.csv` (gzip-compressed)
- **Hexagonal aggregation:** Interference mapped to H3 hexagonal cells (level 4)
- **Date range:** Feb 14, 2022 to present; updates ~24 hours after aggregation window closes (UTC)

**Data Format:**
- CSV (gzip-compressed; Content-Encoding: gzip)
- Header row: `hex,count_good_aircraft,count_bad_aircraft`
- Hex: H3 cell ID (hexadecimal string, 16 chars)
- count_good_aircraft: aircraft in the cell that day with good navigation accuracy
- count_bad_aircraft: aircraft with degraded accuracy (low NIC); GPSJam colour = (bad-1)/total: green <2 %, yellow 2-10 %, red >10 %
- Aggregates 24-hour ADS-B position accuracy data from ADS-B Exchange

**Authentication:** None (public web interface)

**Rate Limit:** Not documented; assume standard web rate limits (1-2 req/sec)

**Fields of Interest:** `hex`, `count_good_aircraft`, `count_bad_aircraft`

**Latency:** 24 hours (daily update, next-day UTC 00:00)

**License:** Not explicitly stated; research-friendly use suggested

**Fixture Path:** `tests/fixtures/gpsjam/2026_09_29.csv` (1 KB sample; first 50 rows)

**Gotchas:** 
- Files are gzip-compressed; decompress after download (Content-Encoding header set)
- H3 resolution is fixed at level 4 (~1,770 km² per cell, edge ~22 km); no parameter for other resolutions
- Measures aircraft interference at cruise altitude; ground-level impacts differ significantly
- Some dates incomplete or missing (check for 0-row files)
- Manual update process (not real-time); occasional gaps in historical data
- File size typically 50-500 KB compressed

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

**Status:** broken (portal under maintenance; no JSON API confirmed)

**Base URL:** `https://msi.nga.mil`

**Intended Endpoints (Status: Unavailable):**
- `/api/publications/broadcast-warn?output=json&navArea=P` — HYDROPAC warnings (returns HTML error page)
- `/api/publications/broadcast-warn?output=json&navArea=11` — NAVAREA XI (returns HTML error page)
- `/api/swagger-ui.html` — Interactive API docs (503 Service Unavailable)
- `/api/publications/download` — PDF/data downloads (status unclear)

**Current Status:** Portal displays "MSI IS CURRENTLY UNDER MAINTENANCE" with notices to use PDF downloads instead.

**Authentication:** None (public navigation warnings)

**Rate Limit:** Not documented; assume standard REST limits

**Fields of Interest:** Notice ID, effective date, area (NAVAREA), coordinates, hazard type, chart corrections

**Latency:** 1 hour (Notice publication/distribution, when operational)

**License:** U.S. Government public data (no copyright)

**Fixture Path:** None (API unreachable; Swagger returns 503)

**Alternative Access:**
- NTM PDF releases available: https://msi.nga.mil/ (apology_objects path)
- Latest NTM releases linked on home page
- Consider scraping/parsing PDF NTMs as fallback

**Gotchas:** 
- JSON API endpoint not operational; portal under maintenance (as of Sept 2026)
- No confirmed JSON endpoint for automated queries
- Navigation warnings (HYDROPAC, NAVAREA XI) may be PDF-only
- Commercial use restrictions may apply to derived products
- U.S. domestic notices; international NAVAREA data may be sparse
- Maintenance window duration unknown; may persist

**Source:** [NGA MSI Home](https://msi.nga.mil/home), [Maritime Safety Products](https://www.nga.mil/resources/Maritime_Safety_Products_and_Services.html), [NTM Archive](https://msi.nga.mil/api/publications)

---

## china_msa

**Status:** blocked (access denied by firewall/geo-block)

**Base URL:** `https://www.msa.gov.cn`

**Intended Endpoints:**
- `/msacncms_wap/pages/info_warn.do?channelId=9C219298-B27F-460E-995A-99401B3FF6AF` — Navigation warnings (航行警告)
- Regional MSA offices: Shanghai, Tianjin, Guangdong, Fujian; separate channels
- Regional warning listings: Sansha/Hainan (三沙/海南), Guangdong (广东), Fujian (福建)

**Access Status:** HTTP 403 ERROR: ACCESS DENIED
- Direct curl requests blocked
- Likely geo-IP block (non-China origin) or firewall restriction
- Browser-based JavaScript may work if run from China IP

**Authentication:** None required (public); access blocked at network level

**Content Format:** HTML pages (no JSON API documented)

**Navigation Warning Categories:**
- 航行警告 (Navigation warnings): Military exercises, construction, hazards
- Regional coverage: East China Sea, Yangtze River, South China Sea, coastal waters
- Issuing offices: Shanghai MSA, Tianjin MSA, Guangdong MSA, Fujian MSA

**Latency:** 3600 seconds (daily/weekly updates, when accessible)

**License:** Chinese government public data

**Fixture Path:** None (access blocked)

**Workarounds:**
- Use VPN/proxy from China IP to bypass geo-block
- Archive.org Wayback Machine may have historical snapshots
- Contact China MSA directly for data access
- Monitor via news/satellite imagery as alternative

**Gotchas:** 
- Access blocked by network-level firewall (403); not account/auth issue
- No JSON/API endpoint; HTML only (requires scraping)
- Regional warnings scattered across multiple MSA office URLs
- Content in Simplified Chinese; no English mirror
- Geopolitical sensitivity (South China Sea territorial claims); data restricted by design
- Update frequency unclear; no RSS/API for monitoring

**Source:** [China MSA Home](https://www.msa.gov.cn/), [MSA Wikipedia](https://en.wikipedia.org/wiki/China_Maritime_Safety_Administration), Requires China-based IP or VPN

---

## Summary Table

| Source | Status | Key Gotcha |
|--------|--------|-----------|
| adsb_lol | verified | /v2/mil returns 503; use /v2/point + dbFlags filter |
| adsb_fi | verified | Non-commercial only; v2 deprecated |
| airplanes_live | needs-key | Contact approval required; no self-serve access |
| adsb_lol_history | verified | Multi-part tar archives; >2 GB each; no range requests |
| opensky | needs-key | 4,000 calls/day (auth); strict limits |
| gpsjam | verified | Daily gzip CSV at /data/YYYY-MM-DD-h3_4.csv |
| faa_nms_notam | needs-key | FAA approval required; no public API docs |
| aisstream | needs-key | WebSocket-only; 3-conn/acct limit |
| gfw | needs-key | Rate limits shared; commercial license |
| nga_msi | broken | Portal under maintenance; no JSON API confirmed |
| china_msa | blocked | Geo-IP 403 block; requires China-based IP or VPN |
