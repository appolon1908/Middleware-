"""Hash-locked requirements retain exact pins when dependency extras are present."""
from pathlib import Path

import pytest

from scripts.validate_release_supply_chain import validate_lock


@pytest.mark.parametrize("requirement,name", [
    ("pyjwt[crypto]==2.15.0", "pyjwt"),
    ("psycopg[binary]==3.3.3", "psycopg"),
    ("uvicorn[standard]==0.48.0", "uvicorn"),
    ("Example_Package[one,two]==1.2.3 ; python_version >= '3.10'", "example-package"),
])
def test_hashed_exact_pins_with_extras(tmp_path: Path, requirement: str, name: str):
    lock = tmp_path / "requirements.lock"
    lock.write_text(requirement + " \\\n    --hash=sha256:" + "a" * 64 + "\n")
    errors: list[str] = []
    packages = validate_lock(lock, errors)
    assert errors == []
    assert name in packages


@pytest.mark.parametrize("requirement", [
    "pyjwt[crypto]>=2.15.0", "pyjwt[]==2.15.0", "pyjwt[crypto]==2.15.0",
])
def test_extras_do_not_allow_unpinned_or_unhashed_entries(tmp_path: Path, requirement: str):
    lock = tmp_path / "requirements.lock"
    lock.write_text(requirement + "\n")
    errors: list[str] = []
    validate_lock(lock, errors)
    assert errors
