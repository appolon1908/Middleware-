from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator, FormatChecker

from .config import ConfigurationError, Settings
from .temporal_workflows import ActivityResult, CommandExecutionRequest


ROOT = Path(__file__).resolve().parents[1]
TELNEXA_SMS_COMMAND_SCHEMA = ROOT / "contracts" / "telnexa-sms-command.v1.schema.json"


class TelnexaProviderAdapterError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _telnexa_sms_command_validator() -> Draft202012Validator:
    """Load the local SMS specialization without resolving its remote base ref.

    ``CommandExecutionRequest`` already carries a validated platform envelope.
    This validator enforces the Telnexa-specific constants and payload shape.
    """

    try:
        source = json.loads(TELNEXA_SMS_COMMAND_SCHEMA.read_text(encoding="utf-8"))
        specialization = source["allOf"][1]
        local_schema = {
            **specialization,
            "$defs": source.get("$defs", {}),
        }
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError) as exc:
        raise TelnexaProviderAdapterError(
            "canonical Telnexa SMS command schema cannot be loaded"
        ) from exc
    Draft202012Validator.check_schema(local_schema)
    return Draft202012Validator(local_schema, format_checker=FormatChecker())


class TelnexaSmsAdapter:
    """Fail-closed transport from the durable command plane to Telnexa SMS.

    Middleware never speaks to Jasmin or an SMSC. It submits the reviewed
    command to the Telnexa gateway, which owns sender approval, consent and
    opt-out suppression, campaign approval, canary reservation and routing.

    A submission timeout is an unknown outcome, never a failure. Telnexa keys a
    stored submission on ``(tenant, Idempotency-Key)``, so the read-back is an
    identical replay: the gateway returns the message it already recorded
    instead of sending a second one, and rejects a body that does not hash to
    the stored request. A listing cannot serve as the read-back because the
    Telnexa message projection does not expose the idempotency key.
    """

    SUBMIT_SMS = "sms.message.submit.v1"
    SUPPORTED = {SUBMIT_SMS}
    MESSAGES_PATH = "/api/v1/messages"

    # Telnexa answers both a fresh submission and a deduplicated replay with a
    # 2xx carrying the stored message projection.
    ACCEPTED_STATUSES = frozenset({200, 202})
    IDEMPOTENCY_CONFLICT = "idempotency_key_payload_mismatch"

    # Mirrors the connector manifest's forbidden_payload_keys.
    FORBIDDEN_PAYLOAD_KEYS = frozenset(
        {
            "access_token",
            "client_secret",
            "password",
            "private_key",
            "provider_token",
            "refresh_token",
        }
    )

    def __init__(
        self,
        settings: Settings,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.env = os.environ if env is None else env

    def _required(self, name: str) -> str:
        value = self.env.get(name, "").strip()
        if not value:
            raise ConfigurationError(f"{name} is required for the Telnexa adapter")
        return value

    def _base_url(self) -> str:
        value = self._required("TELNEXA_SMS_BASE_URL").rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError("TELNEXA_SMS_BASE_URL must be an HTTP(S) origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigurationError(
                "TELNEXA_SMS_BASE_URL must not contain credentials, query, or fragment"
            )
        if self.settings.app_env == "production" and parsed.scheme != "https":
            raise ConfigurationError("production Telnexa delivery requires HTTPS")
        return value

    def _api_key(self) -> str:
        return self._required("TELNEXA_SMS_API_KEY")

    def _validate_identity(self, request: CommandExecutionRequest) -> None:
        if request.target != "telnexa-sms":
            raise TelnexaProviderAdapterError(
                "Telnexa adapter does not own this command target"
            )
        if request.capability != "SMS_DELIVERY":
            raise TelnexaProviderAdapterError(
                "Telnexa command capability must be SMS_DELIVERY"
            )
        if request.command_type not in self.SUPPORTED:
            raise TelnexaProviderAdapterError(
                f"unsupported Telnexa command type: {request.command_type}"
            )
        if request.command_version != "1.0":
            raise TelnexaProviderAdapterError("Telnexa command version must be 1.0")

    def _require_active(self, request: CommandExecutionRequest) -> None:
        self._validate_identity(request)
        if not self.settings.sms_delivery_enabled:
            raise TelnexaProviderAdapterError(
                "SMS delivery is disabled by SMS_DELIVERY_ENABLED or its umbrella "
                "switch EXTERNAL_DELIVERY_ENABLED"
            )
        error = next(
            iter(
                _telnexa_sms_command_validator().iter_errors(
                    {
                        "command_id": request.command_id,
                        "command_type": request.command_type,
                        "command_version": request.command_version,
                        "target": request.target,
                        "tenant_id": request.tenant_id,
                        "requested_by": request.requested_by,
                        "correlation_id": request.correlation_id,
                        "idempotency_key": request.idempotency_key,
                        "capability": request.capability,
                        "payload": request.payload,
                    }
                )
            ),
            None,
        )
        if error is not None:
            raise TelnexaProviderAdapterError(
                f"Telnexa SMS command violates its canonical contract: {error.message}"
            )

    def _submission(self, request: CommandExecutionRequest) -> dict[str, Any]:
        """Project the canonical command payload onto Telnexa's send contract.

        ``encoding``, ``characters`` and ``segments`` are deliberately not sent:
        Telnexa recomputes them and is the billing authority for segmentation.
        """
        payload = request.payload
        leaked = sorted(self.FORBIDDEN_PAYLOAD_KEYS.intersection(payload))
        if leaked:
            raise TelnexaProviderAdapterError(
                "command payload carries forbidden secret keys: " + ", ".join(leaked)
            )
        billing_account_id = payload.get("billing_account_id")
        if not isinstance(billing_account_id, str) or not billing_account_id:
            raise TelnexaProviderAdapterError(
                "Telnexa requires billing_account_id on the SMS command"
            )
        submission: dict[str, Any] = {
            "billing_account_id": billing_account_id,
            "destination": payload["destination"],
            "sender": payload["sender"],
            "content": payload["content"],
            "category": payload["category"],
        }
        campaign_id = payload.get("campaign_id")
        if isinstance(campaign_id, str) and campaign_id:
            submission["campaign_id"] = campaign_id
        client_reference = payload.get("client_reference")
        if isinstance(client_reference, str) and client_reference:
            submission["client_reference"] = client_reference
        return submission

    def _headers(self, request: CommandExecutionRequest) -> dict[str, str]:
        return {
            "X-API-Key": self._api_key(),
            "X-Tenant-ID": request.tenant_id,
            "X-Correlation-ID": request.correlation_id,
            "Idempotency-Key": request.idempotency_key,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _error_code(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return "unparseable-response"
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, str):
                return detail
            if isinstance(payload.get("error"), str):
                return payload["error"]
        return "unspecified"

    @staticmethod
    def _provider_message_id(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in ("provider_message_id", "message_id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    async def _submit(
        self,
        request: CommandExecutionRequest,
        submission: dict[str, Any],
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=25.0) as client:
            return await client.post(
                f"{self._base_url()}{self.MESSAGES_PATH}",
                json=submission,
                headers=self._headers(request),
            )

    async def execute(self, request: CommandExecutionRequest) -> ActivityResult:
        self._require_active(request)
        submission = self._submission(request)
        try:
            response = await self._submit(request, submission)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The connection was never established, so no submission exists.
            raise TelnexaProviderAdapterError(
                "Telnexa connection failed before the submission was sent"
            ) from exc
        except httpx.HTTPError as exc:
            # The submission may have been recorded. Resolve it by replay.
            return await self._reconcile_unknown_submission(request, str(exc))

        if response.status_code in self.ACCEPTED_STATUSES:
            try:
                data = response.json()
            except ValueError as exc:
                raise TelnexaProviderAdapterError(
                    "Telnexa accepted the submission with an unreadable body"
                ) from exc
            return ActivityResult(
                status="accepted",
                detail="Telnexa accepted the canonical SMS submission",
                provider_operation_id=self._provider_message_id(data)
                or request.command_id,
            )
        if response.status_code in {502, 503, 504}:
            return await self._reconcile_unknown_submission(
                request, f"gateway status {response.status_code}"
            )
        raise TelnexaProviderAdapterError(
            "Telnexa rejected the submission with status "
            f"{response.status_code}: {self._error_code(response)}"
        )

    async def _reconcile_unknown_submission(
        self,
        request: CommandExecutionRequest,
        reason: str,
    ) -> ActivityResult:
        result = await self.readback(request)
        if result.status == "matched":
            return ActivityResult(
                status="accepted",
                detail=(
                    f"Telnexa outcome was unknown ({reason}); the idempotent "
                    "replay proved the submission was recorded exactly once"
                ),
                provider_operation_id=result.provider_operation_id,
            )
        raise TelnexaProviderAdapterError(
            f"Telnexa outcome unknown ({reason}); {result.detail}"
        )

    async def readback(self, request: CommandExecutionRequest) -> ActivityResult:
        """Resolve the recorded outcome by replaying the identical submission.

        This cannot send a second message: Telnexa returns the already-stored
        message for a repeated ``Idempotency-Key`` and rejects a body that does
        not match the stored request hash.
        """
        self._validate_identity(request)
        submission = self._submission(request)
        try:
            response = await self._submit(request, submission)
        except httpx.HTTPError as exc:
            raise TelnexaProviderAdapterError(
                "Telnexa submission reconciliation failed"
            ) from exc

        if response.status_code in self.ACCEPTED_STATUSES:
            try:
                data = response.json()
            except ValueError:
                data = None
            return ActivityResult(
                status="matched",
                detail="Telnexa submission matched durable command intent",
                provider_operation_id=self._provider_message_id(data)
                or request.command_id,
            )
        if (
            response.status_code == 409
            and self._error_code(response) == self.IDEMPOTENCY_CONFLICT
        ):
            return ActivityResult(
                status="mismatch",
                detail=(
                    "the idempotency key is already bound to a different Telnexa "
                    "submission"
                ),
                provider_operation_id=request.command_id,
            )
        return ActivityResult(
            status="mismatch",
            detail=(
                "Telnexa reconciliation returned status "
                f"{response.status_code}: {self._error_code(response)}"
            ),
            provider_operation_id=request.command_id,
        )
