"""P7 052: cross-band fusion - L1 jamming <-> other-band anomaly, experimental-band cap, +1 step."""

from __future__ import annotations

from heimdall.fuse.fusion import fuse
from heimdall.schemas import AlertKind, Band, Severity
from tests.unit.test_fusion import GRADES, NOW, PB, ev


def _jam(**kw: object) -> object:
    return ev(score=0.6, conf=0.8, ident="jam", **kw).model_copy(update={"bands": [Band.L1_GNSS]})  # type: ignore[arg-type]


def _rfi(lat: float = 20.5, lon: float = 96.5, ident: str = "rfi") -> object:
    return ev(
        kind=AlertKind.SPECTRUM_ANOMALY,
        cell="8464991ffffffff",
        persistence=3,
        score=1.0,
        conf=0.9,
        lat=lat,
        lon=lon,
        ident=ident,
        showing=("smap_rfi",),
        covering=("smap_rfi",),
    ).model_copy(update={"bands": [Band.L_1400]})


def test_experimental_band_alone_is_capped_at_medium() -> None:
    (a,) = fuse([_rfi()], [], NOW, None, PB, GRADES)  # type: ignore[list-item]
    assert a.severity == Severity.MEDIUM


def test_jamming_with_another_band_nearby_steps_up_and_says_why() -> None:
    (alone,) = fuse([_jam()], [], NOW, None, PB, GRADES)  # type: ignore[list-item]
    assert alone.severity == Severity.MEDIUM
    out = {a.kind: a for a in fuse([_jam(), _rfi()], [], NOW, None, PB, GRADES)}  # type: ignore[list-item]
    jam, rfi = out[AlertKind.GNSS_JAMMING], out[AlertKind.SPECTRUM_ANOMALY]
    assert jam.severity == Severity.HIGH
    assert "another band (l_1400)" in jam.summary
    assert any("another band" in c.note for c in jam.corroborations)
    # the anomaly is no longer alone: supported on GPS L1, so not held at the experimental cap
    assert rfi.severity == Severity.HIGH and "gps L1" not in rfi.summary


def test_far_apart_bands_do_not_corroborate() -> None:
    out = {a.kind: a for a in fuse([_jam(), _rfi(lat=40.0, lon=130.0)], [], NOW, None, PB, GRADES)}  # type: ignore[list-item]
    assert out[AlertKind.GNSS_JAMMING].severity == Severity.MEDIUM
    assert out[AlertKind.SPECTRUM_ANOMALY].severity == Severity.MEDIUM
