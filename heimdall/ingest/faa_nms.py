"""FAA NOTAM Management Service (NMS) -> area GeoEvents (domain air, kind notam).

Keyed (ADR-004): OAuth client credentials from FAA_NMS_KEY + FAA_NMS_SECRET, read once at
construction. Either absent -> status needs_key, no request is ever made. Endpoint paths are
configurable (config/warnings.yaml) because FAA access is by approval and paths are unverified.
NOTAM text is untrusted data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from heimdall.config import Settings, secret
from heimdall.ingest.http import RateLimitedClient
from heimdall.ingest.notice_base import NoticeConnector, Unauthorized, load_warnings_cfg
from heimdall.ingest.notice_parse import (
    Validity,
    build_notice_event,
    make_ref,
    parse_qline,
    parse_shape,
    parse_validity,
    sanitize,
)
from heimdall.schemas import Domain, GeoEvent, SourceReliability

_CANCEL_RE = re.compile(r"\bNOTAMC\b|\bCANCELL?ED\b", re.I)
_FIR_OK = re.compile(r"^[A-Z]{4}$")


def _iso(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


class FaaNmsConnector(NoticeConnector):
    def __init__(
        self,
        base_url: str,
        firs: list[str],
        rules: Mapping[str, list[str]],
        *,
        key: str | None,
        secret: str | None,
        token_path: str = "/v1/auth/token",
        notam_path: str = "/nmsapi/v1/notams",
        client: RateLimitedClient | None = None,
        rps: float = 0.5,
        reliability: SourceReliability = SourceReliability.A,
        expected_latency_s: int = 300,
        poll_interval_s: float = 600.0,
        enabled: bool = True,
        **kw: Any,
    ) -> None:
        super().__init__(
            "faa_nms_notam",
            Domain.AIR,
            reliability,
            expected_latency_s,
            client=client or RateLimitedClient("faa_nms_notam", rps=rps),
            poll_interval_s=poll_interval_s,
            enabled=enabled,
            **kw,
        )
        self.base_url = base_url.rstrip("/")
        self.firs = [f for f in dict.fromkeys(firs) if _FIR_OK.match(f)]
        self.rules = rules
        self.token_path = token_path
        self.notam_path = notam_path
        self._key, self._secret = key or "", secret or ""
        self._token: str | None = None
        if enabled and not (self._key and self._secret):
            self.status, self.detail = "needs_key", "set FAA_NMS_KEY and FAA_NMS_SECRET"

    async def _auth(self) -> str:
        if self._token:
            return self._token
        await self.client.bucket.acquire()
        # RateLimitedClient only wraps GET; the token call is a single POST through its client.
        resp = await self.client._client.post(
            f"{self.base_url}{self.token_path}",
            data={"grant_type": "client_credentials"},
            auth=(self._key, self._secret),
        )
        if resp.status_code in (400, 401, 403):
            raise Unauthorized
        resp.raise_for_status()
        tok = resp.json().get("access_token")
        if not isinstance(tok, str) or not tok:
            raise Unauthorized
        self._token = tok
        return tok

    async def _fetch(self) -> list[GeoEvent]:
        out: list[GeoEvent] = []
        token = await self._auth()
        for fir in self.firs:
            try:
                resp = await self.client.get(
                    f"{self.base_url}{self.notam_path}",
                    params={"icaoLocation": fir},
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    self._token = None  # expired: re-auth next poll
                raise
            payload = resp.json()
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise ValueError("unexpected NOTAM payload")
            dropped = 0
            for item in items:
                ev = self.parse_item(item, fir)
                if ev is None:
                    dropped += 1
                else:
                    out.append(ev)
            self.note_dropped(dropped)
        return out

    def parse_item(self, item: Any, fir: str) -> GeoEvent | None:
        try:
            n = item["properties"]["coreNOTAMData"]["notam"]
            nid = str(n["id"])
        except (KeyError, TypeError):
            return None
        if not isinstance(n, dict) or not nid:
            return None
        icao = str(n.get("icaoMessage") or n.get("text") or "")
        eff_to_raw = n.get("effectiveEnd")
        text_v = parse_validity(icao)
        perm = isinstance(eff_to_raw, str) and eff_to_raw.upper().startswith("PERM")
        issued = _iso(n.get("issued")) or text_v.valid_from or datetime.now(UTC)
        valid_from = _iso(n.get("effectiveStart")) or text_v.valid_from
        valid_to = None if perm else (_iso(eff_to_raw) or text_v.valid_to)
        cancelled = bool(_CANCEL_RE.search(icao)) or str(n.get("type") or "").upper() == "C"
        q = parse_qline(icao)
        geometry, ps = parse_shape(icao)
        attrs: dict[str, Any] = {
            "fir": make_ref(n.get("icaoLocation") or fir),
            "permanent": perm or text_v.permanent,
        }
        if q:
            attrs["alt_lower_ft"], attrs["alt_upper_ft"] = q[3], q[4]
        number = str(n.get("number") or nid)
        return build_notice_event(
            id=f"faa_nms:{make_ref(nid).replace(' ', '')}",
            source="faa_nms_notam",
            domain=Domain.AIR,
            kind="notam",
            ts=issued,
            raw_text=icao,
            title=sanitize(f"NOTAM {number} {n.get('icaoLocation') or fir}"),
            ref=make_ref("NOTAM", number),
            rules=self.rules,
            validity=Validity(valid_from, valid_to, cancelled, perm),
            status="cancelled" if cancelled else "active",
            attrs=attrs,
            geometry=geometry,
            parse_status=ps,
        )


def build(key: str, cfg: Mapping[str, Any], settings: Settings) -> FaaNmsConnector:
    w = load_warnings_cfg()
    f = w.get("faa_nms", {})
    firs: list[str] = []
    by_aoi: Mapping[str, list[str]] = f.get("firs", {})
    for _aoi, lst in by_aoi.items():
        firs.extend(lst)
    rps = float(cfg.get("rps", 0.5))
    return FaaNmsConnector(
        cfg.get("base", "https://nms.aim.faa.gov"),
        firs,
        w.get("categories", {}),
        key=secret("FAA_NMS_KEY"),
        secret=secret("FAA_NMS_SECRET"),
        token_path=f.get("token_path", "/v1/auth/token"),
        notam_path=f.get("notam_path", "/nmsapi/v1/notams"),
        client=RateLimitedClient(key, rps=rps),
        reliability=SourceReliability(cfg.get("reliability", "A")),
        expected_latency_s=int(cfg.get("latency_s", 300)),
        enabled=cfg.get("enabled", True) is not False,
    )
