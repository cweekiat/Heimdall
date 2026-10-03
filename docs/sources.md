# Data sources (maintained by source-scout)
Registry: `config/sources.yaml`. Status legend: verified · needs-key · broken · unverified.

## Source set after the RF pivot (ADR-008, 2026-09-30)
Rule: keep a source only if it senses RF effects, explains them away (confounder), or verifies them physically.

| Status | Sources |
|---|---|
| **Kept** | adsb_lol, adsb_fi, opensky, opensky_global, adsb_lol_history (backtest, GNSS day builds), gpsjam (validation reference only), aisstream, faa_nms_notam, nga_msi, swpc, open_meteo, firms, celestrak, stac (sentinel-1-grd only) |
| **Planned, unverified** | P5: smap_rfi · P7: gdelt (wspr_live, pskreporter live since 032; HF detector 045) |
| **Added 047** | ourairports: airfields for "took off from" (public domain CSV, weekly, `davidmegginson.github.io`, approved 2026-10-03; 86,158 rows, 1,813 named military fields) |
| **Added 049** | osm_bases (Overpass, weekly, ODbL), gdelt (15 min, CAMEO 15/18/19/20, context only), news_rss (BBC World, Al Jazeera; display only). AP RSS: Cloudflare bot challenge, not used |
| **Blocked** | kiwisdr_dir: rx.kiwisdr.com → kiwisdr.com/public/ lists receivers only after a scripted click with a fixed auth header (bot gate, checked 2026-10-03); not worked around. Mirror rx.linkfanel.net = new host (needs approval) |
| **Removed in 027** | airplanes_live, gfw, china_msa, ioda, cloudflare_radar, black_marble, STAC sentinel-2-l2a; placeholders taiwan_mnd, japan_jso, reliefweb, acled, telegram |
| **Not adopted** | SatNOGS, KiwiSDR live receiver sessions, PSK Reporter REST, SMOS, GIRO ionosondes |

Sections of removed sources are kept below for history and marked **Removed in 027**.

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

> **Removed in 027 (ADR-008).** Kept for history.

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

> **Removed in 027 (ADR-008).** Kept for history.

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

> **Removed in 027 (ADR-008).** Kept for history.

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

## open_meteo

**Status:** verified

**Base URL:** `https://api.open-meteo.com/v1`

**Endpoints:**
- `/forecast` — Weather forecast (default 7 days; extendable to 16 days)
- Query parameters: `latitude`, `longitude`, `hourly`, `daily`, `past_days` (for archive lag), `forecast_days`

**Authentication:** None (public); commercial use requires API key and custom server

**Rate Limit:** Not documented; assume standard REST limits (typically generous for non-commercial)

**Fields of Interest:** temperature_2m, cloud_cover, precipitation, weather_code, wind_speed_10m (ADR-007: all)

**Latency:** Live forecast API; historical via `past_days` parameter (~1 day archive lag)

**License:** Non-commercial free; commercial requires subscription

**Fixture Path:** `tests/fixtures/open_meteo/real_san_francisco.json` (1.3 KB; 24h past + forecast)

**Gotchas:**
- archive-api.open-meteo.com not live; use api.open-meteo.com with `past_days` instead
- Response: `hourly`, `daily`, `current` objects with parallel time/value arrays
- No authentication for non-commercial use

**Source:** [API Docs](https://open-meteo.com/en/docs), [Historical](https://open-meteo.com/en/docs/historical-weather-api), [GitHub](https://github.com/open-meteo/open-meteo)

---

## firms

**Status:** unverified (needs key)

**Base URL:** `https://firms.modaps.eosdis.nasa.gov/api/v2.1`

**Endpoints:**
- `/active_fire/{region}/csv?key={MAP_KEY}&date={YYYY-MM-DD}` — Fire detections

**Authentication:** FIRMS_MAP_KEY (required; UI key store)

**Rate Limit:** Not documented; standard NASA EOSDIS

**Fields of Interest:** latitude, longitude, brightness, scan, track, acq_date, acq_time, satellite, instrument, confidence, bright_ti4, bright_ti5, frp, daynight (ADR-007 attrs)

**Latency:** 3 hours (NRT); ~1 day (archive)

**License:** NASA public; requires attribution

**Fixture Path:** Not yet created (requires key)

**Gotchas:** CSV format (not JSON); multiple regional endpoints

**Source:** [FIRMS Docs](https://www.earthdata.nasa.gov/learn/find-data/near-real-time/firms), [Home](https://firms.modaps.eosdis.nasa.gov/)

---

## ioda

> **Removed in 027 (ADR-008).** Kept for history.

**Status:** unverified

**Base URL:** `https://api.ioda.inetintel.cc.gatech.edu/v2`

**Endpoints:**
- `/outages` — Query outage signals

**Authentication:** None (public)

**Rate Limit:** Not documented

**Fields of Interest:** entity_type, entity_code, datasource, observed, baseline, level (ADR-007 attrs)

**Latency:** 600 seconds (10 min)

**License:** Academic; check ToS

**Fixture Path:** Not yet created

**Gotchas:** Response format complex (hierarchical JSON); rate limits not public

**Source:** [IODA API](https://ioda.inetintel.cc.gatech.edu/help/api), [Project](https://www.inetintel.cc.gatech.edu/)

---

## cloudflare_radar

> **Removed in 027 (ADR-008).** Kept for history.

**Status:** unverified (needs key)

**Base URL:** `https://api.cloudflare.com/client/v4/radar`

**Endpoints:**
- `/bgp/top/ases`, `/bgp/top/prefixes`, `/attacks`

**Authentication:** CLOUDFLARE_TOKEN (required; UI key store)

**Rate Limit:** Not documented; standard Cloudflare API

**Fields of Interest:** location, asn, scope, outage_type (ADR-007 attrs)

**Latency:** 900 seconds (15 min)

**License:** Cloudflare ToS

**Fixture Path:** Not yet created (requires key)

**Gotchas:** Key required; multiple endpoint types

**Source:** [Radar API](https://developers.cloudflare.com/radar/), [API Docs](https://developers.cloudflare.com/api/)

---

## celestrak

**Status:** unverified

**Base URL:** `https://celestrak.org`

**Endpoints:**
- `/api/v2/satcat` — Satellite catalog
- `/api/v2/decay` — Decayed tracking
- `/api/v2/countries`, `/api/v2/groups` — Filters

**Authentication:** None (public)

**Rate Limit:** Not documented; standard HTTP

**Fields of Interest:** norad_id, name, epoch, semi_major_axis, eccentricity, inclination, raan, arg_of_pericenter, mean_anomaly (ADR-007 orbit_elements)

**Latency:** 86400 seconds (daily TLE updates)

**License:** NORAD TLE public domain; Celestrak adds metadata

**Fixture Path:** Not yet created

**Gotchas:** JSON format; multiple APIs available

**Source:** [CelesTrak Docs](https://celestrak.org/), [GitHub](https://github.com/cbassa/celestrak)

---

## earth_search (STAC)

> **ADR-008:** only `sentinel-1-grd` is kept (RF interference annotations). `sentinel-2-l2a` is removed in 027.

**Status:** unverified

**Base URL:** `https://earth-search.aws.element84.com/v1`

**Endpoints:**
- `/search` — STAC Item Search (POST/GET)
- `/collections` — Available collections (S1, S2, etc.)

**Authentication:** None (public; no token for item search)

**Rate Limit:** Not documented; AWS standard

**Fields of Interest:** stac_item_id, collection, platform, datetime, geometry, cloud_cover, relative_orbit, orbit_state (ADR-007)

**Latency:** 86400 seconds (daily catalog updates)

**License:** Sentinel data Copernicus; STAC metadata CC-BY-4.0

**Fixture Path:** Not yet created

**Gotchas:** Metadata only (no COG/thumbnail fetches per ADR-007); Sentinel thumbnails declined

**Source:** [Earth Search](https://www.element84.com/earth-search/), [STAC Spec](https://github.com/radiantearth/stac-api-spec)

---

## black_marble

> **Removed in 027 (ADR-008).** Kept for history.

**Status:** unverified (needs Earthdata access)

**Base URL:** `https://ladsweb.modaps.eosdis.nasa.gov`

**Endpoints:**
- `/api/v2/content/archives/COLORMAPS/BlackMarble` — Dataset archive
- Alternative: `https://data.naaee.xyz/` (CDN)

**Authentication:** Earthdata token (urs.earthdata.nasa.gov; UI key store)

**Rate Limit:** EOSDIS standard (~5–10 req/sec)

**Fields of Interest:** h3_r4, date, product, n_valid, n_total, valid_frac, cloud_frac, median, p90, mean_radiance (ADR-007)

**Latency:** 86400 seconds (monthly/seasonal; lag ~1–3 months)

**License:** NASA public; attribution + EOSDIS science terms

**Fixture Path:** Not yet created (HDF5 listing only per ADR-007; no tile sample)

**Gotchas:** HDF5 format (h5py); tiles downloaded→reduced to H3-r4→deleted (no pixel arrays stored)

**Source:** [LAADS Web](https://ladsweb.modaps.eosdis.nasa.gov/), [Black Marble](https://earthobservatory.nasa.gov/images/topic/earth-at-night), [Register](https://urs.earthdata.nasa.gov/users/new)

---

## swpc

**Status:** unverified

**Base URL:** `https://services.swpc.noaa.gov/products`

**Endpoints:**
- `/noaa-planetary-k-index-forecast.json` — Kp forecast
- `/noaa-space-weather-3-day-outlook.json` — 3-day outlook

**Authentication:** None (public)

**Rate Limit:** Not documented; NOAA standard

**Fields of Interest:** kp, g_scale, r_scale, s_scale, product (ADR-007 confounder only)

**Latency:** 900 seconds (NOAA ~4x/day updates)

**License:** NOAA public; no copyright

**Fixture Path:** Not yet created

**Gotchas:** Forecast-only; used as confounder (lowers confidence, never creates Evidence per ADR-007)

**Source:** [SWPC Products](https://www.swpc.noaa.gov/), [JSON Services](https://services.swpc.noaa.gov/products/), [Kp Info](https://www.swpc.noaa.gov/products/planetary-k-index)

---
| open_meteo | verified | archive-api not live; use api.open-meteo.com with past_days |
| firms | unverified | Requires FIRMS_MAP_KEY; CSV format |
| ioda | unverified | Public but response format complex; rate limits not documented |
| cloudflare_radar | unverified | Requires CLOUDFLARE_TOKEN; multiple endpoint types |
| celestrak | unverified | Public TLE data; JSON format via v2 API |
| earth_search (STAC) | unverified | No token needed; metadata only per ADR-007 |
| black_marble | unverified | Requires Earthdata auth; HDF5; tiles→H3-r4 reduction→delete |
| swpc | unverified | Public; Kp forecast for confounder use only per ADR-007 |

### P3 live reachability check (lead, 2026-09-30)
Unauthenticated GETs from this machine:
| Source | Endpoint | Result |
|---|---|---|
| celestrak | `gp.php?GROUP=active&FORMAT=json` | 200, ~7 MB JSON. Use narrower groups (e.g. `GROUP=military`, `resource`) or per-NORAD queries |
| earth_search | `/v1/collections` | 200 (STAC, no key) |
| ioda | `/v2/signals/raw/country/MM?from&until` | 200 (no key) |
| swpc | `/products/noaa-planetary-k-index.json` | 200 (no key) |
| firms | `/api/area/` | 200 index page. Data calls need `MAP_KEY` |
| black_marble | ladsweb `/api/v2/content/details/allData/5200/VNP46A1` | 200 listing. File download needs an Earthdata token |
| cloudflare_radar | `/client/v4/radar/annotations/outages` | 400 without a token, as expected. Needs a free API token with Radar read |
| open_meteo | `/v1/forecast` (see above) | 200, verified |

---

## Spectrum sources (ADR-008), checked live 2026-10-01
All connectors emit GeoEvents with `domain: spectrum`. HF spots are mapped at the great-circle midpoint of each path
(the ionospheric reflection region), aggregated per 10-min bin, amateur band and 2° cell inside the Indo-Pacific box
`[60, -20, 160, 50]`. Tests: `tests/unit/test_spectrum_connectors.py` (fixtures recorded from these calls).

| Source key | Endpoint | Auth | Live result (2026-10-01) | Event kind |
|---|---|---|---|---|
| wspr_live | `https://db1.wspr.live/?query=<SQL>` (ClickHouse, `wspr.rx`) | none | ~333k spots/h global, data minutes old; server-side aggregation → ~1.9k rows/h in box, 0.09 s; one poll = 403 events | `hf_spots` |
| pskreporter | `https://retrieve.pskreporter.info/query?flowStartSeconds=-900&rronly=1&noactive=1&lastseqno=N` (XML) | none | ~1.5k reports per query globally, 23 bins in box; ≤ 1 query / 5 min (provider rule). MQTT feed (`mqtt.pskreporter.info` 1883–1886) unreachable through the egress proxy | `hf_spots` |
| satnogs | `https://network.satnogs.org/api/observations/?start=&end=` (cursor `Link: rel=next`), `/api/stations/` daily | none | ~240 observations/h global (10 pages); 50 active stations in box; one poll (3 h) = 110 in-region passes | `sat_observation` |
| sentinel1_rfi | Planetary Computer STAC `collections/sentinel-1-grd/items` + free SAS token `api/sas/v1/token/sentinel-1-grd`; asset `schema-product-<pol>` = `annotation/rfi/rfi-*.xml` (~12 KB) | none | 16 scenes / 3 days over the AOIs; one with RFI detected over northern Myanmar (29 Sep, 30 % noise reports flagged, burst power ratio 18.7) | `sar_rfi` |
| smap_rfi | CMR `search/granules.json?short_name=SPL1BTB_NRT&bounding_box=` → NSIDC Earthdata Cloud `.h5` (~40 MB) | EARTHDATA_TOKEN | CMR search OK: 8 granules in 12 h, NRT ~3 h. Download **not verified**: token not set. Field names/RFI bit per L1B spec; check with `scripts/smap_inspect.py` | `l_band_rfi` |
| cygnss | CMR `search/granules.json?short_name=CYGNSS_L1_V3.2` → OPeNDAP `opendap.earthdata.nasa.gov/collections/C2832195379-POCLOUD/granules/<title>.dap.nc4?dap4.ce=/ddm_timestamp_utc;/sp_lat;/sp_lon;/ddm_noise_floor;/quality_flags` | EARTHDATA_TOKEN | 7 satellites (no cyg06), one daily granule each, 2–3 days late; subset 7.9 MB in 2–4 s (full file 1.15 GB); 2026-09-12..29 backfilled (task 038) | `l1_noise` |
| swpc (X-ray, P6) | `https://services.swpc.noaa.gov/json/goes/primary/xrays-6-hour.json` (same host as Kp) | none | 716 rows / 6 h (1-min, two energy bands); 0.1–0.8 nm kept, peak per 5 min (2026-10-03) | `solar_xray` |
| smos | ESA SMOS dissemination | ESA EO account | **Blocked**: CMR collection `SMOS_Open_V7` lists no files; data only behind ESA login. Registered disabled, no connector | – |

Gotchas
- WSPR: keep aggregation server-side; raw spots are ~300k/h. Very long paths (>12,000 km) are excluded because their
  midpoint is ambiguous.
- PSK Reporter: callsigns are never stored; only counts and locator-derived positions. XML with DOCTYPE/ENTITY is rejected.
- SatNOGS: status `unknown` becomes good/bad/failed after vetting; re-polling a 3 h window upserts the new status.
- Sentinel-1: only the RFI XML assets on `*.blob.core.windows.net` are fetched (no imagery; user decision 2026-09-30).
- CYGNSS: never download the full L1 file; the OPeNDAP subset is reduced to r3 cell JSON and deleted. Granule titles
  are regex-checked before use in paths. `sp_lon` is 0–360. The rfi_detected bit is noisy over dry land.
- SMAP: rasters are reduced to per-H3-r4 counts and deleted (ADR-007 s2); the token goes only in the Authorization header.

## Military air activity (task 039), checked live 2026-10-01
| Source key | Endpoint | Auth | Live result | Output |
|---|---|---|---|---|
| adsb_mil | `https://api.adsb.lol/v2/mil`, fallback `https://opendata.adsb.fi/api/v2/mil` (readsb v2 JSON) | none | 164–259 military aircraft worldwide per call, ~110 KB; polled every 60 s | AircraftObs `is_global`, `mil` (live map, military tracks, theatre detectors; never the per-area GNSS detectors) |
| plane_alert_db | `https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db/main/plane-alert-db.csv` | none | 2.4 MB, 17,259 rows (Mil 9,856 · Civ 4,643 · Gov 1,764 · Pol 996); identifies ~53 % of `/v2/mil` aircraft; ODbL 1.0 (credited in About) | `data/ref/plane-alert-db.csv`, refreshed daily; no events |

Gotchas
- plane-alert-db headers carry `$`/`#` markers (`$ICAO`, `#CMPG`, `$#Tag 2`); its text is community data, bounded
  and displayed only. Its type name is shown only when its ICAO type agrees with the broadcast type.
- Airliner types (A332, B762, DC10) are tankers only when the list tags them `Air2Air`; police "Surveillance" is not ISR.
