# Detectors (math as implemented — detection-scientist keeps this current)

## GNSS jamming (detect/jamming.py)
Scope: true ADS-B only (`src_type in {adsb_icao, adsb_icao_nt}`). Cell = H3 r4, window 30 min, step 5 min.
Aircraft *a* in cell *h* is **bad** if ≥ `bad_dwell_frac` of its reports have NIC < 7 or NACp < 8.
R' = max(0, bad − 1) / total (GPSJam de-noise). Trigger when total ≥ 5 AND R' ≥ 0.10 AND
robust z = (R' − median_14d) / (1.4826·MAD_14d + ε) ≥ 3 (or no baseline yet and R' ≥ 0.10 → flagged "no baseline").
Persistence ≥ 2 consecutive windows; clears below 0.06 (hysteresis).
Emitter estimate: centroid + covariance ellipse (2σ) of each bad aircraft's first-degraded position.
Coverage confidence = min(1, total/15) × provider agreement.

## GNSS spoofing (detect/spoofing.py)
Per aircraft consecutive true-ADS-B fixes with Δt ≥ 5 s: S1 teleport (v_calc > Mach 2 or |v_calc − gs| > 150 kt persisting ≥ 2 pairs),
S2 |alt_geom − alt_baro| > 1000 ft beyond cell median, S3 MLAT vs ADS-B gap > 5 km, S4 ≥ 3 aircraft reporting within 2 km of the same point
while their tracks disagree, S5 NIC ≥ 7 while S1/S3 fire. Alert if ≥ 2 signals, or S3 alone.

## Military orbit (detect/orbit.py)
Aircraft with `mil` flag or type in mil list. Window 75 min, ≥ 20 points. Orbit if circular variance ≥ 0.5 AND cumulative turn ≥ 360° AND D ≤ 0.25.
Novelty vs 14-day orbit baseline of the r4 cell. CRITICAL composite: ≥ 2 tankers in orbit within 100 km of an active jamming Evidence.
