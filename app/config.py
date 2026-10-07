from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, unquote, urlparse


FALSE_VALUES = {"0", "false", "no", "off", ""}
TRUE_VALUES = {"1", "true", "yes", "on"}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
WEBHOOK_PRODUCERS = (
    "odoo-integration",
    "n8n-automation",
    "vicidial-adapter",
    "telnexa-gateway",
    "klyrow-gateway",
    "kyqra-gateway",
    "postly-adapter",
)
ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PROFILES_PATH = ROOT / "config" / "runtime-profiles.v1.json"


class ConfigurationError(RuntimeError):
    """Raised when runtime configuration is unsafe or incomplete."""


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be an explicit boolean")


# Effects this runtime actually implements. Anything else must stay off, so a
# capability cannot be switched on before its handler exists.
SUPPORTED_EXTERNAL_EFFECTS = frozenset(
    {
        "SEND_EVENTS",
        "ODOO_WRITE",
        "FORM_ODOO_DELIVERY_ENABLED",
        "CRAWLER_ODOO_DELIVERY_ENABLED",
        "SCRAPPER_ODOO_DELIVERY_ENABLED",
        "SMS_DELIVERY_ENABLED",
        "EMAIL_DELIVERY_ENABLED",
        "SOCIAL_DELIVERY_ENABLED",
    }
)

EXTERNAL_DELIVERY_EFFECTS = frozenset(
    {
        "ODOO_WRITE",
        "FORM_ODOO_DELIVERY_ENABLED",
        "CRAWLER_ODOO_DELIVERY_ENABLED",
        "SCRAPPER_ODOO_DELIVERY_ENABLED",
        "SMS_DELIVERY_ENABLED",
        "EMAIL_DELIVERY_ENABLED",
    }
)

# System-wide kill switches are a separate contract from implementation-level
# effect gates.  They must never be inferred from the lower-level controls.
UMBRELLA_CONTROL_NAMES = (
    "LIVE_ADVERTISING_ENABLED",
    "EXTERNAL_DELIVERY_ENABLED",
    "SOCIAL_PUBLISHING_ENABLED",
    "EXTERNAL_MODEL_CALLS_ENABLED",
    "N8N_EXTERNAL_PROVIDER_WRITES",
)


def _int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(env.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _secret_env_name(producer_client_id: str) -> str:
    return (
        "WEBHOOK_SECRET_"
        + producer_client_id.upper().replace("-", "_").replace(".", "_")
    )


def _is_absolute_mount_path(path: Path) -> bool:
    value = str(path)
    return (
        path.is_absolute()
        or value.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[\\/]", value) is not None
    )


def _runtime_profiles() -> dict[str, dict[str, object]]:
    try:
        value = json.loads(RUNTIME_PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("runtime profile registry cannot be loaded") from exc
    if value.get("schema_version") != "1.0":
        raise ConfigurationError("runtime profile registry version is unsupported")
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, list) or len(raw_profiles) < 2:
        raise ConfigurationError("runtime profile registry must declare at least two profiles")
    profiles: dict[str, dict[str, object]] = {}
    for raw in raw_profiles:
        if not isinstance(raw, dict):
            raise ConfigurationError("runtime profile must be an object")
        profile_id = raw.get("profile_id")
        if not isinstance(profile_id, str) or profile_id in profiles:
            raise ConfigurationError("runtime profile identity is invalid or duplicated")
        profiles[profile_id] = raw
    return profiles


@dataclass(frozen=True)
class Settings:
    app_env: str
    runtime_profile_id: str | None
    app_version: str
    source_sha: str
    image_digest: str
    schema_head: str
    build_time: str
    issuer: str
    jwks_uri: str
    jwks_timeout_seconds: int
    readiness_timeout_seconds: int
    audience: str
    database_url: str | None
    redis_url: str | None
    nats_url: str | None
    nats_stream: str
    nats_subject_prefix: str
    nats_credentials_file: Path | None
    nats_dispatch_mode: str
    nats_allow_insecure_test_connection: bool
    production_activation_id: str | None
    production_dialing: str
    temporal_address: str | None
    temporal_namespace: str
    temporal_task_queue: str
    temporal_worker_mode: str
    temporal_server_root_ca_file: Path | None
    temporal_client_cert_file: Path | None
    temporal_client_key_file: Path | None
    temporal_tls_server_name: str | None
    temporal_allow_insecure_test_connection: bool
    allow_in_memory_storage: bool
    max_request_body_bytes: int
    webhook_max_clock_skew_seconds: int
    webhook_replay_retention_seconds: int
    webhook_secrets: dict[str, bytes]
    outbox_dispatch_enabled: bool
    external_effects: dict[str, bool]
    umbrella_controls: dict[str, bool]
    odoo_base_url: str | None = None
    odoo_default_hmac_secret: bytes = b""
    odoo_tenant_hmac_secrets: dict[str, bytes] = field(default_factory=dict)
    odoo_timeout_seconds: int = 20
    release_id: str = "unknown"
    configuration_checksum: str = "unknown"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        issuer = source.get(
            "KEYCLOAK_ISSUER",
            "https://auth.codestra.co/realms/codestra",
        ).rstrip("/")
        jwks = source.get(
            "KEYCLOAK_JWKS_URI",
            f"{issuer}/protocol/openid-connect/certs",
        )
        effects = {
            name: _bool(source, name, False)
            for name in (
                "SEND_EVENTS",
                "ENABLE_EXTERNAL_DELIVERY",
                "LIVE_WRITE",
                "LIVE_WRITES",
                "ODOO_WRITE",
                "CALLBACK_DISPATCH",
                "N8N_DELIVERY_ENABLED",
                "VICIDIAL_WRITES_ENABLED",
                "EXTERNAL_DIAL_ENABLED",
                "PRODUCTION_CALLBACKS_ENABLED",
                "N8N_PRODUCTION_WORKFLOWS_ENABLED",
                "FORM_ODOO_DELIVERY_ENABLED",
                "CRAWLER_ODOO_DELIVERY_ENABLED",
                "SCRAPPER_ODOO_DELIVERY_ENABLED",
                "CRAWLER_EXTERNAL_CONTACT_ENABLED",
                "SCRAPPER_EXTERNAL_CONTACT_ENABLED",
                "SMS_DELIVERY_ENABLED",
                "EMAIL_DELIVERY_ENABLED",
                "SOCIAL_DELIVERY_ENABLED",
                "CRAWLER_EXECUTION_ENABLED",
                "SCRAPPER_EXECUTION_ENABLED",
                "LIVE_SMS_DELIVERY",
                "LIVE_EMAIL_DELIVERY",
                "UNRESTRICTED_CRAWLING",
            )
        }
        umbrella_controls = {
            name: _bool(source, name, False) for name in UMBRELLA_CONTROL_NAMES
        }
        odoo_tenant_secrets_raw = source.get("ODOO_19_TENANT_HMAC_SECRETS", "").strip()
        odoo_tenant_secrets: dict[str, bytes] = {}
        if odoo_tenant_secrets_raw:
            try:
                decoded = json.loads(odoo_tenant_secrets_raw)
            except ValueError as exc:
                raise ConfigurationError(
                    "ODOO_19_TENANT_HMAC_SECRETS must be a JSON object"
                ) from exc
            if not isinstance(decoded, dict) or not all(
                isinstance(key, str) and isinstance(value, str) and key and value
                for key, value in decoded.items()
            ):
                raise ConfigurationError(
                    "ODOO_19_TENANT_HMAC_SECRETS must map tenant IDs to secrets"
                )
            odoo_tenant_secrets = {
                key: value.encode("utf-8") for key, value in decoded.items()
            }
        webhook_secrets = {
            producer: source.get(_secret_env_name(producer), "").encode("utf-8")
            for producer in WEBHOOK_PRODUCERS
        }
        settings = cls(
            app_env=source.get("APP_ENV", "development").strip().lower(),
            runtime_profile_id=(
                source.get("RUNTIME_PROFILE_ID", "").strip() or None
            ),
            app_version=source.get("APP_VERSION", "0.1.0").strip(),
            source_sha=source.get("APP_SOURCE_SHA", "unknown").strip(),
            image_digest=source.get("IMAGE_DIGEST", "unknown").strip(),
            schema_head=source.get(
                "SCHEMA_HEAD",
                "0009_observability_incidents",
            ).strip(),
            build_time=source.get("BUILD_TIME", "unknown").strip(),
            release_id=source.get("RELEASE_ID", "unknown").strip(),
            configuration_checksum=source.get(
                "CONFIGURATION_CHECKSUM",
                "unknown",
            ).strip(),
            issuer=issuer,
            jwks_uri=jwks,
            jwks_timeout_seconds=_int(
                source,
                "JWKS_TIMEOUT_SECONDS",
                3,
                minimum=1,
                maximum=10,
            ),
            readiness_timeout_seconds=_int(
                source,
                "READINESS_TIMEOUT_SECONDS",
                3,
                minimum=1,
                maximum=10,
            ),
            audience=source.get("MIDDLEWARE_AUDIENCE", "middleware-api"),
            database_url=source.get("DATABASE_URL") or None,
            redis_url=source.get("REDIS_URL") or None,
            nats_url=source.get("NATS_URL") or None,
            nats_stream=source.get("NATS_STREAM", "CODESTRA_EVENTS").strip(),
            nats_subject_prefix=source.get(
                "NATS_SUBJECT_PREFIX",
                "codestra.events",
            ).strip(),
            nats_credentials_file=(
                Path(source["NATS_CREDS_FILE"])
                if source.get("NATS_CREDS_FILE")
                else None
            ),
            nats_dispatch_mode=source.get(
                "NATS_DISPATCH_MODE",
                "disabled",
            ).strip().lower(),
            nats_allow_insecure_test_connection=_bool(
                source,
                "NATS_ALLOW_INSECURE_TEST_CONNECTION",
                False,
            ),
            production_activation_id=(
                source.get("PRODUCTION_ACTIVATION_ID", "").strip() or None
            ),
            production_dialing=source.get(
                "PRODUCTION_DIALING",
                "DISABLED",
            ).strip(),
            temporal_address=source.get("TEMPORAL_ADDRESS") or None,
            temporal_namespace=source.get(
                "TEMPORAL_NAMESPACE",
                "codestra-production",
            ).strip(),
            temporal_task_queue=source.get(
                "TEMPORAL_TASK_QUEUE",
                "codestra-production-critical",
            ).strip(),
            temporal_worker_mode=source.get(
                "TEMPORAL_WORKER_MODE",
                "disabled",
            ).strip().lower(),
            temporal_server_root_ca_file=(
                Path(source["TEMPORAL_SERVER_ROOT_CA_FILE"])
                if source.get("TEMPORAL_SERVER_ROOT_CA_FILE")
                else None
            ),
            temporal_client_cert_file=(
                Path(source["TEMPORAL_CLIENT_CERT_FILE"])
                if source.get("TEMPORAL_CLIENT_CERT_FILE")
                else None
            ),
            temporal_client_key_file=(
                Path(source["TEMPORAL_CLIENT_KEY_FILE"])
                if source.get("TEMPORAL_CLIENT_KEY_FILE")
                else None
            ),
            temporal_tls_server_name=(
                source.get("TEMPORAL_TLS_SERVER_NAME", "").strip() or None
            ),
            temporal_allow_insecure_test_connection=_bool(
                source,
                "TEMPORAL_ALLOW_INSECURE_TEST_CONNECTION",
                False,
            ),
            allow_in_memory_storage=_bool(source, "ALLOW_IN_MEMORY_STORAGE", False),
            max_request_body_bytes=_int(
                source,
                "MAX_REQUEST_BODY_BYTES",
                1_048_576,
                minimum=1_024,
                maximum=10_485_760,
            ),
            webhook_max_clock_skew_seconds=_int(
                source,
                "WEBHOOK_MAX_CLOCK_SKEW_SECONDS",
                300,
                minimum=1,
                maximum=300,
            ),
            webhook_replay_retention_seconds=_int(
                source,
                "WEBHOOK_REPLAY_RETENTION_SECONDS",
                86_400,
                minimum=86_400,
                maximum=2_592_000,
            ),
            webhook_secrets=webhook_secrets,
            outbox_dispatch_enabled=_bool(source, "OUTBOX_DISPATCH_ENABLED", False),
            external_effects=effects,
            umbrella_controls=umbrella_controls,
            odoo_base_url=(source.get("ODOO_19_BASE_URL", "").strip() or None),
            odoo_default_hmac_secret=source.get(
                "ODOO_19_HMAC_SECRET", ""
            ).encode("utf-8"),
            odoo_tenant_hmac_secrets=odoo_tenant_secrets,
            odoo_timeout_seconds=_int(
                source,
                "ODOO_19_TIMEOUT_SECONDS",
                20,
                minimum=1,
                maximum=120,
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.app_env not in {"development", "test", "staging", "production"}:
            raise ConfigurationError("APP_ENV is not recognized")
        expected_issuer = (
            "https://auth-staging.codestra.co/realms/codestra"
            if self.app_env == "staging"
            else "https://auth.codestra.co/realms/codestra"
        )
        if self.issuer != expected_issuer:
            raise ConfigurationError(
                f"KEYCLOAK_ISSUER must match the {self.app_env} identity authority"
            )
        if self.jwks_uri != f"{self.issuer}/protocol/openid-connect/certs":
            raise ConfigurationError("KEYCLOAK_JWKS_URI must match the canonical issuer")
        if self.audience != "middleware-api":
            raise ConfigurationError("MIDDLEWARE_AUDIENCE must be middleware-api")
        self._validate_environment_profile()
        enabled = {
            name for name, value in self.external_effects.items() if value
        }
        unsupported_enabled = sorted(enabled - SUPPORTED_EXTERNAL_EFFECTS)
        if unsupported_enabled:
            raise ConfigurationError(
                "provider and business effects are not implemented by this runtime: "
                + ", ".join(unsupported_enabled)
            )
        enabled_umbrella_controls = sorted(
            name for name, value in self.umbrella_controls.items() if value
        )
        if self.app_env == "staging" and enabled_umbrella_controls:
            raise ConfigurationError(
                "staging umbrella controls must remain disabled: "
                + ", ".join(enabled_umbrella_controls)
            )
        enabled_delivery_effects = sorted(enabled & EXTERNAL_DELIVERY_EFFECTS)
        if (
            enabled_delivery_effects
            and self.umbrella_controls["EXTERNAL_DELIVERY_ENABLED"] is not True
        ):
            raise ConfigurationError(
                "EXTERNAL_DELIVERY_ENABLED must be true before enabling: "
                + ", ".join(enabled_delivery_effects)
            )
        self._validate_odoo_transport(enabled)
        if (
            "SOCIAL_DELIVERY_ENABLED" in enabled
            and self.umbrella_controls["SOCIAL_PUBLISHING_ENABLED"] is not True
        ):
            raise ConfigurationError(
                "SOCIAL_PUBLISHING_ENABLED must be true before enabling: "
                "SOCIAL_DELIVERY_ENABLED"
            )
        if self.production_dialing != "DISABLED":
            raise ConfigurationError(
                "PRODUCTION_DIALING must remain DISABLED"
            )
        if self.nats_dispatch_mode not in {"disabled", "isolated", "production"}:
            raise ConfigurationError(
                "NATS_DISPATCH_MODE must be disabled, isolated, or production"
            )
        send_events = "SEND_EVENTS" in enabled
        dispatch_configured = self.nats_dispatch_mode != "disabled"
        if not (
            self.outbox_dispatch_enabled == send_events == dispatch_configured
        ):
            raise ConfigurationError(
                "OUTBOX_DISPATCH_ENABLED, SEND_EVENTS, and NATS_DISPATCH_MODE "
                "must be enabled or disabled together"
            )
        if self.outbox_dispatch_enabled:
            if self.nats_dispatch_mode == "production" and self.app_env != "production":
                raise ConfigurationError(
                    "production JetStream dispatch requires APP_ENV=production"
                )
            if self.nats_dispatch_mode == "isolated" and self.app_env == "production":
                raise ConfigurationError(
                    "isolated JetStream dispatch is forbidden in production"
                )
            parsed_nats = urlparse(self.nats_url or "")
            insecure_local_test = (
                self.nats_allow_insecure_test_connection
                and self.app_env in {"test", "development"}
                and parsed_nats.scheme == "nats"
                and parsed_nats.hostname in {"127.0.0.1", "localhost"}
            )
            if not insecure_local_test and (
                parsed_nats.scheme != "tls" or not parsed_nats.hostname
            ):
                raise ConfigurationError(
                    "NATS_URL must use tls:// with a hostname outside disposable tests"
                )
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", self.nats_stream):
                raise ConfigurationError("NATS_STREAM is invalid")
            if not re.fullmatch(
                r"[a-z0-9]+(?:\.[a-z0-9_-]+)+",
                self.nats_subject_prefix,
            ):
                raise ConfigurationError("NATS_SUBJECT_PREFIX is invalid")
            if not insecure_local_test and (
                    self.nats_credentials_file is None
                    or not _is_absolute_mount_path(self.nats_credentials_file)
                ):
                raise ConfigurationError(
                    "NATS_CREDS_FILE must be an absolute mounted credential path"
                )
            if self.nats_dispatch_mode == "isolated":
                expected_environment = (
                    "staging" if self.app_env == "staging" else "test"
                )
                expected_stream = f"CODESTRA_{expected_environment.upper()}_EVENTS"
                expected_prefix = f"codestra.{expected_environment}.events"
                if self.nats_stream != expected_stream:
                    raise ConfigurationError(
                        f"isolated JetStream must use NATS_STREAM={expected_stream}"
                    )
                if self.nats_subject_prefix != expected_prefix:
                    raise ConfigurationError(
                        "isolated JetStream subject prefix does not match the environment"
                    )
            else:
                if self.nats_stream != "CODESTRA_EVENTS":
                    raise ConfigurationError(
                        "production JetStream must use NATS_STREAM=CODESTRA_EVENTS"
                    )
                if self.nats_subject_prefix != "codestra.events":
                    raise ConfigurationError(
                        "production JetStream must use NATS_SUBJECT_PREFIX=codestra.events"
                    )
                if not self.production_activation_id or not re.fullmatch(
                    r"[A-Z0-9][A-Z0-9._/-]{7,127}",
                    self.production_activation_id,
                ):
                    raise ConfigurationError(
                        "PRODUCTION_ACTIVATION_ID must identify the approved activation"
                    )
        elif self.nats_allow_insecure_test_connection:
            raise ConfigurationError(
                "NATS_ALLOW_INSECURE_TEST_CONNECTION requires isolated dispatch"
            )
        if self.temporal_worker_mode not in {"disabled", "isolated", "production"}:
            raise ConfigurationError(
                "TEMPORAL_WORKER_MODE must be disabled, isolated, or production"
            )
        if self.temporal_worker_mode == "disabled":
            if self.temporal_allow_insecure_test_connection:
                raise ConfigurationError(
                    "TEMPORAL_ALLOW_INSECURE_TEST_CONNECTION requires isolated mode"
                )
        else:
            if not self.temporal_address:
                raise ConfigurationError(
                    "TEMPORAL_ADDRESS is required when the worker is enabled"
                )
            insecure_temporal_test = (
                self.temporal_allow_insecure_test_connection
                and self.app_env in {"test", "development"}
                and self.temporal_address.startswith(("127.0.0.1:", "localhost:"))
            )
            if self.temporal_worker_mode == "production":
                if self.app_env != "production":
                    raise ConfigurationError(
                        "production Temporal mode requires APP_ENV=production"
                    )
                expected_namespace = "codestra-production"
                expected_task_queue = "codestra-production-critical"
                if not self.production_activation_id or not re.fullmatch(
                    r"[A-Z0-9][A-Z0-9._/-]{7,127}",
                    self.production_activation_id,
                ):
                    raise ConfigurationError(
                        "production Temporal mode requires PRODUCTION_ACTIVATION_ID"
                    )
            else:
                if self.app_env == "production":
                    raise ConfigurationError(
                        "isolated Temporal mode is forbidden in production"
                    )
                environment = "staging" if self.app_env == "staging" else "test"
                expected_namespace = f"codestra-{environment}"
                expected_task_queue = f"codestra-{environment}-critical"
            if self.temporal_namespace != expected_namespace:
                raise ConfigurationError(
                    "Temporal namespace does not match the selected environment"
                )
            if self.temporal_task_queue != expected_task_queue:
                raise ConfigurationError(
                    "Temporal task queue does not match the selected environment"
                )
            tls_paths = (
                self.temporal_server_root_ca_file,
                self.temporal_client_cert_file,
                self.temporal_client_key_file,
            )
            if not insecure_temporal_test and any(
                path is None or not _is_absolute_mount_path(path)
                for path in tls_paths
            ):
                raise ConfigurationError(
                    "Temporal requires absolute mounted CA, client certificate, "
                    "and client key paths"
                )
            if not insecure_temporal_test and not self.temporal_tls_server_name:
                raise ConfigurationError(
                    "TEMPORAL_TLS_SERVER_NAME is required with TLS"
                )
        if self.allow_in_memory_storage:
            if self.app_env not in {"test", "development"}:
                raise ConfigurationError(
                    "ALLOW_IN_MEMORY_STORAGE is allowed only in test/development"
                )
        elif not self.database_url or not self.redis_url:
            raise ConfigurationError(
                "DATABASE_URL and REDIS_URL are required unless explicitly using "
                "in-memory storage in test/development"
            )
        if self.schema_head != "0009_observability_incidents":
            raise ConfigurationError(
                "SCHEMA_HEAD must be 0009_observability_incidents"
            )
        if self.app_env in {"staging", "production"}:
            if not SHA40.fullmatch(self.source_sha):
                raise ConfigurationError("APP_SOURCE_SHA must be an exact 40-character SHA")
            if not IMAGE_DIGEST.fullmatch(self.image_digest):
                raise ConfigurationError("IMAGE_DIGEST must be an immutable sha256 digest")
            if self.build_time in {"", "unknown"}:
                raise ConfigurationError("BUILD_TIME is required in staging/production")
            self.validate_all_webhook_secrets()

    @property
    def odoo_delivery_enabled(self) -> bool:
        return (
            self.umbrella_controls.get("EXTERNAL_DELIVERY_ENABLED") is True
            and self.external_effects.get("ODOO_WRITE") is True
        )

    def odoo_secret_for(self, tenant_id: str) -> bytes:
        return self.odoo_tenant_hmac_secrets.get(
            tenant_id, self.odoo_default_hmac_secret
        )

    def odoo_source_delivery_enabled(self, provenance_method: str) -> bool:
        gate = {
            "submitted_by_person": "FORM_ODOO_DELIVERY_ENABLED",
            "crawler_discovery": "CRAWLER_ODOO_DELIVERY_ENABLED",
            "scraper_import": "SCRAPPER_ODOO_DELIVERY_ENABLED",
        }.get(provenance_method)
        if gate is None:
            return False
        return self.odoo_delivery_enabled and bool(self.external_effects.get(gate))

    def _validate_odoo_transport(self, enabled: set[str]) -> None:
        source_scoped = {
            name for name in enabled if name.endswith("_ODOO_DELIVERY_ENABLED")
        }
        if source_scoped and "ODOO_WRITE" not in enabled:
            raise ConfigurationError(
                "source-scoped Odoo delivery requires ODOO_WRITE: "
                + ", ".join(sorted(source_scoped))
            )
        if "ODOO_WRITE" not in enabled:
            return
        if not self.odoo_base_url:
            raise ConfigurationError("ODOO_19_BASE_URL is required to write to Odoo")
        if not self.odoo_base_url.startswith("https://"):
            raise ConfigurationError("ODOO_19_BASE_URL must be an HTTPS endpoint")
        secrets = [self.odoo_default_hmac_secret, *self.odoo_tenant_hmac_secrets.values()]
        if not any(secrets):
            raise ConfigurationError(
                "ODOO_19_HMAC_SECRET or ODOO_19_TENANT_HMAC_SECRETS is required "
                "to write to Odoo"
            )
        if any(secret and len(secret) < 32 for secret in secrets):
            raise ConfigurationError(
                "Odoo signing secrets must be at least 32 bytes"
            )

    @property
    def social_publishing_enabled(self) -> bool:
        """True only when the effect gate and its own umbrella switch are on.

        Social publishing hangs off SOCIAL_PUBLISHING_ENABLED, not the
        EXTERNAL_DELIVERY_ENABLED umbrella that guards the messaging effects.
        """
        return bool(
            self.external_effects.get("SOCIAL_DELIVERY_ENABLED")
            and self.umbrella_controls.get("SOCIAL_PUBLISHING_ENABLED")
        )

    @property
    def email_delivery_enabled(self) -> bool:
        """True only when the effect gate and its umbrella switch are both on."""
        return bool(
            self.external_effects.get("EMAIL_DELIVERY_ENABLED")
            and self.umbrella_controls.get("EXTERNAL_DELIVERY_ENABLED")
        )

    @property
    def sms_delivery_enabled(self) -> bool:
        """True only when the effect gate and its umbrella switch are both on."""
        return bool(
            self.external_effects.get("SMS_DELIVERY_ENABLED")
            and self.umbrella_controls.get("EXTERNAL_DELIVERY_ENABLED")
        )

    def validate_all_webhook_secrets(self) -> None:
        for producer in WEBHOOK_PRODUCERS:
            secret = self.webhook_secrets.get(producer, b"")
            name = _secret_env_name(producer)
            if len(secret) < 32:
                raise ConfigurationError(
                    f"{name} must be configured with at least 32 bytes"
                )

    def webhook_secret(self, producer_client_id: str) -> bytes:
        if producer_client_id not in WEBHOOK_PRODUCERS:
            raise ConfigurationError(f"unknown webhook producer: {producer_client_id}")
        secret = self.webhook_secrets.get(producer_client_id, b"")
        name = _secret_env_name(producer_client_id)
        if len(secret) < 32:
            raise ConfigurationError(f"{name} must contain at least 32 bytes")
        return secret

    def _validate_environment_profile(self) -> None:
        if self.app_env not in {"staging", "production"}:
            if self.runtime_profile_id is not None:
                raise ConfigurationError(
                    "RUNTIME_PROFILE_ID is reserved for staging/production"
                )
            return
        profiles = _runtime_profiles()
        profile = profiles.get(self.runtime_profile_id or "")
        if profile is None:
            raise ConfigurationError(
                "RUNTIME_PROFILE_ID must select a registered runtime profile"
            )
        if profile is None or profile.get("environment") != self.app_env:
            raise ConfigurationError("runtime profile does not match APP_ENV")
        self._validate_database_profile(profile["database"])
        self._validate_redis_profile(profile["redis"])

        nats_profile = profile["nats"]
        assert isinstance(nats_profile, dict)
        if self.nats_stream != nats_profile["stream"]:
            raise ConfigurationError("NATS_STREAM does not match the runtime profile")
        if self.nats_subject_prefix != nats_profile["subject_prefix"]:
            raise ConfigurationError(
                "NATS_SUBJECT_PREFIX does not match the runtime profile"
            )
        if self.nats_url is not None:
            try:
                parsed_nats = urlparse(self.nats_url)
                nats_port = parsed_nats.port
            except ValueError as exc:
                raise ConfigurationError("NATS_URL is malformed") from exc
            if (
                parsed_nats.scheme != "tls"
                or parsed_nats.hostname != nats_profile["host"]
                or nats_port != nats_profile["port"]
                or parsed_nats.username is not None
                or parsed_nats.password is not None
                or parsed_nats.path not in {"", "/"}
                or parsed_nats.query
                or parsed_nats.fragment
            ):
                raise ConfigurationError("NATS_URL does not match the runtime profile")

        temporal_profile = profile["temporal"]
        assert isinstance(temporal_profile, dict)
        if self.temporal_namespace != temporal_profile["namespace"]:
            raise ConfigurationError(
                "TEMPORAL_NAMESPACE does not match the runtime profile"
            )
        if self.temporal_task_queue != temporal_profile["task_queue"]:
            raise ConfigurationError(
                "TEMPORAL_TASK_QUEUE does not match the runtime profile"
            )
        if (
            self.temporal_address is not None
            and self.temporal_address != temporal_profile["address"]
        ):
            raise ConfigurationError(
                "TEMPORAL_ADDRESS does not match the runtime profile"
            )
        temporal_host = str(temporal_profile["address"]).rsplit(":", 1)[0]
        if (
            self.temporal_tls_server_name is not None
            and self.temporal_tls_server_name != temporal_host
        ):
            raise ConfigurationError(
                "TEMPORAL_TLS_SERVER_NAME does not match the runtime profile"
            )

        secret_prefix = profile["secret_path_prefix"]
        assert isinstance(secret_prefix, str)
        for credential in (
            self.nats_credentials_file,
            self.temporal_server_root_ca_file,
            self.temporal_client_cert_file,
            self.temporal_client_key_file,
        ):
            normalized_credential = (
                str(credential).replace("\\", "/")
                if credential is not None
                else None
            )
            if (
                normalized_credential is not None
                and not normalized_credential.startswith(secret_prefix)
            ):
                raise ConfigurationError(
                    "mounted credential path does not match the runtime profile"
                )
        if (
            profile["production_activation_allowed"] is not True
            and self.production_activation_id is not None
        ):
            raise ConfigurationError(
                "PRODUCTION_ACTIVATION_ID is forbidden by the runtime profile"
            )

    def _validate_database_profile(self, raw_profile: object) -> None:
        assert isinstance(raw_profile, dict)
        try:
            parsed = urlparse(self.database_url or "")
            port = parsed.port
            query = (
                parse_qs(parsed.query, strict_parsing=True)
                if parsed.query
                else {}
            )
        except ValueError as exc:
            raise ConfigurationError("DATABASE_URL is malformed") from exc
        if (
            parsed.scheme != raw_profile["scheme"]
            or parsed.hostname != raw_profile["host"]
            or port != raw_profile["port"]
            or unquote(parsed.path.lstrip("/")) != raw_profile["name"]
            or unquote(parsed.username or "") != raw_profile["username"]
            or not parsed.password
            or query != (
                {"sslmode": [raw_profile["sslmode"]]}
                if raw_profile.get("sslmode")
                else {}
            )
            or parsed.params
            or parsed.fragment
        ):
            raise ConfigurationError(
                "DATABASE_URL does not match the locked runtime profile"
            )

    def _validate_redis_profile(self, raw_profile: object) -> None:
        assert isinstance(raw_profile, dict)
        try:
            parsed = urlparse(self.redis_url or "")
            port = parsed.port
            database = int(unquote(parsed.path.lstrip("/")))
        except ValueError as exc:
            raise ConfigurationError("REDIS_URL is malformed") from exc
        if (
            parsed.scheme != raw_profile["scheme"]
            or parsed.hostname != raw_profile["host"]
            or port != raw_profile["port"]
            or unquote(parsed.username or "") != raw_profile["username"]
            or not parsed.password
            or database != raw_profile["database"]
            or parsed.query
            or parsed.params
            or parsed.fragment
        ):
            raise ConfigurationError(
                "REDIS_URL does not match the locked runtime profile"
            )
