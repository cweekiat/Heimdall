# Detectors (math as implemented — detection-scientist keeps this current)

All detectors are pure: `detect(obs, cfg, now, baseline) -> list[Evidence]`, where
`baseline(key, metric) -> list[float]` is supplied by the pipeline. Windows are aligned: the window
end is `now` floored to the `step_min` grid (from UTC midnight), start = end − `window_min`,
interval `[start, end)`. Evidence ids are `sha1(kind|aoi|cell|entity|window_start ISO)` (entity = ""
for cell-level jamming); detectors take a keyword `aoi`. Every Evidence also carries
`anomaly_first_ts`/`anomaly_last_ts` (epoch s) = time span of the observations that constitute the
anomaly, used for persistence. Every threshold below is a key in `config/config.yaml`; defaults are in
each module's `DEFAULTS`. Shared helpers: `heimdall/detect/common.py`, `heimdall/geo.py`.

## GNSS jamming (detect/jamming.py, `jamming-1.1.1`)
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
* Coverage confidence = min(`confidence_cap`, min(1, total / 15) × agreement × (0.8 if only one
  provider)). `confidence_cap` defaults to 1 (no cap); sparse-traffic AOI profiles lower it
  (gap "confidence capped"), so severity = score × confidence cannot reach HIGH on thin data.
* **Per-AOI overrides** (1.1.0): an AOI may carry `detectors: {jamming: {...}}` in config.yaml,
  merged key-by-key over the global `detectors` block (`Settings.detectors_for`). The pipeline's
  observation pull covers the longest window (+ one step) of any AOI.
* Records: one RecordRef per (bad aircraft, provider whose *own* reports make it bad), i.e. only
  anomaly-showing providers. Anomaly span = first..last degraded report of the bad aircraft.
* Score = min(1, R' / (3 × ratio_alert)).
* Counterfactual: "would not fire if ≤ ⌊thr·total + 1⌋ of total were degraded", plus the
  baseline median that would have silenced the z-test.

## GNSS spoofing (detect/spoofing.py, `spoofing-1.0.2`)
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
* Anomalous fixes = S1: flagged pairs are **not** assumed to alternate out/back (1.0.1). The
  first flagged pair of a capture opens a displaced segment and fixes its *anchor* (last good fix
  before the jump). A later flagged pair (i, i+1) closes the capture only if it re-converges with
  the anchor: pair(anchor, fix i+1) passes the pair test **and** d(anchor, fix i+1) < d(anchor,
  fix i) (the jump points back). Otherwise it is a *further* jump (displaced → displaced): the
  current segment ends and a new one starts at that jump (anchor unchanged). Each segment
  contributes its fixes with ts − t_jump < `s1_span_cap_s` (300 s, strictly under one step), so a
  single jump — even one whose displaced track continues all window — can never span a full
  persistence step on its own. Limitation: a window that starts mid-capture sees the jump *back*
  first and treats it as an opening jump; S2 divergent fixes; S3 ADS-B+MLAT pair fixes;
  S4 first fix of each hit bin (fired signals only).
* Records: showing providers = providers whose own report reproduces an anomalous fix (same src
  type, |Δt| ≤ 2 s, ≤ 1 km); records = their first matching fix ∪ context records (S1 pre-jump
  anchor + first displaced fix, S3 ADS-B + MLAT truth fix) whose provider is a showing provider,
  de-duplicated. Reliability uses only these sources.

## Military orbit (detect/orbit.py, `orbit-1.0.1`)
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
* Records: first and last fix of each provider with ≥ min_points/2 raw fixes of the track (else
  the providers of the de-duplicated track fixes only). Anomaly span = first..last resampled fix.
* `jam_evidence`: distance from centroid to nearest jamming Evidence → features `jam_distance_km`,
  `near_jamming` (≤ `jam_proximity_km` 100) and informational rule `near_jamming`.

## AIS spoofing (detect/ais_spoofing.py, `ais-spoofing-1.1.0`)
Input `Sequence[VesselObs]`, window 60 min at a 5-min step (aligned like the GNSS detectors).
Invalid positions (out of range, exactly (0, 0)) are dropped first; (mmsi, ts) duplicates from several
receivers are one fix. Every Evidence carries per-signal rules, `anomaly_first_ts`/`anomaly_last_ts`,
one RecordRef per anomalous MMSI fix (capped at 40), a counterfactual and coverage
(`n_observed` = MMSIs in the r4 cell; confidence = min(1, fixes/20), x 0.8 if one provider).
* **A1 jump** (per MMSI, `per_vessel`=1): pair flagged iff implied speed > `teleport_kn` (60) **and**
  distance > `jump_min_km` (5). Glitch removal as GNSS S1 (both adjacent pairs flagged, bridging pair
  clean -> drop the fix). Fires when flagged pairs (+ trailing clean pairs if exactly one jump) >=
  `persist_pairs` (2). Anomalous span = fixes within `span_cap_s` (300) after each jump, so a lone
  jump cannot reach persistence 2. Score 0.5 + 0.5 min(1, v_max / (10 x teleport)).
  **MMSI-collision guard**: fixes split by distance from the first fix (> `collision_km` 50) with
  >= 3 switches and no flagged pair *inside* a cluster = two ships, one MMSI -> no Evidence
  (`ais_spoofing.mmsi_collisions` exposes the diagnosis, gap "mmsi_collision").
* **A2 identical**: per `identical_bin_s` (300) bin and position rounded to `identical_decimals` (5,
  ~1 m) >= `identical_min_vessels` (3) distinct MMSIs. Rafted ships 20 m apart differ at 5 decimals.
  Score 0.6 + 0.4 min(1, (n-2)/5).
* **A3 ring**: per `ring_bin_s` bin (last fix per MMSI) >= 5 MMSIs with centroid radial distances
  CV = sd/mean <= 0.1, bearing circular variance (geo.circular_variance) >= 0.7 and median radius
  in [0.2, 30] km. Score 0.7 + 0.3 min(1, n/10). Evaluated per neighbourhood, not over the whole
  AOI (background traffic moves a global centroid): fixes are bucketed in 4 half-cell-shifted square
  grids (cell side 4 x 30 km, local km projection) and each bucket with >= 5 vessels is tested;
  if the raw bucket fails, a RANSAC circle search (circumcircle of 3 vessels with R in the radius
  range, inliers within `ring_cv_max` x R, <= 2000 seeded samples, exhaustive if fewer) proposes concentric
  sets. **Chance-alignment guard** (dense ordinary traffic contains many accidental rings): a
  candidate with k inliers is kept only if (a) Poisson upper tail P(X >= k), mean lam = rho x
  4 pi R tol (tol = `ring_cv_max` x R, rho = other MMSIs / max(hull area of the neighbourhood,
  pi R^2)), times the Bonferroni family size C(n,3) x (in-range fraction of sampled triples), is
  < `ring_poisson_p` (1e-3), and (b) the interior is hollow: distinct MMSIs strictly inside 0.8 R
  <= max(`ring_hollow_abs` (1), `ring_hollow_frac` (0.5) x rho pi (0.8 R)^2). The kept candidate
  with the smallest p (ties: most inliers) must then pass the same CV/bearing/radius rules. Duplicate sets
  from the shifted grids are merged by MMSI set and by r4 cell.
* **A4 circle** (per MMSI, `per_vessel`=1): >= 20 fixes, Kasa least-squares circle fit (centred
  normal equations, 3x3 elimination; degenerate -> none) with relative RMSE <= 0.05, radius <= 5 km
  and |net signed turn| = |sum of angle_diff of segment bearings| >= 720 deg (>= 2 laps; a one-lap
  purse-seine set gives ~360 and does not fire). Score 0.6 + 0.4 turn/1440.
Not detected: identity spoofing (MMSI/IMO mismatch), land mask. All thresholds are priors, not backtested.

## AIS dark gap (detect/ais_gap.py, `ais-gap-1.2.0`)
Lookback `window_h` (24). Gap = consecutive fixes of one MMSI with dt >= `gap_min_h` (2), or *open*
(no fix from the last fix to the aligned `now`, >= 2 h). A closed gap is reported for
`closed_report_h` (6) after it ended. Skipped if at the last fix nav_status in {1, 5} and sog < 0.5 kn
(unknown sog counts as slow). **Coverage test**: the gap is cut into hourly bins from the last fix;
a bin is covered iff >= `gap_min_neighbors` (3) *other* MMSIs were heard in it within
`gap_neighbor_km` (20) of the last fix; Evidence only if covered bins / bins >= `gap_covered_frac`
(0.5), else `find_gaps` reports reason "no reception evidence" and nothing is emitted. Bins are
`ceil(gap_h)` (the final partial hour counts as a bin), except that a trailing partial bin shorter
than `gap_min_bin_min` (30 min) is ignored (a few minutes is rarely covered and would drag the
covered fraction down). **Feed-live test**
(open gaps only): >= `gap_min_neighbors` other MMSIs must be heard near the last fix within the
final `gap_live_h` (1 h) before `now`, else reason "feed not live" (a receiver outage or a source
toggled off must not turn every ship dark). **AOI assignment**: gaps are computed on the
*unfiltered* vessel window (neighbour coverage near the AOI edge included) and assigned to an AOI
by the ship's last fix before the gap (`in_region`), so a ship sailing out of the AOI is not an
open gap. Features
gap_h, open, dist_km, implied_speed_kn (dist / gap_h; 0 for open), covered_frac,
neighbors_median. score = min(1, gap_h/12) x covered_frac; coverage confidence = min(1, median
neighbours per covered bin / 6). Persistence subject `aoi|ais_gap|mmsi:<mmsi>`: an open gap has a
fixed run start (the last fix), so it reaches persistence 2 on the second tick.

## Warning areas (detect/warning_area.py, `warning-area-1.0.0`, ADR-005 A/B)
One `WARNING_AREA` Evidence per **active** notice (kind nav_warning|notam, `attrs.status=="active"`,
`valid_from <= now`, `valid_to` None or > now) of a configured category (military_exercise,
live_firing, missile_rocket, gnss_interference) that intersects the AOI (centroid or any covering
cell centre within radius + 25 km). Context only: fusion never opens an alert from it. `title`/`text`
are never read; only `attrs.ref` (re-sanitised to `[A-Z0-9/ -]`, 40 chars), source, category, id,
url and times enter Evidence. Partial geometry (no_geometry/too_large/partial) -> centroid match,
confidence 0.6.

## Thermal anomalies (detect/thermal.py, `thermal-1.0.0`, ADR-007, task 021a)
Input: FIRMS `thermal` GeoEvents (value = FRP MW, `attrs.confidence`, `attrs.satellite`); `text`/`title`
never read. Window = `window_h` (24 h) aligned to the hour. `detect(events, baseline_events, cfg, now, *, aoi)`.
1. Keep confidence in `min_confidence` (nominal, high) and inside the AOI radius. Low-confidence pixels
   are dropped for rules and baselines.
2. **Static mask**: r8 cell with detections (any confidence) on >= `static_days` (10) distinct UTC days of
   the `baseline_days` (60) before the window -> events in it are dropped; the r4 Evidence gets gap
   "static heat source masked: N detections". If everything is masked there is no Evidence, hence no gap.
3. Cluster per r6 cell: T1 `n >= min_detections` (3); T2 `sum FRP >= frp_min_mw` (50);
   T3 `ratio = N_r4_window / (B + 1) >= ratio_min` (3), with B = mean unmasked detections per day in the r4
   cell over the baseline (`count / min(baseline_days, max(history_days, 1))`). **Fire = (T1 or T2) and T3.**
   `+1` keeps a quiet cell from dividing by ~0: 3 fresh detections need a ratio of 3 -> B <= 0. With 5
   detections B up to 0.67/day still fires.
4. Coverage: `n_observed` = distinct passes (satellite, 10-min bin) in the cluster;
   `confidence = min(1, passes / expected_passes)` (4). `n_quality` = high-confidence detections.
   History length is estimated from the earliest baseline event in the feed; shorter than `baseline_days`
   -> gap "short baseline: ..." and score capped at 0.6.
5. `score = clamp(ratio / (2 ratio_min)) * coverage_confidence`. Records: up to 50 FIRMS event ids
   (strongest FRP first). Counterfactual: "would not fire with < 3 detections or ratio < 3".
Known limits: pixel jitter across r8 boundaries can leak a flare (T3 ratio is the backstop); no land
cover mask (crop burning suppressed only via the baseline); persistence is fusion's (>= 2 windows);
priors, not backtested (calibrate on the FIRMS archive, task 016).

## Internet outage (detect/outage.py, `outage-1.0.0`, ADR-007, task 021b)
`detect(events, cfg, now, *, aoi)` over IODA `outage_signal` (value = observed/baseline; recomputed from
attrs if None) and Cloudflare `outage_annotation` GeoEvents, window `window_h` (6 h). Entity =
`entity_type:entity_code`, located at the mean event lat/lon and required within AOI radius +
`aoi_margin_km` (100).
* **O1**: in one time bin >= `min_datasources` (2) datasources have ratio <= `drop_ratio_max` (0.7).
  Different bins do not add up, so a single-datasource blip is silent.
* **O2**: an active annotation (`valid_from < window_end`, `valid_to` None or >= window_start) with
  `attrs.location == entity_code`. Supporting; fires alone only if `annotation_alone` (default false),
  then keyed `annotation:<loc>` and requiring the annotation to carry lat/lon.
* Score = clamp(1 - min_ratio) (+0.15 with O2); 0.3 for annotation-only. Features `min_ratio`,
  `n_datasources`, `duration_h` (first to last qualifying bin + 5 min), `annotation_active`.
* Coverage: `n_observed` = datasources with data, `confidence = with_data / len(datasources)`; gap
  "datasource missing: <name>". `geometry` None. `title`/`text` are never read; `attrs.location` is
  re-sanitised to `[A-Za-z0-9_-]`.

## Night-lights drop (detect/night_lights.py, `night-lights-1.0.0`, ADR-007, task 021b)
Per r4 cell, nightly `night_lights` events (mean radiance of clean pixels, `attrs.date`, `valid_frac`).
Valid night: N3 `valid_frac >= min_valid_frac` (0.5); invalid nights are skipped everywhere. Forward pass:
B = median of the last `baseline_nights` (30) *clean* (not-dropped) valid nights, needing >=
`min_history_nights` (5); a night is dropped iff N1 `B >= min_baseline_radiance` (5.0) and N2
`value / B <= drop_ratio_max` (0.5). Dropped nights do not enter the baseline; a non-dropped night resets
the run. Fire iff the run ends on the latest reported night (which must be valid) and N4 run length >=
`min_nights` (2), and the latest night is <= `max_lag_days` (10) old. Emits `OUTAGE` with
`night_lights_ratio` (latest), `worst_ratio`, `baseline_radiance`, `n_dropped_nights`, gap "imagery lag: N
days" (+ excluded nights, short baseline). Score = (1 - worst_ratio) * min(1, run / (2 min_nights));
coverage confidence = min(1, clean nights / baseline_nights) * mean valid_frac. Window = first dropped
night 00:00Z to latest night + 1 d (stable id while the run start is unchanged). Cloudy or unlit cells
produce nothing (the "gap only" case has no Evidence to attach to). Known limits: a permanent change keeps
alerting; snow, moonlight and lunar-phase effects are handled only by upstream pixel masking. Priors, not
backtested (2021 Myanmar / Baltic control).

## Fusion v0 (fuse/fusion.py) and Admiralty (fuse/admiralty.py)
1. **Persistence gate** (set by the pipeline). Windows are 30 min at a 5-min step, so counting
   triggered windows would make one short burst "persist" for 6 windows. Per subject
   (key `aoi|kind|cell:<h3>`, or `aoi|mil_orbit|hex:<icao>` so orbits follow the aircraft):
   ticks = 1 + ticks of the previous step (if the subject triggered at end − step ± step/2, else 0);
   run_start = min(anomaly_first_ts, previous run_start);
   persistence = min(ticks, 1 + ⌊(anomaly_last_ts − run_start) / step⌋).
   Both are stored as metrics (`persistence_ticks`, `persistence_run_start`) keyed by the subject.
   A single event shorter than one step can never reach 2; a sustained one reaches 2 on its
   second tick. Alert only if persistence ≥ `fusion.persistence_windows[kind]` (2).
2. Group gated Evidence by (kind, cell). Merge into the live alert for that kind+cell — **any
   status except dismissed/expired** (new, acknowledged, under_review, shelved, confirmed; ADR-003)
   — updating only evidence-derived fields (severity [never downgraded], confidence, admiralty,
   summary, evidence_ids, corroborations, corroboration_status, last_seen, lat/lon). Otherwise a
   new alert, id = sha1(alert|aoi|kind|cell|first_seen). The pipeline re-reads the stored alert
   immediately before upsert (`apply_evidence_update`) and never writes `status`, except
   NEW → EXPIRED on staleness.
3. Severity from s = lead.score × lead.coverage.confidence: ≥ 0.6 high, ≥ 0.35 medium,
   ≥ 0.15 low, else info. **CRITICAL composite**: ≥ 2 distinct tanker (`tanker_types`) orbit
   Evidence (gated) within 100 km of a gated jamming Evidence → `composite` alert in the jam cell.
4. **Control AOIs** (role=control, e.g. Baltic) produce Evidence and alerts, severity capped at info.
5. Corroborations come from `fuse/corroborate.py::cross_source(gated, context, notices, cfg)`
   (ADR-005 D; `fusion.corroboration`: proximity_km 50, jam_ais_km 100, jam_orbit_km 100, sar_km 10,
   time_slack_h 2). **supports**: jamming/GNSS spoofing/mil orbit <- active notice of category
   military_exercise|live_firing|missile_rocket|gnss_interference covering the cell (or within
   proximity) overlapping the window +- slack; jamming <-> AIS spoofing within jam_ais_km; mil orbit
   <- gated jamming within jam_orbit_km (same ADS-B feed); AIS gap <- GFW gap of the same MMSI
   overlapping the gap, or a SAR detection within sar_km of the last fix during the gap.
   **context**: GFW loitering/encounter of the same MMSI; other-kind gated Evidence in the same cell.
   Notes use only `attrs.ref`, source and category. An absent notice is never `contradicts`.
   **Families**: `fusion.source_family` maps sources to adsb|ais|gfw|sar|notice; an Evidence's
   families are those of its record sources, a support's those of its records/notice source.
   "corroborated" iff >= 2 families in total (own + supporting); else "uncorroborated".
   WARNING_AREA Evidence is never gated: it cannot open an alert.
6. **Admiralty reliability = best grade among providers whose own reports show the anomaly** —
   the `source` of the Evidence `records` (detectors emit records only for anomaly-showing
   providers; `coverage.providers`, who merely covered the area, are ignored). Grades from
   sources.yaml (provider 'adsb.lol' → key 'adsb_lol'; unknown or none → F). Rationale:
   aggregators mirror the same broadcast, so the finding is as reliable as the best source that
   independently shows it; a weaker extra mirror must not downgrade it. **Credibility counts independent source families, not domains** (jamming and a tanker
   orbit are both `air` but one ADS-B feed; a NOTAM is independent): 1 analyst-confirmed or
   ≥ 3 families; 2 two families; 3 one family with lead score ≥ `strong_score` (0.6) and
   persistent, **or** one family with same-family support (tanker orbit near jamming); 4 one
   family otherwise; 5 contradicted; 6 no evidence. `grade(..., families=None)` keeps the legacy
   domain count.
7. Summary is a template over Evidence features (no LLM). `recommended_actions` = playbook ids
   whose `applies_to` contains the kind or "*".
8. **Dismissed episodes (anti-chattering, ISA-18.2).** A DISMISSED alert owns its episode: while
   gated Evidence for the same kind+cell+AOI keeps arriving within `expire_after_min` (60 min, the
   clear gap) of the dismissed alert's last_seen, the Evidence is attached to the dismissed alert
   (evidence_ids and last_seen only; status, severity, summary untouched) and no new alert opens.
   A new alert opens only after the anomaly has been absent for >= the clear gap and re-triggers,
   **or** immediately if the new Evidence's severity is higher than the dismissed alert's
   (escalation is never suppressed), **or** once the episode is older than
   `dismiss_max_episode_h` (12 h, measured from the dismissed alert's first_seen to the new
   Evidence's window_end) — so a same-severity campaign re-alerts at most every 12 h instead of
   being silenced forever by one dismissal.
   *Dismissal race*: if the analyst dismisses the alert between `fuse` and the pipeline's re-read,
   `apply_evidence_update` keeps the dismissal (evidence_ids/last_seen only) unless the update's
   severity is higher; then it returns a **new** NEW alert (id = sha1(alert|aoi|kind|cell|
   update.last_seen), first_seen = update.last_seen, evidence = ids the dismissed alert had not
   seen), matching the escalation rule above.
9. NEW alerts without fresh Evidence for `expire_after_min` (60) → EXPIRED. At upsert the
   pipeline re-reads the alert and expires it only if the stored copy is still NEW, its last_seen
   is < now − expire_after_min and not newer than the snapshot fusion saw (a refresh between
   list and upsert wins); expiry never copies stale evidence fields. Alerts in an analyst
   status never expire automatically.
10. **Sources and footprint (014c, ADR-006).** `Alert.sources` has one `AlertSource` per connector
    (source key, e.g. `adsb_lol`) whose own records show the anomaly, plus the connectors behind
    every *supporting* corroboration (contradicting sources are not listed). `family` is
    `family_of(connector)`; `admiralty` = (connector reliability from sources.yaml, F if unknown)
    x (the alert's overall credibility, which is a property of the whole alert). Sorted by
    reliability then name. `Alert.footprint` is `single_track` (description `Track <id>`) when the
    kind is one-object (ais_spoofing, ais_gap, mil_orbit) and the group names exactly one entity;
    otherwise `area_cell` with the H3 cell, its resolution and diameter (2 x mean hexagon edge,
    ~52 km at r4). `sources` and `footprint` are refreshed with the other evidence fields.
11. **Alert.ref** `HMD-YYYYMMDD-NNNN` is store-owned, not fusion-owned: the Store assigns it on the
    first put of an `Alert.id` (day = UTC day of `first_seen`, NNNN = next per-day number) and
    keeps it on every later put. A well-formed incoming ref on a new id is honoured (lake
    rebuild) and the day counter is bumped past it. DuckDB keeps a `ref` column and an
    `alert_ref_day` counter table, so numbering survives restarts; Composite delegates to its
    primary store; Replay inherits MemoryStore, so replay order fixes the refs.

## Pipeline (pipeline.py)
`tick(store, settings, now)`: one `aircraft_window` pull covering the longest window, one
`vessels_window` (24 h) and one `events_window` (`notice_lookback_days` 30, notices filtered to
active); AIS gaps are found **once per tick** (per distinct `ais_gap` config) on the unfiltered
vessel window and each gap is assigned to exactly one AOI: the nearest centre whose radius contains
the ship's last fix (`_assign_gaps`), so overlapping AOIs never double-report; per AOI
(radius filter) run jamming (with `active` cells from `evidence_history`), spoofing, orbit (with
the jamming Evidence), AIS spoofing, AIS gap (last fix within the AOI radius) and warning areas;
set `aoi` + persistence (subject `aoi|ais_gap|mmsi:<mmsi>`, `aoi|ais_spoofing|mmsi:<mmsi>` for the
per-vessel A1/A4, per cell for A2/A3); record `ais_vessels` (distinct MMSIs) per r4 cell; record `jam_ratio` per cell with total ≥
`min_aircraft` and `orbit_active` per orbit cell; fuse against live alerts; upsert Evidence and
alerts. `record_metric` is an upsert on (key, metric, ts), so re-running the same tick is
idempotent (same ids, metrics not duplicated, persistence not inflated).

## Backtest (heimdall/backtest, task 006)
`python -m heimdall.backtest --aoi A --date D --traces DIR [--gpsjam CSV] [--refs FILE]` ->
`reports/backtest-A-D.md`. Offline and deterministic (no wall clock; sorted output).
* **Traces** (`traces.py`): readsb `globe_history` per-aircraft JSON (gzip by magic bytes),
  rows `[dt, lat, lon, alt|"ground", gs, track, flags, vrate, detail, type, alt_geom, geom_rate]`;
  flags&8 = alt is geometric, flags&4 = vrate geometric. NIC/NACp only from the row's `detail`
  object; rows without it are *unknown* (no forward fill). src_type = row[9] | detail.type | other.
  Provider `history` (-> sources key adsb_lol_history).
* **Replay** (`replay.py`): obs in the AOI radius -> `ReplayStore` (MemoryStore + bisect time index,
  metric upsert) -> `pipeline.tick` every 5 min over the UTC day. `raised_at[alert]` = first tick
  at which the alert existed.
* **Scoring** (`score.py`): GPSJam day CSV (hex, count_good_aircraft, count_bad_aircraft; finer
  cells summed to r4). R_g = max(0, bad-1)/(good+bad); red > 0.10, yellow (0.02, 0.10], green
  else, gray if good+bad < min. Per r4 cell in the AOI our day = alert | evidence_only | quiet |
  not_observed. Binary scores with positive prediction = alert: *red* (truth red) and *yellow+*
  (truth yellow or red); gray / no-GPSJam cells excluded. P, R, F1 (undefined -> none).
* **Sweep**: ratio_alert {0.05, 0.08, 0.10, 0.15} x min_aircraft {3, 5, 8}; ratio_clear scaled by
  the configured ratio_clear/ratio_alert. Recommendation = max of (F1 red, P red, F1 yellow+,
  ratio_alert, min_aircraft) (ties -> more conservative); "keep" unless it beats the current
  config on the first three. The report also replays the sparse profile below.
* **Lead time**: per reference time, first matching alert time (raised_at, else first_seen) in
  [ref - 24 h, ref + 6 h]; lead = ref - alert time (> 0 = early).
* **Sparse-region profile (proposed, not applied)**: per-AOI `detectors: {jamming: {window_min:
  60, min_aircraft: 3, confidence_cap: 0.5}}` for SE Asia AOIs.

## Calibration status
All thresholds are literature/GPSJam priors, **not yet backtested** (Baltic control run pending
in P1). Most sensitive: `ratio_alert`/`mad_floor` (jamming), `speed_diff_kt` (spoofing S1 on
stale gs), `cluster_*` (holding stacks), `novelty_scale_h`.

## Plain-language text (013a)
Everything shown on screen from Evidence (counterfactual, rule descriptions, gaps) avoids jargon
(ratio, z-score, MAD, NIC/NACp, H3, sigma); the numbers stay in `features`/`RuleResult` values. Example
(jamming): "Clears when 2 or fewer of the 12 aircraft lose GPS accuracy (alert level 10%, now 25%)".
Detector versions bumped for text: jamming-1.1.1, spoofing-1.0.2, orbit-1.0.1. `fuse/fusion.py`
alert summaries still use R'/H3 wording (not in this task's scope).


## Hormuz replay (task 014d)

Pipeline: `scripts/fetch_adsblol_day.sh DATE` -> `scripts/filter_adsblol.py` (bbox prefilter 22.9-29.7N,
52.5-60.1E, rows kept verbatim incl. `alt_baro`, NIC, NACp) -> `data/replay/hormuz/DATE.parquet` ->
`python -m heimdall.backtest --aoi hormuz --date DATE --input <parquet> --no-sweep` (real jamming/
spoofing/orbit detectors, unchanged thresholds; writes `<stem>.alerts.jsonl`, `<stem>.evidence.jsonl`
and a replay-local lake) -> `python -m heimdall.api.demo --replay <parquet>` (alerts keep historical
timestamps; tracks are time-shifted onto the wall clock and loop). No math added; thresholds untouched.

**Date: UNVERIFIED.** Candidate 2025-06-17 (Israel-Iran conflict window, 13-24 Jun 2025). This session's
sandbox blocked the archive host and web access, so no public jamming report could be cited and the
release naming was not checked. Verify a source before treating the day as ground truth; if none, pick
another date. Data: adsb.lol, ODbL 1.0 ("Contains information from adsb.lol, made available under the ODbL").
