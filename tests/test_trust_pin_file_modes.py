"""Trust-pin replacement must preserve source file permissions."""
from __future__ import annotations

import runpy
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", [0o755, 0o644])
def test_atomic_apply_preserves_source_permissions(tmp_path: Path, mode: int) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts/derive_trust_pins.py"),
                              run_name="atomic_apply_mode_regression")
    target = tmp_path / "validator.py"
    target.write_bytes(b'#!/usr/bin/env python3\nVALUE = "before"\n')
    target.chmod(mode)
    proposed = b'#!/usr/bin/env python3\nVALUE = "after"\n'

    changed = namespace["atomic_apply"](tmp_path, {"validator.py": proposed})

    assert changed == ["validator.py"]
    assert target.read_bytes() == proposed
    assert stat.S_IMODE(target.stat().st_mode) == mode
