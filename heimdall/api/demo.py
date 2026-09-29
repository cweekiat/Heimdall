"""Seed a MemoryStore with synthetic alerts and serve the API: `python -m heimdall.api.demo`."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import h3

from heimdall.adapters.memory_store import MemoryStore
from heimdall.schemas import (
    Admiralty,
    AircraftObs,
    Alert,
    AlertKind,
    Corroboration,
    Coverage,
    Domain,
    Evidence,
    RecordRef,
    RuleResult,
    Severity,
    SourceHealth,
    SourceReliability,
)

# (aoi, lat, lon, kind, severity, ratio, baseline, confidence)
SEEDS = [
    ("myanmar", 20.9, 96.1, AlertKind.GNSS_JAMMING, Severity.HIGH, 0.34, 0.02, 0.82),
    ("myanmar", 19.2, 97.4, AlertKind.GNSS_SPOOFING, Severity.MEDIUM, 0.12, 0.01, 0.55),
    ("south_china_sea", 12.9, 114.2, AlertKind.GNSS_JAMMING, Severity.CRITICAL, 0.58, 0.03, 0.9),
    ("south_china_sea", 11.4, 112.6, AlertKind.MIL_ORBIT, Severity.MEDIUM, 0.0, 0.0, 0.62),
    ("taiwan_luzon", 21.8, 120.9, AlertKind.MIL_ORBIT, Severity.HIGH, 0.0, 0.0, 0.7),
    ("taiwan_luzon", 23.3, 119.8, AlertKind.GNSS_JAMMING, Severity.LOW, 0.07, 0.03, 0.4),
    ("malacca_sg", 2.9, 101.3, AlertKind.GNSS_JAMMING, Severity.LOW, 0.05, 0.04, 0.35),
    ("baltic", 55.1, 20.9, AlertKind.GNSS_JAMMING, Severity.HIGH, 0.41, 0.05, 0.88),
]


def seed(store: MemoryStore, seed_value: int = 7) -> MemoryStore:
    rnd = random.Random(seed_value)
    now = datetime.now(UTC)
    for i, (aoi, lat, lon, kind, sev, ratio, base, conf) in enumerate(SEEDS):
        cell = h3.latlng_to_cell(lat, lon, 4)
        ev_id = f"ev-{i}"
        is_jam = kind in (AlertKind.GNSS_JAMMING, AlertKind.GNSS_SPOOFING)
        rules = (
            [
                RuleResult(
                    rule="degraded_ratio",
                    description="Share of true-ADS-B aircraft with NIC<=4 or NACp<=5",
                    value=ratio,
                    threshold=0.10,
                    op=">=",
                    passed=ratio >= 0.10,
                    baseline=base,
                ),
                RuleResult(
                    rule="min_quality_aircraft",
                    description="Enough trustworthy aircraft in the cell",
                    value=float(8 + i),
                    threshold=5,
                    op=">=",
                    passed=True,
                ),
                RuleResult(
                    rule="persistence",
                    description="Anomaly held for consecutive windows",
                    value=3,
                    threshold=2,
                    op=">=",
                    passed=True,
                ),
            ]
            if is_jam
            else [
                RuleResult(
                    rule="orbit_turns",
                    description="Cumulative heading change over window (deg)",
                    value=1080.0,
                    threshold=720.0,
                    op=">=",
                    passed=True,
                    baseline=90.0,
                ),
                RuleResult(
                    rule="mil_flag",
                    description="ICAO type in military DB",
                    value=1,
                    threshold=1,
                    op="==",
                    passed=True,
                ),
            ]
        )
        ev = Evidence(
            id=ev_id,
            kind=kind,
            detector="jamming" if is_jam else "orbit",
            detector_version="0.1",
            window_start=now - timedelta(minutes=30 + i * 7),
            window_end=now - timedelta(minutes=i * 7),
            aoi=aoi,
            h3_cell=cell,
            h3_res=4,
            lat=lat,
            lon=lon,
            score=conf,
            rules=rules,
            features={"degraded_ratio": ratio, "baseline_ratio": base},
            records=[RecordRef(source="adsb.lol", id=f"3c6{i}a1@{now:%H%M}")],
            entities=[f"7c6b{i:02x}", f"71be{i:02x}"] if not is_jam else [f"48a1{i:02x}"],
            counterfactual=(
                f"If the degraded ratio were below 10% (now {ratio:.0%}) this alert would not fire."
                if is_jam
                else "With fewer than 2 full turns this would be a normal holding pattern."
            ),
            coverage=Coverage(
                n_observed=12 + i,
                n_quality=8 + i,
                providers=["adsb.lol", "adsb.fi"],
                confidence=conf,
            ),
            gaps=["Only 2 of 3 aggregators reporting in this cell"] if i % 3 == 0 else [],
            persistence=3,
            created_at=now,
        )
        store.upsert_evidence(ev)
        corr = [
            Corroboration(
                domain=Domain.SEA,
                evidence_id="ais-ctx",
                relation="supports",
                note="AIS position jumps observed in the same cell",
            )
        ]
        store.upsert_alert(
            Alert(
                id=f"alert-{i}",
                kind=kind,
                severity=sev,
                confidence=conf,
                admiralty=Admiralty(
                    reliability=SourceReliability.B if conf > 0.6 else SourceReliability.C,
                    credibility=2 if conf > 0.6 else 3,
                ),
                title=f"{kind.value.replace('_', ' ').title()} near {aoi}",
                summary=f"{kind.value} evidence in H3 {cell}: see rules table.",
                aoi=aoi,
                h3_cell=cell,
                lat=lat,
                lon=lon,
                evidence_ids=[ev_id],
                corroborations=corr if i % 2 == 0 else [],
                corroboration_status="corroborated" if i % 2 == 0 else "uncorroborated",
                recommended_actions=["watch_cell", "export_brief"],
                first_seen=now - timedelta(hours=2, minutes=i * 9),
                last_seen=now - timedelta(minutes=i * 7),
            )
        )
        # metric history for the mini chart
        for h in range(24):
            ts = now - timedelta(hours=24 - h)
            store.record_metric(cell, "degraded_ratio", ts, max(0.0, base + rnd.random() * 0.02))
        store.record_metric(cell, "degraded_ratio", now, ratio)
    # an orbit track
    for k in range(60):
        a = k / 60 * 6 * math.pi
        store.insert_aircraft(
            [
                AircraftObs(
                    ts=now - timedelta(minutes=60 - k),
                    hex="7c6b00",
                    provider="adsb.lol",
                    lat=11.4 + 0.25 * math.sin(a),
                    lon=112.6 + 0.25 * math.cos(a),
                    src_type="adsb_icao",
                    alt_baro=28000,
                    gs=380,
                )
            ]
        )
    for src, dom, rel, err in [
        ("adsb.lol", Domain.AIR, SourceReliability.B, 0.01),
        ("adsb.fi", Domain.AIR, SourceReliability.B, 0.03),
        ("airplanes.live", Domain.AIR, SourceReliability.B, 0.0),
        ("aisstream", Domain.SEA, SourceReliability.C, 0.12),
    ]:
        store.upsert_health(
            SourceHealth(
                source=src,
                domain=dom,
                reliability=rel,
                last_ok=now - timedelta(seconds=40),
                records_last_hour=int(rnd.random() * 5000) + 1000,
                error_rate=err,
                expected_latency_s=60,
                last_error="HTTP 429" if err > 0.05 else None,
                last_error_at=now - timedelta(minutes=9) if err > 0.05 else None,
            )
        )
    return store


def main() -> None:  # pragma: no cover
    import uvicorn

    from heimdall.api.app import create_app

    uvicorn.run(create_app(seed(MemoryStore())), host="127.0.0.1", port=8000)


if __name__ == "__main__":  # pragma: no cover
    main()
