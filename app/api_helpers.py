"""Pure helpers for the HTTP API (app.main): no I/O beyond the filesystem under a given root.

Kept separate so they can be unit-tested without a database, network or app start-up.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from app.checks import validate_check_spec
from app.models import NATIVE_HARNESS, harness_capabilities, normalize_capabilities
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


# -- availability (DAASHboard's Agents & models page) -------------------------------------------

#: Display names for agent harnesses. "fake" is the test suite's scripted CLI.
HARNESS_LABELS = {"daash": "DAASH built-in agent", "openclaude": "OpenClaude",
                  "claude-code": "Claude Code", "codex": "Codex", "fake": "Fake CLI (tests only)"}
#: Harnesses always listed, even when no runner offers them; others appear when a runner does.
LISTED_HARNESSES = ("daash", "openclaude", "claude-code", "codex")
_ROLE_LABELS = (("coordinator", "Swarm Coordinator"), ("mission_judgement", "Mission Judgement"),
                ("quality_judgement", "Quality Judgement"),
                ("mission_delivery", "Mission Delivery (first attempt)"),
                ("classifier", "Classifier"))


def availability(cfg: Any, gateway_models: list[str], reachable: bool,
                 runners: list[dict]) -> dict:
    """What a swarm with config `cfg` can use right now: every model (escalation step, the
    roles that start on it, whether the gateway offers it) and every agent harness (where it
    runs, what it can do, which models, and why it is or is not available).

    `runners` are API runner dicts (with `online`). A remote harness is "available" when an
    online runner offers it, "offline" when only offline runners do, "disabled" when only
    disabled ones do, and "no_runner" otherwise. The built-in harness needs the gateway."""
    allowed = set(cfg.allowed_models)
    ladder = [m for m in cfg.model_ladder if m in allowed]
    role_model = {"coordinator": cfg.coordinator.model, **cfg.role_models.model_dump()}
    names = list(dict.fromkeys([*cfg.model_ladder, *gateway_models, *cfg.allowed_models]))
    models = [{"name": m, "step": ladder.index(m) + 1 if m in ladder else None,
               "allowed": m in allowed, "offered": (m in gateway_models) if reachable else None,
               "roles": [label for key, label in _ROLE_LABELS if role_model.get(key) == m]}
              for m in names]
    order = {m: i for i, m in enumerate(names)}

    offered: dict[str, list[tuple[dict, dict]]] = {}
    for r in runners:
        for spec in r.get("harnesses") or []:
            if spec.get("harness"):
                offered.setdefault(spec["harness"], []).append((r, spec))
    harnesses = []
    for name in [*LISTED_HARNESSES, *sorted(set(offered) - set(LISTED_HARNESSES))]:
        hits = offered.get(name, [])
        if name == NATIVE_HARNESS:
            status = "available" if reachable else "gateway_unreachable"
            caps = harness_capabilities(name)
            h_models = sorted(gateway_models, key=lambda m: (order.get(m, 1e9), m))
        else:
            status = ("available" if any(r["online"] for r, _ in hits)
                      else "offline" if any(r["enabled"] for r, _ in hits)
                      else "disabled" if hits else "no_runner")
            caps = (normalize_capabilities([c for _, s in hits for c in s.get("capabilities") or []])
                    if hits else harness_capabilities(name))
            lists = [s.get("models") or [] for _, s in hits]
            # a runner that names no models lets the harness use any model
            h_models = (None if not hits or not all(lists) else
                        sorted({m for ms in lists for m in ms}, key=lambda m: (order.get(m, 1e9), m)))
        harnesses.append({
            "harness": name, "label": HARNESS_LABELS.get(name, name),
            "builtin": name == NATIVE_HARNESS, "status": status, "capabilities": caps,
            "models": h_models,
            "runners": [{"id": r["id"], "name": r["name"], "online": r["online"],
                         "enabled": r["enabled"]} for r, _ in hits],
            "allowed": name in cfg.allowed_harnesses,
            "delivery_rows": sum(1 for d in cfg.delivery_harnesses if d.harness == name)})
    return {"models": models, "harnesses": harnesses}
