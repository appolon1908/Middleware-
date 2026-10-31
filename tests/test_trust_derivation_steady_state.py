"""One-way, independent trust authorization remains enforced in pin derivation."""
from __future__ import annotations

import importlib.util
import hashlib
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "derive_trust_pins.py"
LAUNCHER = ROOT / ".codestra" / "run-trusted-production-orchestrator.py"
VALIDATOR = ROOT / ".codestra" / "validate-production-orchestrator-contract.py"
RELEASE = ROOT / ".codestra" / "validate-release-intent.py"


def _under_test():
    spec = importlib.util.spec_from_file_location("trust_derivation_steady_state", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _authority():
    launcher = runpy.run_path(str(LAUNCHER), run_name="verify_launcher_policy")
    validator_sha = hashlib.sha256(VALIDATOR.read_bytes()).hexdigest()
    validator = runpy.run_path(str(VALIDATOR), run_name="verify_validator")
    fingerprint = validator["release_validator_security_fingerprint"](
        RELEASE.read_text(encoding="utf-8")
    )
    return launcher, validator_sha, fingerprint


def test_current_steady_state_is_exact_one_way_self_edge() -> None:
    derivation = _under_test()
    launcher, validator_sha, fingerprint = _authority()
    derivation.validate_steady_state_launcher(
        launcher,
        validator_sha256=validator_sha,
        fingerprint=fingerprint,
    )
    assert set(launcher["APPROVED_VALIDATOR_TRANSITIONS"]) == {validator_sha}
    assert set(launcher["APPROVED_VALIDATOR_TRANSITIONS"][validator_sha]) == {validator_sha}


@pytest.mark.parametrize("tamper", ["validator", "fingerprint", "release", "rollback"])
def test_nonapproved_steady_state_fails_closed(tamper: str) -> None:
    derivation = _under_test()
    launcher, validator_sha, fingerprint = _authority()
    # runpy also exposes imported module objects; copy only the policy data.
    launcher = dict(launcher)
    launcher["APPROVED_VALIDATOR_TRANSITIONS"] = {
        key: dict(value)
        for key, value in launcher["APPROVED_VALIDATOR_TRANSITIONS"].items()
    }
    if tamper == "validator":
        validator_sha = "0" * 64
    elif tamper == "fingerprint":
        fingerprint = "0" * 64
    elif tamper == "release":
        source = RELEASE.read_text(encoding="utf-8")
        assert source.count("if not condition:") == 1
        changed = source.replace("if not condition:", "if False:", 1)
        validator = runpy.run_path(str(VALIDATOR), run_name="release_tamper_probe")
        fingerprint = validator["release_validator_security_fingerprint"](changed)
    else:
        launcher["APPROVED_VALIDATOR_TRANSITIONS"][validator_sha]["1" * 64] = (
            "security-fingerprint", fingerprint
        )
    with pytest.raises(derivation.DerivationError):
        derivation.validate_steady_state_launcher(
            launcher,
            validator_sha256=validator_sha,
            fingerprint=fingerprint,
        )


def test_closure_only_change_retains_approved_release_fingerprint() -> None:
    derivation = _under_test()
    launcher, validator_sha, fingerprint = _authority()
    launcher_bytes = LAUNCHER.read_bytes()
    release = RELEASE.read_text(encoding="utf-8")
    namespace = runpy.run_path(str(RELEASE), run_name="release_binding_probe")
    closure = namespace["EXPECTED_REQUIRED_CHECK_SOURCE_CLOSURE_SHA256"][
        "appolon1908/Middleware-"
    ]
    changed, count = derivation.replace_digest(release, closure, "1" * 64)
    assert count == 1
    assert hashlib.sha256(changed.encode()).hexdigest() != hashlib.sha256(
        release.encode()
    ).hexdigest()
    validator = runpy.run_path(str(VALIDATOR), run_name="approved_fingerprint_probe")
    observed = validator["release_validator_security_fingerprint"](changed)
    assert observed == fingerprint
    derivation.validate_steady_state_launcher(
        launcher,
        validator_sha256=validator_sha,
        fingerprint=observed,
    )
    assert LAUNCHER.read_bytes() == launcher_bytes
