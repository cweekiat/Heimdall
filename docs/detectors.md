# Detectors (math as implemented — detection-scientist keeps this current)

All detectors are pure: `detect(obs, cfg, now, baseline) -> list[Evidence]`, where
`baseline(key, metric) -> list[float]` is supplied by the pipeline. Windows are aligned: the window
end is `now` floored to the `step_min` grid (from UTC midnight), start = end − `window_min`,
interval `[start, end)`. Evidence ids are `sha1(kind|aoi|cell|entity|window_start ISO)` (entity = ""
for cell-level jamming); detectors take a keyword `aoi`. Every Evidence also carries
`anomaly_first_ts`/`anomaly_last_ts` (epoch s) = time span of the observations that constitute the
anomaly, used for persistence. Every threshold below is a key in `config/config.yaml`; defaults are in
each module's `DEFAULTS`. Shared helpers: `heimdall/detect/common.py`, `heimdall/geo.py`.

## GNSS jamming (detect/jamming.py, `jamming-1.1.0`)
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

## GNSS spoofing (detect/spoofing.py, `spoofing-1.0.1`)
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
* Records: first and last fix of each provider with ≥ min_points/2 raw fixes of the track (else
  the providers of the de-duplicated track fixes only). Anomaly span = first..last resampled fix.
* `jam_evidence`: distance from centroid to nearest jamming Evidence → features `jam_distance_km`,
  `near_jamming` (≤ `jam_proximity_km` 100) and informational rule `near_jamming`.

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
5. Corroborations: other-kind gated Evidence in the same cell = `context`. Status "corroborated"
   only when a *different domain* supports; otherwise "uncorroborated" (never "false").
6. **Admiralty reliability = best grade among providers whose own reports show the anomaly** —
   the `source` of the Evidence `records` (detectors emit records only for anomaly-showing
   providers; `coverage.providers`, who merely covered the area, are ignored). Grades from
   sources.yaml (provider 'adsb.lol' → key 'adsb_lol'; unknown or none → F). Rationale:
   aggregators mirror the same broadcast, so the finding is as reliable as the best source that
   independently shows it; a weaker extra mirror must not downgrade it. **Credibility**: 1 analyst-confirmed or ≥ 3 domains; 2 two domains; 3 single
   domain with lead score ≥ `strong_score` (0.6) and persistent; 4 single domain otherwise;
   5 contradicted; 6 no evidence.
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

## Pipeline (pipeline.py)
`tick(store, settings, now)`: one `aircraft_window` pull covering the longest window; per AOI
(radius filter) run jamming (with `active` cells from `evidence_history`), spoofing, orbit (with
the jamming Evidence); set `aoi` + persistence; record `jam_ratio` per cell with total ≥
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
