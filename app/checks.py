"""Deterministic acceptance-criterion checks against a swarm workspace directory.

Reads files only (never writes). Paths are confined to the workspace: absolute
paths, `..` components and symlinks that resolve outside are failed checks.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Optional

from app.models import DETERMINISTIC_CHECKS, AcceptanceCriterion, CheckKind

MAX_READ_BYTES = 2 * 1024 * 1024
EVIDENCE_MAX_CHARS = 200
COMMAND_EVIDENCE_CHARS = 1200


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


MAX_PATTERN_CHARS = 300
MAX_COMMAND_CHARS = 300
COMMAND_TIMEOUT_SECONDS = 120


def validate_command(cmd: Optional[str]) -> Optional[str]:
    """An error message if a command check's shell command is unusable, else None."""
    if cmd is None or not cmd.strip():
        return "Check 'command' needs a shell command in check_arg."
    if len(cmd) > MAX_COMMAND_CHARS:
        return (f"Check 'command' is too long ({len(cmd)} characters; the limit is "
                f"{MAX_COMMAND_CHARS}). Put the steps in a script file and run that.")
    if "\x00" in cmd or "\n" in cmd or "\r" in cmd:
        return "Check 'command' must be a single line without NUL or newline characters."
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
    if kind == CheckKind.COMMAND.value:
        return validate_command(arg)
    problem = _path_problem(path)
    if problem:
        return f"Check '{kind}' needs a valid path: {problem}."
    if kind == CheckKind.FILE_CONTAINS.value:
        if arg is None or arg == "":
            return "Check 'file_contains' needs a regular expression in check_arg."
        if len(arg) > MAX_PATTERN_CHARS:
            return (f"Check 'file_contains' regular expression is too long ({len(arg)} "
                    f"characters; the limit is {MAX_PATTERN_CHARS}). Use a simpler pattern.")
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


def command_result_for(criterion_id: str, check_results: Any) -> Optional[dict]:
    """The stored runner result for a command criterion, if any."""
    for r in check_results or ():
        if isinstance(r, dict) and r.get("id") == criterion_id:
            return r
    return None


def evaluate_command(criterion: AcceptanceCriterion, check_results: Any) -> CheckResult:
    """A command criterion from the runner-produced `deliverable["check_results"]`."""
    spec_error = validate_command(criterion.check_arg)
    if spec_error:
        return CheckResult(False, spec_error)
    r = command_result_for(criterion.id, check_results)
    if r is None:
        return CheckResult(False, "no runner executed this check (the work was not done by a "
                                  "remote agent that can run code)")
    tail = str(r.get("output_tail") or "").strip()
    if r.get("timed_out"):
        return CheckResult(False, _truncate(
            f"timed out; `{criterion.check_arg}` was killed. {tail}", COMMAND_EVIDENCE_CHARS))
    if r.get("exit_code") == 0:
        return CheckResult(True, _truncate(f"exit 0. {tail}", COMMAND_EVIDENCE_CHARS))
    return CheckResult(False, _truncate(
        f"exit {r.get('exit_code')}. {tail}", COMMAND_EVIDENCE_CHARS))


def run_check(criterion: AcceptanceCriterion, workspace: Path,
              check_results: Any = None) -> CheckResult:
    """Run one deterministic check. Raises ValueError only for rubric/unknown kinds."""
    kind = _kind(criterion.check_kind)
    if kind == CheckKind.RUBRIC.value or kind not in DETERMINISTIC_CHECKS:
        raise ValueError(f"Check kind '{kind}' is not deterministic and cannot be run by code.")
    if kind == CheckKind.COMMAND.value:
        return evaluate_command(criterion, check_results)
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
