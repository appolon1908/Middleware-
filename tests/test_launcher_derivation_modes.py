"""Fail-closed transition and steady-state trust derivation regression tests."""

from __future__ import annotations

import pytest

from scripts.derive_trust_pins import DerivationError, project_protected_launcher

V = "a" * 64
F = "b" * 64
R = "c" * 64
O = "d" * 64


def steady(validator=V, fp=F, release=R, policy=None, extra=""):
    if policy is None:
        policy = """APPROVED_VALIDATOR_TRANSITIONS = {
    CURRENT_VALIDATOR_SHA256: {
        CURRENT_VALIDATOR_SHA256: (
            "security-fingerprint",
            CURRENT_RELEASE_SECURITY_FINGERPRINT,
        ),
    },
}"""
    return f'''CURRENT_VALIDATOR_SHA256 = ("{validator}")
CURRENT_RELEASE_VALIDATOR_SHA256 = ("{release}")
CURRENT_RELEASE_SECURITY_FINGERPRINT = ("{fp}")
{policy}
{extra}
'''


def transition(validator=O, fp="e" * 64):
    return f'''CURRENT_VALIDATOR_SHA256 = ("{O}")
SUCCESSOR_VALIDATOR_SHA256 = ("{validator}")
CURRENT_RELEASE_SECURITY_FINGERPRINT = ("{O}")
SUCCESSOR_RELEASE_SECURITY_FINGERPRINT = ("{fp}")
APPROVED_VALIDATOR_TRANSITIONS = {{}}
'''


def test_valid_steady_state_is_byte_identical_and_reports_explicit_mode():
    source = steady()
    projected, result = project_protected_launcher(source, V, F)
    assert projected == source
    assert result["mode"] == "steady-state"
    assert result["parity"] is True
    assert result["current_release_validator_in_tree"] == R
    assert result["successor_in_tree"] is None


def test_legacy_successor_transition_still_projects_candidate_digests():
    source = transition()
    projected, result = project_protected_launcher(source, V, F)
    assert result["mode"] == "transition"
    assert projected != source
    assert V in projected and F in projected
    assert result["parity"] is False


def test_known_current_pr448_steady_state_fingerprint_is_accepted():
    validator = "a0464ee3ac87ed1127d8ca349b821ab861d40451f595d74d40cd47e7f1f72dac"
    fingerprint = "4c2cba2fae0abccfce66b32c45e10f9a77509921a13435441096abdf216a8548"
    release = "cc9c3ad67fe91b116240c7300ff111e20b8884c56b73edb3497ab7138f94bf9f"
    source = steady(validator=validator, fp=fingerprint, release=release)
    projected, result = project_protected_launcher(source, validator, fingerprint)
    assert projected == source
    assert result["current_release_validator_in_tree"] == release


@pytest.mark.parametrize(
    "source,digest,fingerprint",
    [
        (steady(validator=O), V, F),
        (steady(fp=O), V, F),
        (steady(release="invalid"), V, F),
        (steady(extra=f'SUCCESSOR_VALIDATOR_SHA256 = ("{O}")'), V, F),
        (steady(extra='SUCCESSOR_VALIDATOR_SHA256 = ("not-a-digest")'), V, F),
        (steady(extra=f'SUCCESSOR_RELEASE_SECURITY_FINGERPRINT = ("{O}")'), V, F),
        (steady(policy="APPROVED_VALIDATOR_TRANSITIONS = {}"), V, F),
        (steady(policy="APPROVED_VALIDATOR_TRANSITIONS = {CURRENT_VALIDATOR_SHA256: {}}"), V, F),
        (
            steady(
                policy=f'APPROVED_VALIDATOR_TRANSITIONS = '
                f'{{CURRENT_VALIDATOR_SHA256: {{"{O}": '
                f'("security-fingerprint", CURRENT_RELEASE_SECURITY_FINGERPRINT)}}}}'
            ),
            V,
            F,
        ),
        (
            steady(
                policy='APPROVED_VALIDATOR_TRANSITIONS = '
                '{CURRENT_VALIDATOR_SHA256: {CURRENT_VALIDATOR_SHA256: '
                '("no-fingerprint", CURRENT_RELEASE_SECURITY_FINGERPRINT)}}'
            ),
            V,
            F,
        ),
        (steady(policy='APPROVED_VALIDATOR_TRANSITIONS = __import__("os").system("exit 1")'), V, F),
        (steady(), "not-a-sha256", F),
        (steady(), V, "bogus"),
        (steady(extra="APPROVED_VALIDATOR_TRANSITIONS = {}"), V, F),
        (steady(extra=f'CURRENT_VALIDATOR_SHA256 = ("{V}")'), V, F),
        (steady(extra=f'CURRENT_RELEASE_SECURITY_FINGERPRINT = ("{F}")'), V, F),
        (steady(extra=f'CURRENT_RELEASE_VALIDATOR_SHA256 = ("{R}")'), V, F),
    ],
)
def test_ambiguous_or_downgraded_launcher_fails_closed(source, digest, fingerprint):
    with pytest.raises(DerivationError):
        project_protected_launcher(source, digest, fingerprint)
