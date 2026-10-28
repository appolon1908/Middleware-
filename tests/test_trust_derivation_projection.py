"""Compare proposed trust-tree closure with the bytes candidate apply writes."""
import contextlib
import hashlib
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest

import pytest


ROOT = Path(__file__).resolve().parents[1]
DERIVATION = runpy.run_path(str(ROOT / "scripts/derive_trust_pins.py"))
RELEASE_VALIDATOR = runpy.run_path(str(ROOT / ".codestra/validate-release-intent.py"))


@pytest.fixture
def crlf_checkout(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "core.autocrlf", "true")
    git(tmp_path, "config", "user.name", "Index Bytes Regression")
    git(tmp_path, "config", "user.email", "index-bytes@example.invalid")
    paths = (*DERIVATION["TRUST_FILES"], "Dockerfile.runtime")
    for rel in paths:
        write(tmp_path, rel, b"VALUE = 1\n")
    git(tmp_path, "add", "--all")
    git(tmp_path, "commit", "-q", "-m", "canonical LF inputs")
    for rel in paths:
        write(tmp_path, rel, b"VALUE = 1\r\n")
    return tmp_path


@pytest.mark.parametrize("rel", (*DERIVATION["TRUST_FILES"], "Dockerfile.runtime"))
def test_default_source_reads_staged_lf_despite_crlf_checkout(crlf_checkout, rel):
    source = DERIVATION["Source"](crlf_checkout, False)
    assert source.read(rel) == b"VALUE = 1\n"
    assert DERIVATION["Source"](crlf_checkout, True).read(rel) == b"VALUE = 1\r\n"


def test_index_digest_passes_strict_raw_binding_and_crlf_is_rejected(
    crlf_checkout, monkeypatch,
):
    source = DERIVATION["Source"](crlf_checkout, False)
    digest = DERIVATION["Derivation"](source).file_digest("Dockerfile.runtime")
    assert digest == hashlib.sha256(b"VALUE = 1\n").hexdigest()
    validate = RELEASE_VALIDATOR["validate_workflow_executable_bytes"]
    monkeypatch.setitem(validate.__globals__, "EXPECTED_CHECK_WORKFLOW_EXECUTABLE_SHA256", {
        "fixture/repo": {"fixture.yml": {"Dockerfile.runtime": digest}},
    })
    raw = subprocess.check_output(["git", "show", ":Dockerfile.runtime"], cwd=crlf_checkout)
    validate("fixture/repo", "fixture.yml", "Dockerfile.runtime", raw)
    with pytest.raises(RELEASE_VALIDATOR["PolicyError"], match="executable drift"):
        validate("fixture/repo", "fixture.yml", "Dockerfile.runtime", b"VALUE = 1\r\n")


def test_apply_rejects_development_worktree_mode_before_derivation(
    crlf_checkout, monkeypatch, capsys,
):
    main = DERIVATION["main"]

    def forbidden_derivation(*args):
        raise AssertionError("development input reached candidate derivation")

    monkeypatch.setitem(main.__globals__, "Derivation", forbidden_derivation)
    with pytest.raises(SystemExit) as caught:
        main(["--apply-candidate", "--allow-dirty", "--root", str(crlf_checkout)])
    assert caught.value.code == 2
    assert "staged Git bytes" in capsys.readouterr().err
    assert (crlf_checkout / DERIVATION["VALIDATOR"]).read_bytes() == b"VALUE = 1\r\n"


@pytest.mark.parametrize("mode", ["--check", "--apply-candidate"])
def test_index_mode_preserves_unstaged_work_before_derivation(
    crlf_checkout, monkeypatch, capsys, mode,
):
    initial_index = git(crlf_checkout, "write-tree")
    write(crlf_checkout, "Dockerfile.runtime", b"VALUE = 2\r\n")
    main = DERIVATION["main"]

    def forbidden_derivation(*args):
        raise AssertionError("unstaged work reached candidate derivation")

    monkeypatch.setitem(main.__globals__, "Derivation", forbidden_derivation)
    assert main([mode, "--root", str(crlf_checkout)]) == 2
    assert "unstaged changes" in capsys.readouterr().err
    assert (crlf_checkout / "Dockerfile.runtime").read_bytes() == b"VALUE = 2\r\n"
    assert git(crlf_checkout, "write-tree") == initial_index



def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def closure(root: Path, tree: str) -> str:
    with contextlib.chdir(root):
        entries = RELEASE_VALIDATOR["local_source_tree_entries"](tree)
    return RELEASE_VALIDATOR["source_closure_fingerprint"](
        entries, DERIVATION["REPOSITORY"]
    )


class TrustProjectionTests(unittest.TestCase):
    @contextlib.contextmanager
    def repository(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            git(root, "init", "-q")
            git(root, "config", "user.name", "Projection Regression")
            git(root, "config", "user.email", "projection@example.invalid")
            write(root, DERIVATION["LAUNCHER"], b"PROTECTED_LAUNCHER = True\n")
            write(root, "candidate.py", b"VALUE = 1\n")
            write(root, DERIVATION["RELEASE"], b"CLOSURE = None\n")
            git(root, "add", "--all")
            git(root, "commit", "-q", "-m", "initial exact source")
            yield root

    def test_projection_matches_actual_apply_and_preserves_protected_launcher(self):
        for allow_dirty in (False, True):
            with self.subTest(allow_dirty=allow_dirty), self.repository() as root:
                launcher = DERIVATION["LAUNCHER"]
                protected = (root / launcher).read_bytes()
                initial_index = git(root, "write-tree")
                proposed = {
                    launcher: b"PROTECTED_LAUNCHER = False\n",
                    "candidate.py": b"VALUE = 2\n",
                }
                derivation = DERIVATION["Derivation"](
                    DERIVATION["Source"](root, allow_dirty)
                )
                projected = derivation.projected_tree(proposed)
                self.assertEqual(git(root, "write-tree"), initial_index)
                self.assertEqual((root / launcher).read_bytes(), protected)
                changed = DERIVATION["atomic_apply"](root, proposed)
                self.assertEqual(changed, ["candidate.py"])
                self.assertEqual((root / launcher).read_bytes(), protected)
                self.assertEqual((root / "candidate.py").read_bytes(), proposed["candidate.py"])
                git(root, "add", "--all")
                self.assertEqual(projected, git(root, "write-tree"))

    def test_embedded_closure_matches_actual_index_after_apply(self):
        for allow_dirty in (False, True):
            with self.subTest(allow_dirty=allow_dirty), self.repository() as root:
                launcher = DERIVATION["LAUNCHER"]
                release = DERIVATION["RELEASE"]
                protected = (root / launcher).read_bytes()
                proposed = {
                    launcher: b"PROTECTED_LAUNCHER = False\n",
                    "candidate.py": b"VALUE = 2\n",
                    release: b"CLOSURE = None\n",
                }
                derivation = DERIVATION["Derivation"](
                    DERIVATION["Source"](root, allow_dirty)
                )
                expected = closure(root, derivation.projected_tree(proposed))
                proposed[release] = (
                    f"EXPECTED_REQUIRED_CHECK_SOURCE_CLOSURE_SHA256 = "
                    f"{{{DERIVATION['REPOSITORY']!r}: {expected!r}}}\n"
                ).encode()
                # Embedding the pin cannot change its normalized closure.
                self.assertEqual(closure(root, derivation.projected_tree(proposed)), expected)
                DERIVATION["atomic_apply"](root, proposed)
                git(root, "add", "--all")
                actual = closure(root, git(root, "write-tree"))
                embedded = runpy.run_path(str(root / release))[
                    "EXPECTED_REQUIRED_CHECK_SOURCE_CLOSURE_SHA256"
                ][DERIVATION["REPOSITORY"]]
                self.assertEqual((root / launcher).read_bytes(), protected)
                self.assertEqual(actual, embedded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
