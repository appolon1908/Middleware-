"""Executable design contract for M1-F01, the canonical application factory."""

from __future__ import annotations

import ast
from pathlib import Path

from app.application import AppProfile, create_app
from app.core.config import Settings
from app.router_registry import assert_unique_routes, route_operations

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "docs/design/M1-F01-APPLICATION-FACTORY.md"


def _test_settings() -> Settings:
    return Settings.from_env({"APP_ENV": "test", "ALLOW_IN_MEMORY_STORAGE": "true"})


def test_m1_f01_design_names_canonical_authorities() -> None:
    text = DESIGN.read_text(encoding="utf-8")
    for authority in (
        "app/factory.py",
        "app/application.py::create_app",
        "app/router_registry.py",
        "app/core/runtime.py",
        "app/core/request_guard.py",
        "app/core/health.py",
    ):
        assert authority in text


def test_factory_exposes_canonical_state_contract() -> None:
    settings = _test_settings()
    for profile in AppProfile:
        app = create_app(settings=settings, profile=profile)
        assert app.state.settings is settings
        assert app.state.profile is profile
        assert app.state.runtime_state.settings is settings
        assert app.state.runtime is None
        assert app.state.service
        assert app.state.observability is not None
        assert_unique_routes(app)


def test_profile_route_sets_are_declared_subsets_of_monolith() -> None:
    settings = _test_settings()
    operations = {
        profile: set(route_operations(create_app(settings=settings, profile=profile)))
        for profile in AppProfile
    }
    assert operations[AppProfile.INTEGRATION] < operations[AppProfile.MONOLITH]
    assert operations[AppProfile.CONTROL_PLANE] < operations[AppProfile.MONOLITH]
    assert operations[AppProfile.INTEGRATION] - operations[AppProfile.CONTROL_PLANE]
    assert operations[AppProfile.CONTROL_PLANE] - operations[AppProfile.INTEGRATION]


def test_primary_entrypoints_delegate_instead_of_constructing_fastapi() -> None:
    for relative in ("app/main.py", "app/entrypoints/integration_api.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        fastapi_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FastAPI"
        ]
        assert not fastapi_calls, relative
        assert "create_app" in source, relative


def test_only_the_factory_constructs_fastapi() -> None:
    constructors = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
                if name == "FastAPI":
                    constructors.append(path.relative_to(ROOT).as_posix())
    assert set(constructors) == {"app/factory.py"}, constructors
