from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/codestra-hierarchy-governance.yml"


def test_required_ci_is_bound_to_development_department_control_plane() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    jobs = workflow["jobs"]

    assert "promotion-guard" in jobs
    assert "development-ci-admission" in jobs
    assert "control-plane-certification" in jobs
    assert "promotion-gate" in jobs

    admission = jobs["development-ci-admission"]
    assert admission["needs"] == "promotion-guard"
    admission_source = WORKFLOW.read_text()
    assert "codestra/required-ci" in admission_source
    assert "DEVELOPMENT_CI_ADMISSION=PASS" in admission_source

    control_plane = jobs["control-plane-certification"]
    assert set(control_plane["needs"]) == {
        "promotion-guard",
        "development-ci-admission",
    }
    assert "codestra-control-plane" in control_plane["runs-on"]

    promotion_gate = jobs["promotion-gate"]
    assert set(promotion_gate["needs"]) == {
        "promotion-guard",
        "development-ci-admission",
        "control-plane-certification",
    }
    assert promotion_gate["if"] == "${{ always() }}"
    assert "DEVELOPMENT_DEPARTMENT=PASS" in admission_source


def test_development_department_chain_is_fail_closed() -> None:
    source = WORKFLOW.read_text()
    assert 'test "${GUARD_RESULT}" = "success"' in source
    assert 'test "${CI_ADMISSION_RESULT}" = "success"' in source
    assert 'test "${CONTROL_PLANE_RESULT}" = "success"' in source
    assert "required_ci_timeout" in source
