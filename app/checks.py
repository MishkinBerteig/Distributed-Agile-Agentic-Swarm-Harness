"""Deterministic acceptance-criterion checks against a swarm workspace directory.

Reads files only (never writes). Paths are confined to the workspace: absolute
paths, `..` components and symlinks that resolve outside are failed checks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional

from app.models import DETERMINISTIC_CHECKS, AcceptanceCriterion, CheckKind

MAX_READ_BYTES = 2 * 1024 * 1024
EVIDENCE_MAX_CHARS = 200


@dataclass
class CheckResult:
    passed: bool
    evidence: str


def _kind(kind) -> str:
    return getattr(kind, "value", kind)


def is_deterministic(kind: str) -> bool:
    return _kind(kind) in DETERMINISTIC_CHECKS


def _truncate(s: str, n: int = EVIDENCE_MAX_CHARS) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _path_problem(path: Optional[str]) -> Optional[str]:
    """Static (no filesystem) reason a relative workspace path is unacceptable."""
    if path is None or not path.strip():
        return "no path was given"
    if "\x00" in path:
        return "path contains a NUL character"
    if (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
            or path.startswith(("/", "\\"))):
        return f"absolute path '{path}' is not allowed; use a path relative to the workspace"
    parts = re.split(r"[\\/]", path)
    if ".." in parts:
        return f"path '{path}' contains '..' and could escape the workspace"
    return None


def validate_check_spec(kind: str, path: Optional[str], arg: Optional[str]) -> Optional[str]:
    """An error message if the check spec is unusable, else None."""
    kind = _kind(kind)
    valid = {k.value for k in CheckKind}
    if kind not in valid:
        return f"Unknown check kind '{kind}'. Valid kinds: {', '.join(sorted(valid))}."
    if kind == CheckKind.RUBRIC.value:
        if path:
            problem = _path_problem(path)
            if problem:
                return f"Rubric check path invalid: {problem}."
        return None
    problem = _path_problem(path)
    if problem:
        return f"Check '{kind}' needs a valid path: {problem}."
    if kind == CheckKind.FILE_CONTAINS.value:
        if arg is None or arg == "":
            return "Check 'file_contains' needs a regular expression in check_arg."
        try:
            re.compile(arg)
        except re.error as exc:
            return f"Check 'file_contains' has an invalid regular expression: {exc}."
    elif kind == CheckKind.MIN_WORDS.value:
        try:
            n = int(str(arg).strip()) if arg is not None else 0
        except ValueError:
            n = 0
        if n <= 0:
            return "Check 'min_words' needs check_arg to be a positive integer."
    return None


def _resolve(workspace: Path, path: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
    problem = _path_problem(path)
    if problem:
        return None, problem
    root = Path(workspace).resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        return None, f"path '{path}' resolves outside the workspace (symlink escape)"
    return target, None


def _read_text(target: Path) -> tuple[Optional[str], bool, Optional[str]]:
    """(text, truncated, error); reads at most MAX_READ_BYTES."""
    try:
        with target.open("rb") as fh:
            data = fh.read(MAX_READ_BYTES + 1)
    except OSError as exc:
        return None, False, f"could not read file: {exc.strerror or exc}"
    truncated = len(data) > MAX_READ_BYTES
    return data[:MAX_READ_BYTES].decode("utf-8", errors="replace"), truncated, None


def run_check(criterion: AcceptanceCriterion, workspace: Path) -> CheckResult:
    """Run one deterministic check. Raises ValueError only for rubric/unknown kinds."""
    kind = _kind(criterion.check_kind)
    if kind == CheckKind.RUBRIC.value or kind not in DETERMINISTIC_CHECKS:
        raise ValueError(f"Check kind '{kind}' is not deterministic and cannot be run by code.")
    path, arg = criterion.check_path, criterion.check_arg

    spec_error = validate_check_spec(kind, path, arg)
    if spec_error:
        return CheckResult(False, spec_error)

    target, problem = _resolve(workspace, path)
    if problem:
        return CheckResult(False, f"Rejected: {problem}.")

    if not target.exists():
        return CheckResult(False, f"File '{path}' does not exist.")
    if not target.is_file():
        return CheckResult(False, f"'{path}' is not a regular file.")
    if kind == CheckKind.FILE_EXISTS.value:
        return CheckResult(True, f"File '{path}' exists ({target.stat().st_size} bytes).")

    text, truncated, err = _read_text(target)
    if err:
        return CheckResult(False, f"'{path}': {err}.")
    note = " (only the first 2 MB were read)" if truncated else ""

    if kind == CheckKind.FILE_CONTAINS.value:
        try:
            m = re.compile(arg, re.IGNORECASE | re.MULTILINE).search(text)
        except re.error as exc:  # already validated; defensive
            return CheckResult(False, f"Invalid regular expression: {exc}.")
        if not m:
            return CheckResult(False, f"No match for /{_truncate(arg, 80)}/ in '{path}'{note}.")
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.start())
        line = text[start: end if end != -1 else len(text)]
        return CheckResult(True, _truncate(line))

    if kind == CheckKind.MIN_WORDS.value:
        needed = int(str(arg).strip())
        count = len(text.split())
        ok = count >= needed
        return CheckResult(ok, f"'{path}' has {count} words; {needed} required{note}.")

    # json_valid
    if truncated:
        return CheckResult(False, f"'{path}' exceeds the 2 MB read cap; cannot validate JSON.")
    try:
        json.loads(text)
    except ValueError as exc:
        return CheckResult(False, f"'{path}' is not valid JSON: {exc}.")
    return CheckResult(True, f"'{path}' is valid JSON.")
