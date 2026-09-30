# Fixtures

Recorded fixtures live in `adsb_lol/`, `adsb_fi/`, `gpsjam/`. Task 009 fixtures below are
**HANDCRAFTED** (no real recording exists: NGA MSI was under maintenance and China MSA geo-blocks
non-China IPs during task 005; FAA NMS needs approval). They follow the documented/assumed payload shapes
and must be replaced by recordings when access exists.

- `nga_msi/broadcast_warn_hydropac.json` — handcrafted; shape `{"broadcast-warn": [...]}` per NGA MSI docs.
- `faa_nms/notams_sample.json` — handcrafted; GeoJSON-like `items[].properties.coreNOTAMData.notam` (assumed).
- `notices/injection.html`, `notices/injection.json` — adversarial (script tag, "ignore previous
  instructions", links, ~100 KB coordinate-like string). Generated once; inert data for untrusted-text tests.
