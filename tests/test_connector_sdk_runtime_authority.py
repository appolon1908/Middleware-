"""Behavioral regressions for the privileged runtime boundary."""

from dataclasses import replace

import pytest

from tests import test_connector_sdk_v1 as sdk_fixture

from middleware.connector_sdk import (
    CommandNotAllowedError,
    CommandOutcome,
    CommandResult,
    ConnectorRegistry,
    ConnectorRuntime,
    ConnectorVersionConflictError,
    StaticCapabilityProvider,
)


FakeAdapter = sdk_fixture.FakeAdapter


def fixture_runtime(adapter=FakeAdapter):
    fixture = sdk_fixture.ConnectorSdkStandardsTests()
    fixture.setUp()
    fixture.activate("klyrow-email", adapter)
    request = fixture.request("8ce7a6f8-32ab-4bca-a8ba-dca760128f99")
    runtime = ConnectorRuntime(
        fixture.registry,
        StaticCapabilityProvider({(request.context.tenant_id, "EMAIL_DELIVERY"): True}),
    )
    return fixture, runtime, request


def test_direct_registration_rejects_overlapping_authority_atomically():
    registry = ConnectorRegistry()
    raw = sdk_fixture.ConnectorSdkStandardsTests.raw("klyrow-email")
    registry.register_manifest(raw)
    incoming = sdk_fixture.ConnectorSdkStandardsTests.raw("klyrow-alert-email")
    incoming["commands"][0]["prefix"] = raw["commands"][0]["prefix"] + "nested."
    with pytest.raises(ConnectorVersionConflictError):
        registry.register_manifest(incoming)
    assert len(registry.list()) == 1


def test_incremental_directory_load_checks_existing_authority(tmp_path):
    import json

    registry = ConnectorRegistry()
    raw = sdk_fixture.ConnectorSdkStandardsTests.raw("klyrow-email")
    registry.register_manifest(raw)
    incoming = sdk_fixture.ConnectorSdkStandardsTests.raw("klyrow-alert-email")
    incoming["commands"][0]["prefix"] = raw["commands"][0]["prefix"]
    incoming["forbidden_command_prefixes"] = []
    (tmp_path / "incoming.connector.json").write_text(json.dumps(incoming))
    with pytest.raises(ConnectorVersionConflictError):
        registry.load_directory(tmp_path)
    assert len(registry.list()) == 1


@pytest.mark.parametrize(
    "result",
    [
        CommandResult(outcome="COMPLETED", operation_id="op-1"),
        CommandResult(
            outcome=CommandOutcome.COMPLETED, operation_id="op-1", retryable=True
        ),
        CommandResult(
            outcome=CommandOutcome.COMPLETED,
            operation_id="op-1",
            safe_result={"n": float("nan")},
        ),
        CommandResult(
            outcome=CommandOutcome.COMPLETED,
            operation_id="op-1",
            safe_result={"n": object()},
        ),
    ],
)
def test_malformed_adapter_results_fail_closed(result):
    class InvalidAdapter(FakeAdapter):
        def execute_command(self, request):
            return result

    _, runtime, request = fixture_runtime(InvalidAdapter)
    with pytest.raises(CommandNotAllowedError):
        runtime.execute(request)


@pytest.mark.parametrize("key", ["x" * 181, "bad key 0001", "bad\nkey0001"])
def test_idempotency_identity_is_bounded_printable(key):
    _, runtime, request = fixture_runtime()
    request = replace(request, context=replace(request.context, idempotency_key=key))
    with pytest.raises(CommandNotAllowedError):
        runtime.execute(request)


def test_upgrade_invalidates_trusted_factory_binding():
    fixture, _, _ = fixture_runtime()
    raw = fixture.raw("klyrow-email")
    raw["version"] = "1.1.0"
    fixture.registry.register_manifest(raw, replace=True)
    from middleware.connector_sdk import ConnectorNotFoundError

    with pytest.raises(ConnectorNotFoundError):
        fixture.registry.adapter_factory("klyrow-email")


def test_runtime_replays_terminal_result_without_resubmitting():
    class CountingAdapter(FakeAdapter):
        calls = 0

        def execute_command(self, request):
            type(self).calls += 1
            return super().execute_command(request)

    _, runtime, request = fixture_runtime(CountingAdapter)
    first = runtime.execute(request)
    assert runtime.execute(request) == first
    assert CountingAdapter.calls == 1


def test_runtime_rejects_changed_semantics_for_same_key():
    _, runtime, request = fixture_runtime()
    runtime.execute(request)
    with pytest.raises(ConnectorVersionConflictError):
        runtime.execute(replace(request, payload={"message_id": "changed"}))


def test_adapter_exception_becomes_unknown_and_replay_only_reconciles():
    class CrashedAdapter(FakeAdapter):
        submits = 0
        reconciles = 0

        def execute_command(self, request):
            type(self).submits += 1
            raise TimeoutError("provider password=never-expose-this")

        def reconcile_unknown(self, request, prior_result):
            type(self).reconciles += 1
            return replace(prior_result, outcome=CommandOutcome.UNKNOWN)

    _, runtime, request = fixture_runtime(CrashedAdapter)
    first = runtime.execute(request)
    assert first.outcome is CommandOutcome.UNKNOWN
    assert "password" not in repr(first)
    assert runtime.execute(request).outcome is CommandOutcome.UNKNOWN
    assert CrashedAdapter.submits == 1
    assert CrashedAdapter.reconciles == 2


def test_explicit_failed_retry_uses_manifest_budget_and_same_identity():
    class RetryAdapter(FakeAdapter):
        identities = []

        def execute_command(self, request):
            type(self).identities.append(
                (request.command_id, request.context.idempotency_key)
            )
            return CommandResult(
                outcome=CommandOutcome.FAILED,
                operation_id=request.command_id,
                retryable=True,
                error_code="PROVIDER_BUSY",
            )

    fixture, runtime, request = fixture_runtime(RetryAdapter)
    runtime._sleep = lambda _: None
    result = runtime.execute(request)
    budget = (
        fixture.registry.get(request.connector_id)
        .manifest.command_policies[0]
        .retry_policy.maximum_attempts
    )
    assert result.outcome is CommandOutcome.FAILED
    assert len(RetryAdapter.identities) == budget
    assert len(set(RetryAdapter.identities)) == 1
    runtime.execute(request)
    assert len(RetryAdapter.identities) == budget


def test_readback_timeout_stays_unknown_and_is_never_resubmitted():
    class ReadbackTimeout(FakeAdapter):
        submits = 0

        def execute_command(self, request):
            type(self).submits += 1
            return super().execute_command(request)

        def read_back(self, request, prior_result):
            raise TimeoutError("sensitive provider response")

    _, runtime, request = fixture_runtime(ReadbackTimeout)
    assert runtime.execute(request).outcome is CommandOutcome.UNKNOWN
    assert runtime.execute(request).outcome is CommandOutcome.UNKNOWN
    assert ReadbackTimeout.submits == 1


def test_incomplete_readback_is_not_cached_as_completed_submission():
    from middleware.connector_sdk import ReadBackRequiredError

    class DelayedReadback(FakeAdapter):
        submits = 0
        reads = 0
        result_outcome = CommandOutcome.COMPLETED

        def execute_command(self, request):
            type(self).submits += 1
            return super().execute_command(request)

        def read_back(self, request, prior_result):
            type(self).reads += 1
            return replace(
                prior_result,
                outcome=CommandOutcome.SUBMITTED
                if type(self).reads == 1
                else CommandOutcome.COMPLETED,
            )

    _, runtime, request = fixture_runtime(DelayedReadback)
    with pytest.raises(ReadBackRequiredError):
        runtime.execute(request)
    assert runtime.execute(request).outcome is CommandOutcome.COMPLETED
    assert DelayedReadback.reads == 2
    assert DelayedReadback.submits == 1


def test_connection_configuration_rejects_inline_secrets_before_adapter_call():
    _, runtime, _ = fixture_runtime()
    with pytest.raises(CommandNotAllowedError):
        runtime.test_connection("klyrow-email", {"password": "inline"})


def test_health_rejects_untyped_or_invalid_provider_status():
    from middleware.connector_sdk import ConnectorHealth

    class BadHealth(FakeAdapter):
        def health(self):
            return ConnectorHealth(status="arbitrary", checked_at_epoch=-1)

    _, runtime, _ = fixture_runtime(BadHealth)
    with pytest.raises(CommandNotAllowedError):
        runtime.health("klyrow-email")


def test_authorization_is_rechecked_before_each_retry():
    from middleware.connector_sdk import CapabilityDisabledError

    class RetryAdapter(FakeAdapter):
        submits = 0

        def execute_command(self, request):
            type(self).submits += 1
            return CommandResult(
                CommandOutcome.FAILED,
                request.command_id,
                retryable=True,
                error_code="PROVIDER_BUSY",
            )

    fixture, _, request = fixture_runtime(RetryAdapter)
    verdicts = iter([True, True, False])
    runtime = ConnectorRuntime(
        fixture.registry,
        StaticCapabilityProvider({(request.context.tenant_id, "EMAIL_DELIVERY"): True}),
        authorize=lambda _: next(verdicts),
        sleep=lambda _: None,
    )
    with pytest.raises(CapabilityDisabledError):
        runtime.execute(request)
    assert RetryAdapter.submits == 1


def test_authorization_cannot_be_supplied_by_a_capability_snapshot():
    from middleware.connector_sdk import CapabilityDisabledError

    _, _, request = fixture_runtime()
    fixture, _, _ = fixture_runtime()
    runtime = ConnectorRuntime(
        fixture.registry,
        StaticCapabilityProvider({(request.context.tenant_id, "EMAIL_DELIVERY"): True}),
        authorize=lambda _: False,
    )
    with pytest.raises(CapabilityDisabledError):
        runtime.execute(request)


@pytest.mark.parametrize(
    "command_type,version",
    [
        ("email.message.send.v2", 1),
        ("email.message.send", 1),
        ("email.message.send.v1", True),
    ],
)
def test_command_name_and_numeric_version_must_agree(command_type, version):
    _, runtime, request = fixture_runtime()
    with pytest.raises(CommandNotAllowedError):
        runtime.execute(
            replace(request, command_type=command_type, command_version=version)
        )


def test_readback_cannot_switch_a_known_provider_reference():
    class SwitchingReference(FakeAdapter):
        def execute_command(self, request):
            return replace(
                super().execute_command(request), provider_reference="provider-1"
            )

        def read_back(self, request, prior_result):
            return replace(
                super().read_back(request, prior_result),
                provider_reference="provider-2",
            )

    _, runtime, request = fixture_runtime(SwitchingReference)
    with pytest.raises(CommandNotAllowedError):
        runtime.execute(request)


@pytest.mark.parametrize("change", ["upgrade", "rebind"])
def test_cached_factory_handle_is_invalidated(change):
    from middleware.connector_sdk import ConnectorNotFoundError

    fixture, _, request = fixture_runtime()
    registry = fixture.registry
    record = registry.get(request.connector_id)
    cached = registry.adapter_factory(request.connector_id)
    if change == "upgrade":
        raw = fixture.raw(request.connector_id)
        raw["version"] = "1.1.0"
        registry.register_manifest(raw, replace=True)
    else:
        registry.register_adapter_factory(request.connector_id, FakeAdapter)
    with pytest.raises(ConnectorNotFoundError):
        cached(record.manifest)


def test_rebinding_during_retry_prevents_stale_adapter_submission():
    from middleware.connector_sdk import ConnectorStateError

    class RetryAdapter(FakeAdapter):
        submits = 0

        def execute_command(self, request):
            type(self).submits += 1
            return CommandResult(
                CommandOutcome.FAILED, request.command_id, retryable=True
            )

    fixture, runtime, request = fixture_runtime(RetryAdapter)
    runtime._sleep = lambda _: fixture.registry.register_adapter_factory(
        request.connector_id, FakeAdapter
    )
    with pytest.raises(ConnectorStateError):
        runtime.execute(request)
    assert RetryAdapter.submits == 1


def test_failed_upgrade_preserves_state_and_factory_atomically():
    fixture, _, request = fixture_runtime()
    registry = fixture.registry
    before = registry.list()
    factory = registry.adapter_factory(request.connector_id)
    raw = fixture.raw(request.connector_id)
    raw["version"] = "1.1.0"
    raw["commands"][0]["prefix"] = fixture.raw("klyrow-alert-email")["commands"][0][
        "prefix"
    ]
    with pytest.raises(ConnectorVersionConflictError):
        registry.register_manifest(raw, replace=True)
    assert registry.list() == before
    assert registry.adapter_factory(request.connector_id) is factory


def test_malformed_submission_is_journaled_as_unknown_before_recovery():
    class InvalidAdapter(FakeAdapter):
        submits = 0

        def execute_command(self, request):
            type(self).submits += 1
            return {"outcome": "COMPLETED"}

    _, runtime, request = fixture_runtime(InvalidAdapter)
    with pytest.raises(CommandNotAllowedError):
        runtime.execute(request)
    assert runtime.execute(request).outcome is CommandOutcome.COMPLETED
    assert InvalidAdapter.submits == 1


@pytest.mark.parametrize("reference", [[], {}, "provider-other"])
def test_malformed_settlement_reference_fails_closed(reference):
    class InvalidAdapter(FakeAdapter):
        def execute_command(self, request):
            return replace(
                super().execute_command(request), provider_reference="provider-first"
            )

        def read_back(self, request, prior_result):
            return replace(
                super().read_back(request, prior_result), provider_reference=reference
            )

    _, runtime, request = fixture_runtime(InvalidAdapter)
    with pytest.raises(CommandNotAllowedError):
        runtime.execute(request)


def test_readback_preserves_known_reference_when_provider_omits_it():
    class Adapter(FakeAdapter):
        def execute_command(self, request):
            return replace(
                super().execute_command(request), provider_reference="provider-first"
            )

    _, runtime, request = fixture_runtime(Adapter)
    assert runtime.execute(request).provider_reference == "provider-first"
