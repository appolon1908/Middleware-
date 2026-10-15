"""Fail-closed HTTP client for the standalone Leads Workstation service."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


class LeadsWorkstationConfigurationError(RuntimeError):
    pass


class LeadsWorkstationUpstreamError(RuntimeError):
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Leads Workstation upstream returned HTTP {status_code}")


@dataclass(frozen=True)
class LeadsWorkstationConfig:
    base_url: str
    service_token: str
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "LeadsWorkstationConfig":
        base_url = os.getenv("LEADS_WORKSTATION_URL", "").strip()
        token = os.getenv("LEADS_WORKSTATION_SERVICE_TOKEN", "").strip()
        if not base_url or not token:
            raise LeadsWorkstationConfigurationError(
                "Leads Workstation URL and service token must be configured"
            )
        _validate_internal_origin(base_url)
        try:
            timeout = float(os.getenv("LEADS_WORKSTATION_TIMEOUT_SECONDS", "10"))
        except ValueError as exc:
            raise LeadsWorkstationConfigurationError(
                "invalid Leads Workstation timeout"
            ) from exc
        if timeout <= 0 or timeout > 60:
            raise LeadsWorkstationConfigurationError(
                "Leads Workstation timeout must be between 0 and 60 seconds"
            )
        return cls(base_url=base_url.rstrip("/"), service_token=token, timeout_seconds=timeout)


def _validate_internal_origin(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LeadsWorkstationConfigurationError("invalid Leads Workstation URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LeadsWorkstationConfigurationError(
            "Leads Workstation URL must not contain credentials, query, or fragment"
        )
    host = parsed.hostname
    is_internal = False
    try:
        address = ipaddress.ip_address(host)
        is_internal = address.is_private or address.is_loopback
    except ValueError:
        is_internal = (
            "." not in host
            or host.endswith(".internal")
            or host.endswith(".local")
            or host == "localhost"
        )
    if not is_internal:
        raise LeadsWorkstationConfigurationError(
            "Leads Workstation origin must be an internal host"
        )


class LeadsWorkstationClient:
    def __init__(
        self,
        config: LeadsWorkstationConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.http = httpx.AsyncClient(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.service_token}",
                "Accept": "application/json",
                "User-Agent": "Codestra-Middleware/Leads-Workstation",
            },
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            timeout=httpx.Timeout(config.timeout_seconds),
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str = "",
        request_id: str = "",
        correlation_id: str = "",
        causation_id: str = "",
        traceparent: str = "",
        if_match: str = "",
    ) -> httpx.Response:
        method = method.upper()
        if method not in {"GET", "HEAD"} and not idempotency_key:
            raise ValueError("Leads Workstation mutations require Idempotency-Key")
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if request_id:
            headers["X-Codestra-Request-ID"] = request_id
        if correlation_id:
            headers["X-Codestra-Correlation-ID"] = correlation_id
        if causation_id:
            headers["X-Codestra-Causation-ID"] = causation_id
        if traceparent:
            headers["traceparent"] = traceparent
        if if_match:
            headers["If-Match"] = if_match

        response = await self.http.request(
            method,
            path,
            json=payload if payload is not None else None,
            params=params,
            headers=headers,
        )
        if response.is_redirect:
            raise LeadsWorkstationUpstreamError(502, {"error": "redirect_rejected"})
        if response.status_code >= 500:
            raise LeadsWorkstationUpstreamError(
                502,
                {"error": "leads_workstation_unavailable"},
            )
        return response


async def get_leads_workstation_client():
    client = LeadsWorkstationClient(LeadsWorkstationConfig.from_env())
    try:
        yield client
    finally:
        await client.aclose()
