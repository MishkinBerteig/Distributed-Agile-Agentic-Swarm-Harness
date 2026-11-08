"""Pure helpers for the HTTP API (app.main): no I/O beyond the filesystem under a given root.

Kept separate so they can be unit-tested without a database, network or app start-up.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from app.checks import validate_check_spec
from app.tools import ToolError, safe_path

ARTIFACT_LIST_CAP = 2000
ARTIFACT_READ_CAP = 200_000


# -- artifacts ---------------------------------------------------------------

def list_artifacts(root: Path, cap: int = ARTIFACT_LIST_CAP) -> list[dict]:
    """Files under `root` as {path (relative, posix), size, modified (ISO UTC)}; dirs skipped."""
    root = Path(root)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(root.rglob("*")):
        try:
            if p.is_symlink() or not p.is_file():
                continue
            st = p.stat()
        except OSError:
            continue
        out.append({"path": p.relative_to(root).as_posix(), "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()})
        if len(out) >= cap:
            break
    return out


def read_artifact(root: Path, rel: str, cap: int = ARTIFACT_READ_CAP) -> dict:
    """{path, content, truncated}. Raises FileNotFoundError for a missing file or a
    traversal attempt (the API maps both to 404)."""
    try:
        target = safe_path(Path(root), rel)
    except (ToolError, ValueError, OSError) as exc:
        raise FileNotFoundError(str(exc)) from None
    try:
        if not target.is_file():
            raise FileNotFoundError(f"no such file: {rel}")
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # unreadable, or a name the OS rejects
        raise FileNotFoundError(str(exc)) from None
    return {"path": rel, "content": text[:cap], "truncated": len(text) > cap}


# -- swarm transitions -------------------------------------------------------

ACTION_VERBS = {
    "start": "started the swarm", "pause": "paused the swarm", "resume": "resumed the swarm",
    "feedback": "opened the swarm for feedback", "verify": "sent the swarm to final verification",
    "learn": "moved the swarm to learning", "archive": "archived the swarm",
    "delete": "deleted the swarm",
}


def transition_fields(action: str, new_state: str, started_at: Optional[datetime],
                      now: datetime) -> dict[str, Any]:
    """Column updates that accompany a lifecycle transition (besides lifecycle_state)."""
    fields: dict[str, Any] = {"lifecycle_state": new_state}
    if action == "start" and started_at is None:
        fields["started_at"] = now
    if action == "resume":
        fields["paused_reason"] = None
    return fields


def transition_detail(action: str, old: str, new: str) -> str:
    return f"User {ACTION_VERBS.get(action, action)} ({old} -> {new})."


# -- metrics -----------------------------------------------------------------

def limits_block(limits: Any, started_at: Optional[datetime], tokens_used: int,
                 cost_usd: float, now: datetime) -> dict:
    """The `limits` object of GET /swarms/{id}/metrics (DESIGN section 9)."""
    elapsed = 0.0
    if started_at is not None:
        s = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
        n = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        elapsed = max(0.0, (n - s).total_seconds() / 60.0)
    return {"tokens_used": tokens_used, "max_total_tokens": limits.max_total_tokens,
            "cost_usd": cost_usd, "max_budget_usd": limits.max_budget_usd,
            "elapsed_minutes": round(elapsed, 2),
            "max_wall_clock_minutes": limits.max_wall_clock_minutes}


# -- tasks -------------------------------------------------------------------

def criteria_error(criteria: Iterable[Any]) -> Optional[str]:
    """First problem in a list of CriterionCreate-like objects, else None."""
    for i, c in enumerate(criteria, start=1):
        msg = validate_check_spec(getattr(c.check_kind, "value", c.check_kind),
                                  c.check_path, c.check_arg)
        if msg:
            return f"Acceptance criterion {i}: {msg}"
    return None


def task_patch_fields(patch: dict[str, Any]) -> dict[str, Any]:
    """Repo update fields for a user PATCH (only fields the user actually set).

    Reopening a task (status -> Pending) clears its assignment and approval/commit stamps
    (and done_at, so a reopened Done task is not still timed as finished) and gives it a
    fresh attempt budget, so a reopened Failed task is not failed again after one try."""
    fields = {k: (v.value if hasattr(v, "value") else v) for k, v in patch.items()}
    if fields.get("status") == "Pending":
        fields.update(assigned_agent_id=None, approved_at=None, committed_at=None, done_at=None,
                      attempts=0)
    return fields
