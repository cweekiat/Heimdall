# Detectors (math as implemented — detection-scientist keeps this current)

All detectors are pure: `detect(obs, cfg, now, baseline) -> list[Evidence]`, where
`baseline(key, metric) -> list[float]` is supplied by the pipeline. Windows are aligned: the window
end is `now` floored to the `step_min` grid (from UTC midnight), start = end − `window_min`,
interval `[start, end)`. Evidence ids are `sha1(kind|cell|entity|window_start ISO)` (entity = ""
for cell-level jamming). Every threshold below is a key in `config/config.yaml`; defaults are in
each module's `DEFAULTS`. Shared helpers: `heimdall/detect/common.py`, `heimdall/geo.py`.

## GNSS jamming (detect/jamming.py, `jamming-1.0.0`)
Scope: airborne (`on_ground=false`) **true ADS-B** reports (`src_type ∈ {adsb_icao, adsb_icao_nt}`).
Cell = H3 r4, window 30 min, step 5 min. MLAT/TIS-B aircraft count only in `coverage.n_observed`.

* Report is *degraded* if NIC < `nic_bad_below` (7) or NACp < `nacp_bad_below` (8); a report
  with neither field is *unknown* and ignored.
* Aircraft (keyed by ICAO hex across all aggregators) is **bad** if degraded/known ≥ `bad_dwell_frac` (0.5).
* total = aircraft with ≥ 1 known report; R' = max(0, bad − 1) / total (GPSJam de-noise, so a
  single faulty transponder gives R' = 0). total = 0 ⇒ no Evidence (MLAT-only cells cannot fire).
* Baseline: values of metric `jam_ratio` for the cell over `baseline_days` (14 d), excluding
  samples at/after the current window start (no self-contamination).
  z = (R' − median) / (1.4826·MAD + `mad_floor`), `mad_floor` = 0.02.
* **Trigger** (cell not active): total ≥ `min_aircraft` (5) ∧ R' ≥ `ratio_alert` (0.10) ∧
  (z ≥ `robust_z` (3) or baseline has < `baseline_min_samples` (288) values → gap "no baseline").
* **Hysteresis hold** (cell had jamming Evidence in the previous step, passed in as `active`):
  total ≥ `min_aircraft` ∧ R' ≥ `ratio_clear` (0.06); z not required. Rule `hysteresis_hold`.
* Emitter estimate: first degraded fix of each bad aircraft → centroid and 2σ covariance ellipse
  in a local equirectangular frame, with a (5 km)² variance floor per axis (non-degenerate for
  < 3 aircraft). Features `emitter_lat/lon`, `ellipse_major_km/minor_km/angle_deg`.
* Provider agreement = mean over bad aircraft of (#providers whose own reports make it bad /
  #providers that saw it).
* Coverage confidence = min(1, total / 15) × agreement × (0.8 if only one provider).
* Score = min(1, R' / (3 × ratio_alert)).
* Counterfactual: "would not fire if ≤ ⌊thr·total + 1⌋ of total were degraded", plus the
  baseline median that would have silenced the z-test.

## GNSS spoofing (detect/spoofing.py, `spoofing-1.0.0`)
Per aircraft in the 30-min window; true-ADS-B fixes de-duplicated by timestamp (tie → provider name),
then thinned so consecutive fixes are ≥ `min_dt_s` (5 s) apart.

* Pair test: v_calc = haversine / Δt. Flag if v_calc > `teleport_kt` (1323 kt, Mach 2) or, for
  Δt ≤ `speed_pair_max_s` (30 s) with gs on both fixes, |v_calc − mean gs| > `speed_diff_kt` (150).
* Glitch removal: fix i is a glitch if pairs (i−1,i) and (i,i+1) are flagged but (i−1,i+1) is not;
  glitches are dropped and pairs recomputed.
* **S1 teleport**: after glitch removal, persisted = #flagged pairs, or (if exactly one) 1 + clean
  pairs after it; S1 if persisted ≥ `persist_pairs` (2).
* **S2 alt divergence**: d = alt_geom − alt_baro per fix. Regional norm = median over aircraft in
  the same r4 cell of each aircraft's median d (needs ≥ `alt_norm_min_aircraft` = 3, else norm 0 +
  gap). S2 if ≥ 2 fixes and ≥ 50 % of fixes have |d − norm| > `alt_divergence_ft` (1000).
  Limitation: area-wide spoofing that shifts every aircraft moves the norm too.
* **S3 MLAT gap**: for each MLAT fix of the same hex, nearest ADS-B fix with |Δt| ≤ 10 s; bad if
  gap > `mlat_gap_km` (5) + gs·|Δt|. S3 if ≥ `mlat_min_pairs` (2) bad pairs.
* **S4 cluster**: 60-s bins; per aircraft mean position and median track; an aircraft with
  ≥ 3 aircraft (incl. itself) within `cluster_km` (2 km) whose tracks have circular variance
  ≥ 0.3 (disagree) is hit; S4 if hit in ≥ `cluster_min_bins` (2) bins. Formations (same track) excluded.
* **S5 NIC trusted**: (S1 or S3) ∧ median NIC of the anomalous fixes ≥ 7 (receiver trusts the fake).
* **Alert** if #signals ≥ `min_signals` (2) or S3 fired. Location = last good fix before the jump
  (S1), else the MLAT position (S3), else the first divergent / clustered fix.
* Score = max(0.5 if S3, min(1, #signals / 4)). Confidence = min(1, n_fixes / 20) × (0.8 if single provider).

## Military orbit (detect/orbit.py, `orbit-1.0.0`)
Military = `mil` flag or type in `mil_types`. All source types (MLAT allowed). Window 75 min.
Fixes de-duplicated per timestamp and thinned to ≥ `resample_s` (30 s). Heading = reported track,
else bearing from previous fix.

* circ_var = 1 − |mean unit heading vector|; cum_turn = Σ |angle_diff| over steps ≥ `turn_deadband_deg`
  (3°; wrap-safe, 359→1 = 2°); D = net displacement / path length.
* **Orbit** if n_points ≥ `min_points` (20) ∧ circ_var ≥ 0.5 ∧ cum_turn ≥ 360° ∧ D ≤ 0.25.
  (A 60 NM-leg racetrack at 420 kt: 150 pts, circ_var 0.89, 1260°, D 0.11 at 75 min; at 30 min
  D = 0.28 → not yet.)
* Cell = H3 r4 of the track centroid. Novelty: n = #values of metric `orbit_active` for the cell
  (14 d, excluding the current window); prior_hours = n·step/60; novelty = exp(−prior_hours / 24).
  No baseline callable → novelty 0.5 + gap.
* Score = 0.4 + 0.3·novelty + 0.3·clip((laps − 1)/2, 0, 1), laps = cum_turn/360.
* Confidence = min(1, n_points / 40) × (0.8 if > 50 % MLAT fixes).
* `jam_evidence`: distance from centroid to nearest jamming Evidence → features `jam_distance_km`,
  `near_jamming` (≤ `jam_proximity_km` 100) and informational rule `near_jamming`.

## Fusion v0 (fuse/fusion.py) and Admiralty (fuse/admiralty.py)
1. **Persistence gate**: pipeline sets `persistence` = 1 + persistence of the same kind in the same
   cell whose window_end ∈ [end − 1.5·step, end − 0.5·step] (orbits: same aircraft too). Alert only
   if persistence ≥ `fusion.persistence_windows[kind]` (2).
2. Group gated Evidence by (kind, cell). Merge into the open (NEW/CONFIRMED) alert for that
   kind+cell (evidence ids appended, last_seen = max, severity never downgrades while open), else
   new alert id = sha1(alert|kind|cell|first_seen).
3. Severity from s = lead.score × lead.coverage.confidence: ≥ 0.6 high, ≥ 0.35 medium,
   ≥ 0.15 low, else info. **CRITICAL composite**: ≥ 2 distinct tanker (`tanker_types`) orbit
   Evidence (gated) within 100 km of a gated jamming Evidence → `composite` alert in the jam cell.
4. **Control AOIs** (role=control, e.g. Baltic) produce Evidence and alerts, severity capped at info.
5. Corroborations: other-kind gated Evidence in the same cell = `context`. Status "corroborated"
   only when a *different domain* supports; otherwise "uncorroborated" (never "false").
6. **Admiralty reliability = best grade among contributing providers** (sources.yaml; provider
   'adsb.lol' → key 'adsb_lol'; unknown → F). Rationale: aggregators mirror the same broadcast, so
   the finding is as reliable as the best source showing it; a weaker extra mirror must not
   downgrade it. **Credibility**: 1 analyst-confirmed or ≥ 3 domains; 2 two domains; 3 single
   domain with lead score ≥ `strong_score` (0.6) and persistent; 4 single domain otherwise;
   5 contradicted; 6 no evidence.
7. Summary is a template over Evidence features (no LLM). `recommended_actions` = playbook ids
   whose `applies_to` contains the kind or "*".
8. Open alerts without fresh Evidence for `expire_after_min` (60) → EXPIRED.

## Pipeline (pipeline.py)
`tick(store, settings, now)`: one `aircraft_window` pull covering the longest window; per AOI
(radius filter) run jamming (with `active` cells from `evidence_history`), spoofing, orbit (with
the jamming Evidence); set `aoi` + persistence; record `jam_ratio` per cell with total ≥
`min_aircraft` and `orbit_active` per orbit cell; fuse against open alerts; upsert Evidence and
alerts. Re-running the same tick is idempotent (same ids, persistence not inflated).

## Calibration status
All thresholds are literature/GPSJam priors, **not yet backtested** (Baltic control run pending
in P1). Most sensitive: `ratio_alert`/`mad_floor` (jamming), `speed_diff_kt` (spoofing S1 on
stale gs), `cluster_*` (holding stacks), `novelty_scale_h`.
