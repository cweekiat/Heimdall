"""NGA Maritime Safety Information broadcast warnings (HYDROPAC / NAVAREA) -> area GeoEvents.

Notice text is untrusted data (see notice_parse). The MSI API was under maintenance at task 005: a
non-JSON body maps to status=down / "source maintenance" and triggers backoff, never a crash.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from heimdall.config import Settings
from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.notice_base import NoticeConnector, SourceMaintenance, load_warnings_cfg
from heimdall.ingest.notice_parse import (
    Validity,
    build_notice_event,
    make_ref,
    parse_dtg,
    parse_validity,
    sanitize,
)
from heimdall.schemas import Domain, GeoEvent, SourceReliability


class NgaMsiConnector(NoticeConnector):
    def __init__(
        self,
        base_url: str,
        navareas: Mapping[str, str],
        rules: Mapping[str, list[str]],
        *,
        client: RateLimitedClient | None = None,
        rps: float = 0.2,
        reliability: SourceReliability = SourceReliability.A,
        expected_latency_s: int = 3600,
        poll_interval_s: float = 3600.0,
        enabled: bool = True,
        max_per_area: int = 200,
        **kw: Any,
    ) -> None:
        super().__init__(
            "nga_msi",
            Domain.SEA,
            reliability,
            expected_latency_s,
            client=client or RateLimitedClient("nga_msi", rps=rps),
            poll_interval_s=poll_interval_s,
            enabled=enabled,
            **kw,
        )
        self.base_url = base_url.rstrip("/")
        self.navareas = dict(navareas)
        self.rules = rules
        self.max_per_area = max_per_area

    async def _fetch(self) -> list[GeoEvent]:
        out: list[GeoEvent] = []
        for area in self.navareas:
            resp = await self.client.get(
                f"{self.base_url}/publications/broadcast-warn",
                params={"output": "json", "navArea": area, "status": "active"},
            )
            try:
                payload = resp.json()
            except ValueError as e:  # HTML maintenance/error page
                raise SourceMaintenance from e
            rows = payload.get("broadcast-warn") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise SourceMaintenance
            dropped = 0
            for row in rows[: self.max_per_area]:
                ev = self.parse_row(row, area)
                if ev is None:
                    dropped += 1
                else:
                    out.append(ev)
            self.note_dropped(dropped)
        return out

    def parse_row(self, row: Any, area: str) -> GeoEvent | None:
        if not isinstance(row, dict):
            return None
        try:
            num, year = int(row["msgNumber"]), int(row["msgYear"])
        except (KeyError, TypeError, ValueError):
            return None
        text = str(row.get("text") or "")
        issue = parse_dtg(str(row.get("issueDate") or ""), datetime.now(UTC))
        if issue is None:
            return None
        name = self.navareas.get(area, area)
        v = parse_validity(text, issue)
        cancel_dt = parse_dtg(str(row.get("cancelDate") or ""), issue)
        cancelled = (
            str(row.get("status") or "").upper().startswith("C") or bool(cancel_dt) or v.cancelled
        )
        valid_to = cancel_dt or v.valid_to
        validity = Validity(v.valid_from or issue, valid_to, cancelled)
        return build_notice_event(
            id=f"nga_msi:{area}:{year}:{num}",
            source="nga_msi",
            domain=Domain.SEA,
            kind="nav_warning",
            ts=issue,
            raw_text=text,
            title=sanitize(f"{name} {num}/{str(year)[-2:]} {row.get('subregion') or ''}"),
            ref=make_ref(name, f"{num}/{str(year)[-2:]}"),
            rules=self.rules,
            validity=validity,
            status="cancelled" if cancelled else "active",
            attrs={"nav_area": make_ref(area), "authority": make_ref(row.get("authority"))},
        )


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> NgaMsiConnector:
    w = load_warnings_cfg()
    nga = w.get("nga_msi", {})
    rps = float(cfg.get("rps", 0.2))
    return NgaMsiConnector(
        cfg.get("base", "https://msi.nga.mil/api"),
        nga.get("navareas", {"P": "HYDROPAC", "11": "NAVAREA XI"}),
        w.get("categories", {}),
        client=RateLimitedClient(key, rps=rps),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 3600)),
        enabled=cfg.get("enabled", True) is not False,
        max_per_area=int(nga.get("max_per_area", 200)),
    )
