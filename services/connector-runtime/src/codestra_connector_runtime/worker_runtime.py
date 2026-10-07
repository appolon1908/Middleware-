"""Trusted Middleware worker assembly; no provider binding or activation here."""

from collections.abc import Callable
from typing import Any

import structlog

from middleware.connector_sdk import ConnectorRegistry, ConnectorRuntime
from middleware.connector_sdk.interfaces import CapabilityProvider
from middleware.connector_sdk.models import CommandRequest

from .api.config import RuntimeSettings
from .api.database import Database
from .command_journal import PostgresOperationStore


def build_worker_runtime(
    *,
    settings: RuntimeSettings,
    database: Database,
    registry: ConnectorRegistry,
    capabilities: CapabilityProvider,
    authorize: Callable[[CommandRequest], bool],
) -> ConnectorRuntime:
    """Require the canonical policy engine's verdict and a durable journal.

    The caller supplies reviewed adapter factories and effective tenant capability
    authority. Never use JWT claims or request snapshots as the authorizer.
    """
    logger = structlog.get_logger(settings.service_name)

    def authority(request: CommandRequest) -> bool:
        return settings.external_effects_enabled and authorize(request) is True

    def observe(event: str, fields: Any) -> None:
        logger.info("connector_command_" + event, **fields)

    return ConnectorRuntime(
        registry,
        capabilities,
        operations=PostgresOperationStore(database, settings.environment),
        authorize=authority,
        observe=observe,
    )
