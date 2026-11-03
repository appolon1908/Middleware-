"""Compare proposed trust-tree closure with the bytes candidate apply writes."""
import contextlib
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DERIVATION = runpy.run_path(str(ROOT / "scripts/derive_trust_pins.py"))
RELEASE_VALIDATOR = runpy.run_path(str(ROOT / ".codestra/validate-release-intent.py"))


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
