#!/usr/bin/env python3
"""Validate Middleware lane file scopes (governance/file-ownership.v1.json).

Standard library only. Implements the five checks from the D1 ownership draft:

  (a) schema and glob validity of governance/file-ownership.v1.json
  (b) lane scopes are disjoint (after most-specific-match resolution; --strict: no
      tracked or declared path may be matched by two lanes at all)
  (c) every `git ls-files` path resolves to exactly one lane or to the explicit
      `unassigned` list
  (d) a lane branch's diff (`git diff --name-only --no-renames BASE...HEAD`, optionally
      plus uncommitted/untracked changes) stays inside the lane's resolved `owns`
      plus the shared files the lane currently holds
  (e) each lane's `review_authority` equals the union of CODEOWNERS owners of the
      paths it may write

Exit codes: 0 = PASS, 1 = check failure, 2 = usage/environment error.
Output: one `LANE_SCOPE_<CHECK>=PASS|FAIL` line per check run, then `ERROR`/`WARN` lines.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA_ID = "codestra.middleware.file-ownership.v1"
DEFAULT_POLICY = "governance/file-ownership.v1.json"
CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")

TOP_KEYS_REQUIRED = {"schema", "scope_version", "base", "max_concurrent_writers", "coverage", "lanes", "shared_files", "unassigned"}
TOP_KEYS_OPTIONAL = {"description", "base_sha", "approvals"}
LANE_KEYS_REQUIRED = {"id", "branch", "worktree", "status", "file_scope", "review_authority"}
LANE_KEYS_OPTIONAL = {"description", "mission", "d1_lane"}
FILE_SCOPE_KEYS_REQUIRED = {"owns"}
FILE_SCOPE_KEYS_OPTIONAL = {"reads", "symbols"}
SHARED_KEYS_REQUIRED = {"path", "owner", "handoff_order", "holder", "unit", "requires_parent_sha", "handoff_log"}
SHARED_KEYS_OPTIONAL = {"description", "symbols"}
HANDOFF_KEYS = {"lane", "pushed_sha", "recorded_at"}

LANE_STATUSES = {"PLANNED", "ACTIVE", "RELEASED"}
COVERAGE_MODES = {"complete", "partial"}
CATCH_ALL = {"**", "*", "**/*", "/**", "/*"}
LANE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(::[a-z0-9][a-z0-9-]*)+$")
BRANCH_RE = re.compile(r"^fix/[A-Za-z0-9][A-Za-z0-9._/-]*$")
OWNER_RE = re.compile(r"^@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:/[A-Za-z0-9._-]+)?$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
GLOB_CHARS = set("*?[")


class UsageError(Exception):
    """Environment or invocation problem (exit 2)."""


# --------------------------------------------------------------------------- globs


def glob_error(pattern: str) -> str | None:
    """Return a reason the pattern is invalid, or None."""
    if not isinstance(pattern, str) or not pattern:
        return "empty or non-string pattern"
    if pattern != pattern.strip():
        return "leading/trailing whitespace"
    if pattern.startswith("/") or pattern.startswith("./"):
        return "must be repo-root relative without leading '/' or './'"
    if "\\" in pattern:
        return "backslash not allowed"
    if "{" in pattern or "}" in pattern:
        return "brace expansion not supported; list each path"
    if pattern.startswith("!"):
        return "negation not supported"
    segments = pattern.rstrip("/").split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            return f"invalid path segment {seg!r}"
        if "**" in seg and seg != "**":
            return "'**' must be a whole path segment"
    depth = 0
    for ch in pattern:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if depth not in (0, 1):
            return "unbalanced character class"
    if depth != 0:
        return "unbalanced character class"
    try:
        compile_glob(pattern)
    except re.error as exc:  # pragma: no cover - defensive
        return f"does not compile: {exc}"
    return None


def _segment_regex(seg: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(seg):
        ch = seg[i]
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            j = seg.index("]", i + 1)
            body = seg[i + 1 : j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = j
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def compile_glob(pattern: str) -> re.Pattern[str]:
    """Compile a root-anchored gitignore-style glob.

    `*`/`?`/`[..]` never cross '/', `**` spans zero or more segments, a trailing '/'
    means "everything below". A pattern also matches every path below a directory it
    matches (gitignore semantics), so `app/adapters/n8n` covers `app/adapters/n8n/x.py`.
    """
    if pattern.endswith("/"):
        pattern = pattern + "**"
    segments = pattern.split("/")
    parts: list[str] = []
    for idx, seg in enumerate(segments):
        last = idx == len(segments) - 1
        if seg == "**":
            parts.append(".*" if last else "(?:[^/]+/)*")
        else:
            parts.append(_segment_regex(seg) + ("" if last else "/"))
    return re.compile("^" + "".join(parts) + "(?:/.*)?$")


def is_literal(pattern: str) -> bool:
    return not (GLOB_CHARS & set(pattern)) and not pattern.endswith("/")


def specificity(pattern: str) -> tuple[int, int, int]:
    """Higher sorts as more specific: literal path > glob; then literal prefix; then literal chars."""
    literal = 1 if is_literal(pattern) else 0
    prefix = 0
    for ch in pattern:
        if ch in GLOB_CHARS:
            break
        prefix += 1
    literal_chars = sum(1 for ch in pattern if ch not in GLOB_CHARS)
    return (literal, prefix, literal_chars)


# --------------------------------------------------------------------------- CODEOWNERS


@dataclass(frozen=True)
class CodeOwnersRule:
    pattern: str
    regex: re.Pattern[str]
    owners: tuple[str, ...]


def parse_codeowners(text: str) -> list[CodeOwnersRule]:
    rules: list[CodeOwnersRule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        pattern, owners = tokens[0], tuple(t for t in tokens[1:] if not t.startswith("#"))
        rules.append(CodeOwnersRule(pattern, _codeowners_regex(pattern), owners))
    return rules


def _codeowners_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.startswith("/")
    body = pattern.lstrip("/")
    inner = body.rstrip("/")
    if not anchored and "/" not in inner:
        body = "**/" + body  # bare names match at any depth (GitHub semantics)
    if body in ("", "**/"):
        body = "**"
    return compile_glob(body)


def codeowners_for(path: str, rules: Sequence[CodeOwnersRule]) -> tuple[str, ...] | None:
    """Last matching rule wins (GitHub semantics). None when no rule matches."""
    found: tuple[str, ...] | None = None
    for rule in rules:
        if rule.regex.match(path):
            found = rule.owners
    return found


# --------------------------------------------------------------------------- policy model


@dataclass
class Lane:
    id: str
    branch: str
    worktree: str
    status: str
    owns: list[str]
    review_authority: list[str]
    compiled: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)


@dataclass
class Shared:
    path: str
    owner: str
    handoff_order: list[str]
    holder: str | None
    requires_parent_sha: bool
    handoff_log: list[dict]


@dataclass
class Policy:
    raw: dict
    lanes: list[Lane]
    shared: list[Shared]
    unassigned: list[str]
    coverage: str
    max_concurrent_writers: int

    def lane(self, lane_id: str) -> Lane | None:
        return next((lane for lane in self.lanes if lane.id == lane_id), None)

    def lane_for_branch(self, branch: str) -> Lane | None:
        return next((lane for lane in self.lanes if lane.branch == branch), None)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def err(self, check: str, msg: str) -> None:
        self.errors.append(f"[{check}] {msg}")

    def warn(self, check: str, msg: str) -> None:
        self.warnings.append(f"[{check}] {msg}")

    def failed(self, check: str) -> bool:
        return any(e.startswith(f"[{check}]") for e in self.errors)


def _check_keys(obj: object, required: set[str], optional: set[str], where: str, rep: Report) -> bool:
    if not isinstance(obj, dict):
        rep.err("a", f"{where}: must be an object")
        return False
    missing = sorted(required - obj.keys())
    unknown = sorted(obj.keys() - required - optional)
    if missing:
        rep.err("a", f"{where}: missing keys {missing}")
    if unknown:
        rep.err("a", f"{where}: unknown keys {unknown}")
    return not missing


def _str_list(value: object, where: str, rep: Report, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        rep.err("a", f"{where}: must be a list of strings")
        return []
    if not allow_empty and not value:
        rep.err("a", f"{where}: must not be empty")
    if len(set(value)) != len(value):
        rep.err("a", f"{where}: duplicate entries")
    return list(value)


def check_schema(raw: object, rep: Report) -> Policy | None:
    """Check (a). Returns a Policy when it is structurally usable for later checks."""
    if not _check_keys(raw, TOP_KEYS_REQUIRED, TOP_KEYS_OPTIONAL, "policy", rep):
        return None
    assert isinstance(raw, dict)
    if raw["schema"] != SCHEMA_ID:
        rep.err("a", f"schema must be {SCHEMA_ID!r}")
    if raw["scope_version"] != 1:
        rep.err("a", "scope_version must be 1")
    if raw["base"] != "main":
        rep.err("a", "base must be 'main' (fix/* branches are cut from current main)")
    if "base_sha" in raw and not (isinstance(raw["base_sha"], str) and SHA_RE.match(raw["base_sha"])):
        rep.err("a", "base_sha must be a full 40-hex SHA")
    mcw = raw["max_concurrent_writers"]
    if not isinstance(mcw, int) or isinstance(mcw, bool) or not 1 <= mcw <= 8:
        rep.err("a", "max_concurrent_writers must be an integer in 1..8")
        mcw = 1
    coverage = raw["coverage"]
    if coverage not in COVERAGE_MODES:
        rep.err("a", f"coverage must be one of {sorted(COVERAGE_MODES)}")

    lanes: list[Lane] = []
    if not isinstance(raw["lanes"], list) or not raw["lanes"]:
        rep.err("a", "lanes must be a non-empty list")
    else:
        for i, item in enumerate(raw["lanes"]):
            where = f"lanes[{i}]"
            if not _check_keys(item, LANE_KEYS_REQUIRED, LANE_KEYS_OPTIONAL, where, rep):
                continue
            lane_id = item["id"]
            where = f"lane {lane_id!r}"
            if not isinstance(lane_id, str) or not LANE_ID_RE.match(lane_id):
                rep.err("a", f"{where}: id must look like 'group::name'")
            if not isinstance(item["branch"], str) or not BRANCH_RE.match(item["branch"]):
                rep.err("a", f"{where}: branch must be a fix/* branch")
            if not isinstance(item["worktree"], str) or not item["worktree"].startswith("/"):
                rep.err("a", f"{where}: worktree must be an absolute path")
            if item["status"] not in LANE_STATUSES:
                rep.err("a", f"{where}: status must be one of {sorted(LANE_STATUSES)}")
            fs = item["file_scope"]
            owns: list[str] = []
            if _check_keys(fs, FILE_SCOPE_KEYS_REQUIRED, FILE_SCOPE_KEYS_OPTIONAL, f"{where}.file_scope", rep):
                owns = _str_list(fs["owns"], f"{where}.file_scope.owns", rep, allow_empty=False)
                for key in ("reads",):
                    if key in fs:
                        for pat in _str_list(fs[key], f"{where}.file_scope.{key}", rep):
                            reason = glob_error(pat)
                            if reason:
                                rep.err("a", f"{where}.file_scope.{key}: {pat!r}: {reason}")
            ra = _str_list(item["review_authority"], f"{where}.review_authority", rep, allow_empty=False)
            for owner in ra:
                if not OWNER_RE.match(owner):
                    rep.err("a", f"{where}.review_authority: {owner!r} is not an @owner")
            lane = Lane(lane_id, item["branch"], item["worktree"], item["status"], owns, ra)
            for pat in owns:
                reason = glob_error(pat)
                if reason:
                    rep.err("a", f"{where}.owns: {pat!r}: {reason}")
                elif pat in CATCH_ALL:
                    rep.err("a", f"{where}.owns: catch-all {pat!r} is not a scope")
                else:
                    lane.compiled.append((pat, compile_glob(pat)))
            lanes.append(lane)

    for key, label in (("id", "lane id"), ("branch", "branch"), ("worktree", "worktree")):
        values = [getattr(lane, key) for lane in lanes]
        dups = sorted({v for v in values if values.count(v) > 1})
        if dups:
            rep.err("a", f"duplicate {label}: {dups} (each writer needs its own worktree and fix/* branch)")

    active = [lane.id for lane in lanes if lane.status == "ACTIVE"]
    if len(active) > mcw:
        rep.err("a", f"{len(active)} ACTIVE lanes exceed max_concurrent_writers={mcw}: {active}")

    lane_ids = {lane.id for lane in lanes}
    shared: list[Shared] = []
    if not isinstance(raw["shared_files"], list):
        rep.err("a", "shared_files must be a list")
    else:
        for i, item in enumerate(raw["shared_files"]):
            where = f"shared_files[{i}]"
            if not _check_keys(item, SHARED_KEYS_REQUIRED, SHARED_KEYS_OPTIONAL, where, rep):
                continue
            path = item["path"]
            where = f"shared {path!r}"
            if not isinstance(path, str) or glob_error(path) or not is_literal(path):
                rep.err("a", f"{where}: path must be a single literal repo path")
            order = _str_list(item["handoff_order"], f"{where}.handoff_order", rep, allow_empty=False)
            for lid in order:
                if lid not in lane_ids:
                    rep.err("a", f"{where}: handoff_order names unknown lane {lid!r}")
            if item["owner"] not in lane_ids:
                rep.err("a", f"{where}: owner {item['owner']!r} is not a declared lane")
            if order and item["owner"] != order[0]:
                rep.err("a", f"{where}: handoff_order must start with the owner")
            holder = item["holder"]
            if holder is not None and holder not in order:
                rep.err("a", f"{where}: holder {holder!r} is not in handoff_order")
            if item["unit"] not in ("file", "symbol"):
                rep.err("a", f"{where}: unit must be 'file' or 'symbol'")
            if not isinstance(item["requires_parent_sha"], bool):
                rep.err("a", f"{where}: requires_parent_sha must be boolean")
            log = item["handoff_log"]
            if not isinstance(log, list):
                rep.err("a", f"{where}: handoff_log must be a list")
                log = []
            for j, entry in enumerate(log):
                if not _check_keys(entry, HANDOFF_KEYS, set(), f"{where}.handoff_log[{j}]", rep):
                    continue
                if entry["lane"] not in order:
                    rep.err("a", f"{where}.handoff_log[{j}]: lane not in handoff_order")
                if not (isinstance(entry["pushed_sha"], str) and SHA_RE.match(entry["pushed_sha"])):
                    rep.err("a", f"{where}.handoff_log[{j}]: pushed_sha must be a full 40-hex SHA")
            if holder in order and item["requires_parent_sha"] is True:
                pos = order.index(holder)
                released = {e.get("lane") for e in log if isinstance(e, dict)}
                missing = [lid for lid in order[:pos] if lid not in released]
                if missing:
                    rep.err("a", f"{where}: holder {holder!r} lacks recorded pushed SHA from {missing}")
            shared.append(Shared(path, item["owner"], order, holder, bool(item["requires_parent_sha"]), log))
        paths = [s.path for s in shared]
        dups = sorted({p for p in paths if paths.count(p) > 1})
        if dups:
            rep.err("a", f"shared_files lists a path more than once: {dups}")

    unassigned = _str_list(raw["unassigned"], "unassigned", rep)
    for pat in unassigned:
        reason = glob_error(pat)
        if reason:
            rep.err("a", f"unassigned: {pat!r}: {reason}")
        elif pat in CATCH_ALL and coverage == "complete":
            rep.err("a", f"unassigned: catch-all {pat!r} is only allowed with coverage='partial'")

    if rep.failed("a") and not lanes:
        return None
    return Policy(raw, lanes, shared, unassigned, coverage if coverage in COVERAGE_MODES else "complete", mcw)


# --------------------------------------------------------------------------- resolution


def lane_matches(path: str, policy: Policy) -> list[tuple[tuple[int, int, int], str, str]]:
    """All (specificity, lane_id, pattern) matches of path, most specific first."""
    hits = []
    for lane in policy.lanes:
        best = None
        for pat, rx in lane.compiled:
            if rx.match(path):
                spec = specificity(pat)
                if best is None or spec > best[0]:
                    best = (spec, lane.id, pat)
        if best:
            hits.append(best)
    hits.sort(reverse=True)
    return hits


def resolve(path: str, policy: Policy) -> tuple[str | None, str | None]:
    """Return (lane_id, conflict_description). lane_id None means no lane owns it."""
    hits = lane_matches(path, policy)
    if not hits:
        return None, None
    if len(hits) > 1 and hits[0][0] == hits[1][0]:
        tied = [f"{lid}:{pat}" for spec, lid, pat in hits if spec == hits[0][0]]
        return None, f"equal-specificity owners {tied}"
    return hits[0][1], None


def declared_paths(policy: Policy) -> list[str]:
    paths = {pat for lane in policy.lanes for pat, _ in lane.compiled if is_literal(pat)}
    paths |= {s.path for s in policy.shared}
    return sorted(paths)


def check_disjoint(policy: Policy, tracked: Iterable[str], rep: Report, strict: bool = False) -> None:
    """Check (b): no path is owned by two lanes (ties always fail; any overlap fails in strict)."""
    seen_patterns: dict[str, str] = {}
    for lane in policy.lanes:
        for pat, _ in lane.compiled:
            if pat in seen_patterns and seen_patterns[pat] != lane.id:
                rep.err("b", f"pattern {pat!r} declared by both {seen_patterns[pat]!r} and {lane.id!r}")
            seen_patterns.setdefault(pat, lane.id)
    candidates = sorted(set(tracked) | set(declared_paths(policy)))
    for path in candidates:
        hits = lane_matches(path, policy)
        if len(hits) < 2:
            continue
        lanes = [f"{lid}:{pat}" for _, lid, pat in hits]
        if strict:
            rep.err("b", f"{path}: matched by more than one lane {lanes}")
        elif hits[0][0] == hits[1][0]:
            rep.err("b", f"{path}: equal-specificity overlap {lanes}")
        else:
            rep.warn("b", f"{path}: resolved to {hits[0][1]!r} by specificity over {lanes[1:]}")
    for s in policy.shared:
        owner, conflict = resolve(s.path, policy)
        if conflict:
            continue  # already reported above
        if owner != s.owner:
            rep.err("b", f"shared {s.path!r}: declared owner {s.owner!r} but owns resolves to {owner!r}")


def check_coverage(policy: Policy, tracked: Sequence[str], rep: Report) -> dict[str, int]:
    """Check (c): every tracked path resolves to a lane or matches `unassigned`."""
    unassigned_rx = [(p, compile_glob(p)) for p in policy.unassigned if not glob_error(p)]
    counts = {"tracked": 0, "lane": 0, "unassigned": 0, "unmapped": 0}
    for path in tracked:
        counts["tracked"] += 1
        lane_id, conflict = resolve(path, policy)
        if conflict:
            rep.err("c", f"{path}: {conflict}")
            counts["unmapped"] += 1
        elif lane_id:
            counts["lane"] += 1
        elif any(rx.match(path) for _, rx in unassigned_rx):
            counts["unassigned"] += 1
        else:
            rep.err("c", f"{path}: maps to no lane and is not in unassigned")
            counts["unmapped"] += 1
    tracked_set = set(tracked)
    for lane in policy.lanes:
        for pat, rx in lane.compiled:
            if not any(rx.match(p) for p in tracked_set):
                rep.warn("c", f"lane {lane.id!r}: owns pattern {pat!r} matches no tracked file (new file?)")
    for s in policy.shared:
        if s.path not in tracked_set:
            rep.warn("c", f"shared {s.path!r} is not a tracked file")
    if policy.coverage == "partial":
        rep.warn("c", f"coverage=partial: {counts['unassigned']} tracked paths fall to the unassigned list")
    return counts


def writable_by(path: str, lane: Lane, policy: Policy) -> tuple[bool, str]:
    shared = next((s for s in policy.shared if s.path == path), None)
    if shared is not None:
        if shared.holder == lane.id:
            return True, "held shared file"
        return False, f"shared file currently held by {shared.holder!r} (handoff order {shared.handoff_order})"
    owner, conflict = resolve(path, policy)
    if conflict:
        return False, conflict
    if owner == lane.id:
        return True, "owned"
    if owner is None:
        return False, "unassigned / no lane owns it"
    return False, f"owned by {owner!r}"


def check_diff(policy: Policy, lane: Lane, changed: Sequence[str], rep: Report) -> None:
    """Check (d): every changed path is writable by the lane."""
    if lane.status != "ACTIVE":
        rep.err("d", f"lane {lane.id!r} has status {lane.status}; only ACTIVE lanes may write")
    for path in sorted(set(changed)):
        ok, why = writable_by(path, lane, policy)
        if not ok:
            rep.err("d", f"{path}: outside lane {lane.id!r} scope ({why})")


def check_review_authority(policy: Policy, tracked: Sequence[str], rules: Sequence[CodeOwnersRule], rep: Report) -> dict[str, list[str]]:
    """Check (e): review_authority equals the union of CODEOWNERS owners of writable paths."""
    derived: dict[str, list[str]] = {}
    universe = sorted(set(tracked) | set(declared_paths(policy)))
    for lane in policy.lanes:
        owners: set[str] = set()
        paths = [p for p in universe if resolve(p, policy)[0] == lane.id]
        paths += [s.path for s in policy.shared if lane.id in s.handoff_order]
        for path in sorted(set(paths)):
            found = codeowners_for(path, rules)
            if not found:
                rep.err("e", f"lane {lane.id!r}: {path} has no CODEOWNERS owner")
                continue
            owners.update(found)
        derived[lane.id] = sorted(owners)
        declared = set(lane.review_authority)
        if declared != owners:
            missing = sorted(owners - declared)
            extra = sorted(declared - owners)
            rep.err("e", f"lane {lane.id!r}: review_authority {sorted(declared)} != CODEOWNERS {sorted(owners)} (missing {missing}, extra {extra})")
    return derived


# --------------------------------------------------------------------------- git helpers


def git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise UsageError("git not found") from exc
    except subprocess.CalledProcessError as exc:
        raise UsageError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return proc.stdout


def tracked_files(root: Path) -> list[str]:
    return [p for p in git(root, "ls-files", "-z").split("\0") if p]


def changed_files(root: Path, base: str, include_worktree: bool) -> list[str]:
    out = git(root, "diff", "--name-only", "--no-renames", "-z", f"{base}...HEAD")
    paths = {p for p in out.split("\0") if p}
    if include_worktree:
        paths |= {p for p in git(root, "diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0") if p}
        paths |= {p for p in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if p}
    return sorted(paths)


def read_codeowners(root: Path) -> list[CodeOwnersRule]:
    for loc in CODEOWNERS_LOCATIONS:
        candidate = root / loc
        if candidate.is_file():
            return parse_codeowners(candidate.read_text(encoding="utf-8"))
    raise UsageError("no CODEOWNERS file found")


def load_policy(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UsageError(f"policy not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"policy is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------------- CLI


def run(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="repository root (default: .)")
    ap.add_argument("--policy", default=None, help=f"policy file (default: <root>/{DEFAULT_POLICY})")
    ap.add_argument("--strict", action="store_true", help="(b): fail on any multi-lane match, not only ties")
    ap.add_argument("--lane", help="(d): lane id to check the branch diff against")
    ap.add_argument("--branch", help="(d): select lane by branch (default: current branch when --diff)")
    ap.add_argument("--diff", action="store_true", help="run check (d) for the selected/current lane")
    ap.add_argument("--base", default="origin/main", help="(d): diff base (default: origin/main)")
    ap.add_argument("--include-worktree", action="store_true", help="(d): include uncommitted and untracked paths")
    ap.add_argument("--require-current-base", action="store_true", help="(d): branch must contain the tip of --base")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    policy_path = Path(args.policy) if args.policy else root / DEFAULT_POLICY
    rep = Report()
    try:
        raw = load_policy(policy_path)
        policy = check_schema(raw, rep)
        print(f"LANE_SCOPE_SCHEMA={'FAIL' if rep.failed('a') else 'PASS'}")
        if policy is None:
            return _finish(rep)
        tracked = tracked_files(root)
        check_disjoint(policy, tracked, rep, strict=args.strict)
        print(f"LANE_SCOPE_DISJOINT={'FAIL' if rep.failed('b') else 'PASS'}")
        counts = check_coverage(policy, tracked, rep)
        print(f"LANE_SCOPE_COVERAGE={'FAIL' if rep.failed('c') else 'PASS'} " + " ".join(f"{k}={v}" for k, v in counts.items()))
        if args.diff or args.lane:
            lane = policy.lane(args.lane) if args.lane else None
            if lane is None:
                branch = args.branch or git(root, "branch", "--show-current").strip()
                lane = policy.lane_for_branch(branch)
                if lane is None:
                    rep.err("d", f"branch {branch!r} is not a declared lane branch")
            if lane is not None:
                current = git(root, "branch", "--show-current").strip()
                if current and current != lane.branch and not args.branch:
                    rep.err("d", f"checked-out branch {current!r} is not lane branch {lane.branch!r}")
                if args.require_current_base:
                    tip = git(root, "rev-parse", args.base).strip()
                    mb = git(root, "merge-base", args.base, "HEAD").strip()
                    if mb != tip:
                        rep.err("d", f"branch is not based on current {args.base} ({tip[:12]}); rebase first")
                changed = changed_files(root, args.base, args.include_worktree)
                check_diff(policy, lane, changed, rep)
                print(f"LANE_SCOPE_DIFF={'FAIL' if rep.failed('d') else 'PASS'} lane={lane.id} changed={len(changed)}")
        rules = read_codeowners(root)
        check_review_authority(policy, tracked, rules, rep)
        print(f"LANE_SCOPE_REVIEW_AUTHORITY={'FAIL' if rep.failed('e') else 'PASS'}")
    except UsageError as exc:
        print(f"LANE_SCOPE=ERROR {exc}", file=sys.stderr)
        return 2
    return _finish(rep)


def _finish(rep: Report) -> int:
    for w in rep.warnings:
        print(f"WARN {w}")
    for e in rep.errors:
        print(f"ERROR {e}")
    print(f"LANE_SCOPE={'FAIL' if rep.errors else 'PASS'}")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(run())
