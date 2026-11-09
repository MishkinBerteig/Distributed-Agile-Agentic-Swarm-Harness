"""Pure helpers for remote harnesses (docs/DESIGN-0.3.md): runner tokens, lease matching,
result validation, workspace tarballs and staging. No database and no HTTP in here, so every
rule is unit-testable; app.main and app.jobs call these."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import secrets
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from app import models
from app.models import KNOWN_HARNESSES, NATIVE_HARNESS

TOKEN_PREFIX = "daash_rt_"
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15
POLL_SECONDS = 5
ONLINE_SECONDS = 60
MAX_WAIT_SECONDS = 30
MAX_BODY_BYTES = 25 * 1024 * 1024
MAX_FILES = 200
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 20_000_000
SUMMARY_MAX = 4000
TRANSCRIPT_MAX = 20_000
PROMPT_LISTING_CLIP = 2000
STAGING_DIR = ".daash/staging"
#: Folders an agent CLI keeps its own state in; never part of a deliverable.
CLI_STATE_DIRS = (".git", ".claude", ".codex", ".openclaude", "node_modules")
#: Generated files an agent's tooling leaves behind (running Python, tests, editors). They are
#: dropped from results silently: never deliverables, and binary caches must not reach prompts.
GENERATED_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv",
                  ".tox", ".ipynb_checkpoints")
GENERATED_SUFFIXES = (".pyc", ".pyo")
GENERATED_NAMES = (".DS_Store",)


def is_generated(rel: str) -> bool:
    """True for build/cache junk (see GENERATED_*) that is never a deliverable."""
    parts = rel.split("/")
    return (any(p in GENERATED_DIRS for p in parts) or parts[-1] in GENERATED_NAMES
            or parts[-1].endswith(GENERATED_SUFFIXES))
OUTCOMES = ("submitted", "failed", "error")
#: Remote harness names a runner may offer (the native one never goes through a runner).
REMOTE_HARNESSES = tuple(h for h in KNOWN_HARNESSES if h != NATIVE_HARNESS)


# -- tokens ---------------------------------------------------------------------------

def new_runner_token() -> str:
    """`daash_rt_` + 43 url-safe characters (256 bits)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """sha256 hex of a bearer token; the only form stored."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def tokens_match(presented: str, expected: str) -> bool:
    """Constant-time equality of two secrets (for the admin API token)."""
    return hmac.compare_digest((presented or "").encode("utf-8"), (expected or "").encode("utf-8"))


def bearer_token(header: Optional[str]) -> str:
    """The token of an `Authorization: Bearer <token>` header ('' if absent/other scheme)."""
    if not header:
        return ""
    scheme, _, rest = header.strip().partition(" ")
    return rest.strip() if scheme.lower() == "bearer" else ""


def heartbeat_seconds_for(lease_seconds: int) -> float:
    """Heartbeat interval for a lease length: the default 15 s, at most a third of the lease."""
    return float(min(HEARTBEAT_SECONDS, max(0.5, lease_seconds / 3)))


# -- capabilities and lease matching ------------------------------------------------------

def normalize_capabilities(raw: Any) -> list[dict[str, Any]]:
    """Runner-announced `[{"harness", "models", "capabilities"}]` cleaned up: unknown or native
    harness names and malformed entries are ignored (not an error), duplicate harnesses merged.
    `capabilities` (docs/DESIGN-0.3.md section 11) keeps known values only; missing = the
    harness default; merged duplicates offer the union."""
    if not isinstance(raw, list):
        return []
    models_by: dict[str, Optional[list[str]]] = {}   # None = any model
    caps_by: dict[str, list[str]] = {}
    for item in raw:
        if not isinstance(item, dict) or item.get("harness") not in REMOTE_HARNESSES:
            continue
        name = item["harness"]
        if isinstance(item.get("capabilities"), list):
            offered = models.normalize_capabilities(item["capabilities"])
        else:
            offered = models.harness_capabilities(name)
        caps_by[name] = models.normalize_capabilities(caps_by.get(name, []) + offered)
        ms = item.get("models")
        ms = [m for m in ms if isinstance(m, str) and m] if isinstance(ms, list) else []
        if not ms or (name in models_by and models_by[name] is None):
            models_by[name] = None
        else:
            cur = models_by.setdefault(name, [])
            cur.extend(m for m in ms if m not in cur)
    return [{"harness": h, "models": ms or [], "capabilities": caps_by.get(h, [])}
            for h, ms in models_by.items()]


def capability_map(capabilities: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """harness -> models (empty list = any model)."""
    return {c["harness"]: list(c.get("models") or []) for c in capabilities
            if isinstance(c, dict) and c.get("harness")}


def harness_offered(capabilities: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """harness -> capabilities the runner offers (missing = the harness default)."""
    out: dict[str, list[str]] = {}
    for c in capabilities:
        if isinstance(c, dict) and c.get("harness"):
            caps = c.get("capabilities")
            out[c["harness"]] = (models.normalize_capabilities(caps) if isinstance(caps, list)
                                 else models.harness_capabilities(c["harness"]))
    return out


def job_matches(job_harness: str, job_model: str, caps: dict[str, list[str]],
                required: Iterable[str] = (),
                offered: Optional[dict[str, list[str]]] = None) -> bool:
    """A runner can take a job if it offers the harness and (offers any model or this one) and,
    when `offered` is given, the harness entry covers the job's required capabilities."""
    if job_harness not in caps:
        return False
    ms = caps[job_harness]
    if ms and job_model not in ms:
        return False
    need = set(models.required_capabilities(list(required)))
    if need:
        have = (offered or {}).get(job_harness)
        if have is None:
            have = models.harness_capabilities(job_harness)
        return need <= set(have)
    return True


def first_match(jobs: Iterable[Any], caps: dict[str, list[str]],
                offered: Optional[dict[str, list[str]]] = None) -> Optional[Any]:
    """The first job (callers pass them oldest first) a runner with `caps` can take."""
    for j in jobs:
        payload = _attr(j, "payload") if (isinstance(j, dict) and "payload" in j) else {}
        req = (payload or {}).get("required_capabilities", []) if isinstance(payload, dict) else []
        if job_matches(_attr(j, "harness"), _attr(j, "model"), caps, req, offered):
            return j
    return None


def _attr(obj: Any, name: str) -> Any:
    return obj[name] if isinstance(obj, dict) else getattr(obj, name)


# -- paths and result validation ------------------------------------------------------------

class ResultError(ValueError):
    """A runner result (or one of its paths) that the coordinator refuses; the text is the
    reason returned in the 400 body."""


def validate_rel_path(path: Any) -> str:
    """The normalised relative POSIX path, or ResultError.

    Rejects: non-strings, empty, NUL, backslashes, absolute paths, `..` segments, anything under
    `.daash/` and CLI state folders (`.git/ .claude/ .codex/ .openclaude/ node_modules/`)."""
    if not isinstance(path, str) or not path.strip():
        raise ResultError("a file path is empty or not a string")
    if "\x00" in path:
        raise ResultError(f"path {path!r} contains a NUL byte")
    if "\\" in path:
        raise ResultError(f"path {path!r} contains a backslash")
    if path.startswith("/") or PurePosixPath(path).is_absolute():
        raise ResultError(f"path {path!r} is absolute")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts:
        raise ResultError(f"path {path!r} is empty")
    if ".." in parts:
        raise ResultError(f"path {path!r} contains '..'")
    if parts[0] == ".daash":
        raise ResultError(f"path {path!r} is inside the reserved .daash folder")
    if any(p in CLI_STATE_DIRS for p in parts):
        raise ResultError(f"path {path!r} is inside an agent CLI state folder")
    return "/".join(parts)


@dataclass
class ValidatedResult:
    outcome: str
    summary: str
    files: list[tuple[str, bytes]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: Optional[float] = None
    duration_ms: int = 0
    exit_code: Optional[int] = None
    transcript_tail: str = ""
    error: str = ""
    check_results: list[dict[str, Any]] = field(default_factory=list)


CHECK_OUTPUT_MAX = 4000
MAX_CHECK_RESULTS = 50


def clean_check_results(raw: Any, allowed_ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    """Command-check results (docs/DESIGN-0.3.md section 12) cleaned up: malformed entries are
    dropped, output clipped to its last CHECK_OUTPUT_MAX characters, one entry per id, only ids in
    `allowed_ids` when given."""
    if not isinstance(raw, list):
        return []
    allowed = set(allowed_ids) if allowed_ids is not None else None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[:MAX_CHECK_RESULTS]:
        if not isinstance(item, dict):
            continue
        cid, code = item.get("id"), item.get("exit_code")
        if not isinstance(cid, str) or not cid or cid in seen:
            continue
        if allowed is not None and cid not in allowed:
            continue
        seen.add(cid)
        timed_out = item.get("timed_out") is True
        out.append({"id": cid,
                    "exit_code": code if isinstance(code, int) and not isinstance(code, bool) else None,
                    "output_tail": str(item.get("output_tail") or "")[-CHECK_OUTPUT_MAX:],
                    "duration_ms": _int(item.get("duration_ms")), "timed_out": timed_out})
    return out


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def validate_result(body: Any, max_files: int = MAX_FILES, max_file_bytes: int = MAX_FILE_BYTES,
                    max_total_bytes: int = MAX_TOTAL_BYTES) -> ValidatedResult:
    """Check a runner's result JSON (section 5.5). Raises ResultError with the reason."""
    if not isinstance(body, dict):
        raise ResultError("the result must be a JSON object")
    outcome = body.get("outcome")
    if outcome not in OUTCOMES:
        raise ResultError(f"outcome must be one of {', '.join(OUTCOMES)}")
    files_in = body.get("files") or []
    deleted_in = body.get("deleted") or []
    if not isinstance(files_in, list) or not isinstance(deleted_in, list):
        raise ResultError("files and deleted must be lists")
    if len(files_in) > max_files:
        raise ResultError(f"too many files ({len(files_in)} > {max_files})")
    files: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    total = 0
    for item in files_in:
        if not isinstance(item, dict):
            raise ResultError("each file must be an object with path and content_b64")
        rel = validate_rel_path(item.get("path"))
        if is_generated(rel):
            continue
        if rel in seen:
            raise ResultError(f"path {rel!r} appears twice")
        seen.add(rel)
        b64 = item.get("content_b64")
        if not isinstance(b64, str):
            raise ResultError(f"file {rel!r} has no content_b64")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ResultError(f"file {rel!r} is not valid base64") from None
        if len(data) > max_file_bytes:
            raise ResultError(f"file {rel!r} is {len(data)} bytes (limit {max_file_bytes})")
        total += len(data)
        if total > max_total_bytes:
            raise ResultError(f"files total more than {max_total_bytes} bytes")
        files.append((rel, data))
    deleted = []
    for d in deleted_in:
        rel = validate_rel_path(d)
        if rel not in deleted and not is_generated(rel):
            deleted.append(rel)
    usage_in = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    usage = {k: _int(usage_in.get(k)) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    if not usage["total_tokens"]:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    cost = body.get("cost_usd")
    return ValidatedResult(
        outcome=outcome, summary=str(body.get("summary") or "")[:SUMMARY_MAX], files=files,
        deleted=deleted, usage=usage,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        duration_ms=_int(body.get("duration_ms")),
        exit_code=body.get("exit_code") if isinstance(body.get("exit_code"), int) else None,
        transcript_tail=str(body.get("transcript_tail") or "")[-TRANSCRIPT_MAX:],
        error=str(body.get("error") or "")[:2000],
        check_results=clean_check_results(body.get("check_results")))


def stored_result(v: ValidatedResult) -> dict[str, Any]:
    """The result as stored on the job: file contents dropped, paths kept."""
    return {"outcome": v.outcome, "summary": v.summary, "files": [p for p, _ in v.files],
            "deleted": list(v.deleted), "usage": v.usage, "cost_usd": v.cost_usd,
            "duration_ms": v.duration_ms, "exit_code": v.exit_code,
            "transcript_tail": v.transcript_tail, "error": v.error,
            "check_results": list(v.check_results)}


# -- filesystem: workspace tarball and staging ---------------------------------------------------

def _inside(root: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def build_workspace_tar(root: Path) -> bytes:
    """tar.gz of the regular files under `root`, relative paths, excluding `.daash/` and
    symlinks (so nothing outside the workspace can leak in)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                rel = p.relative_to(root)
                if rel.parts and rel.parts[0] == ".daash":
                    continue
                if p.is_symlink() or not p.is_file() or not _inside(root, p):
                    continue
                tar.add(str(p), arcname=rel.as_posix(), recursive=False)
    return buf.getvalue()


def staging_dir(root: Path, job_id: str) -> Path:
    return root / STAGING_DIR / job_id


def stage_files(root: Path, job_id: str, files: Iterable[tuple[str, bytes]]) -> Path:
    """Write validated files under `.daash/staging/<job_id>/` (replacing any earlier staging)."""
    base = staging_dir(root, job_id)
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    for rel, data in files:
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return base


def apply_staged(root: Path, job_id: str, deleted: Iterable[str]) -> tuple[list[str], list[str]]:
    """Move staged files into the workspace (replacing) and delete `deleted` paths, never
    leaving the workspace. Removes the staging dir. Returns (written, deleted) relative paths."""
    base = staging_dir(root, job_id)
    written: list[str] = []
    if base.is_dir():
        for src in sorted(base.rglob("*")):
            if not src.is_file() or src.is_symlink():
                continue
            rel = src.relative_to(base).as_posix()
            dest = root / rel
            if not _inside(root, dest):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(src), str(dest))
            written.append(rel)
        shutil.rmtree(base, ignore_errors=True)
    gone: list[str] = []
    for rel in deleted:
        try:
            rel = validate_rel_path(rel)
        except ResultError:
            continue
        dest = root / rel
        if _inside(root, dest) and (dest.is_file() or dest.is_symlink()):
            dest.unlink()
            gone.append(rel)
    return written, gone


def clip_for_listing(job: dict[str, Any], limit: int = PROMPT_LISTING_CLIP) -> dict[str, Any]:
    """A job's JSON with the long prompt bodies shortened, for the dashboard list."""
    out = dict(job)
    payload = dict(out.get("payload") or {})
    for key in ("system_prompt", "prompt"):
        text = payload.get(key)
        if isinstance(text, str) and len(text) > limit:
            payload[key] = text[:limit] + f"... [{len(text) - limit} more characters]"
    out["payload"] = payload
    return out
