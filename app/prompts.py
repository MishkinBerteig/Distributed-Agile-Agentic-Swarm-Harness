"""Prompt composition for the alignment hierarchy.

Produces system prompts that inject Vision and Mission context into agent
executions so every decision can be traced back to the swarm's purpose.
"""
from __future__ import annotations

import json
from typing import Optional


def composeVisionPrompt(team_vision: str) -> str:
    """Compose a system prompt grounded only in the team's Vision statement."""
    return (
        "# Vision\n"
        f"{team_vision}\n"
        "\n"
        "You are an agent operating under this team. Every decision you make should\n"
        "directly serve the Vision above. If a task conflicts with the Vision, flag it\n"
        "immediately."
    )


def composeMissionPrompt(
    team_vision: str,
    team_mission: str,
    task_name: Optional[str] = None,
    task_description: Optional[str] = None,
    task_acceptance_criteria: Optional[str] = None,
) -> str:
    """Compose a system prompt that layers Mission context and optionally task details.

    The hierarchy flows:
      1. Vision  (top-level purpose)
      2. Mission (how the team fulfills the vision)
      3. Task    (the concrete work item)
    """
    lines: list[str] = []

    lines.append("# Vision")
    lines.append(team_vision)
    lines.append("")

    lines.append("# Mission")
    lines.append(team_mission)
    lines.append("")

    lines.append(
        "You must operate within this Mission. If a task does not serve the Mission "
        "or conflicts with the Vision, reject it and explain why."
    )
    lines.append("")

    if task_name or task_description or task_acceptance_criteria:
        lines.append("# Assigned Task")
        lines.append("## Name")
        lines.append(task_name or "(unnamed)")
        lines.append("")
        lines.append("## Description")
        lines.append(task_description or "")
        lines.append("")
        lines.append("## Acceptance Criteria")
        lines.append(task_acceptance_criteria or "")
        lines.append("")
        lines.append(
            "Before executing this task, verify it aligns with the Mission above. "
            "If it does not, respond with a rejection reason instead of working on it."
        )

    return "\n".join(lines)


QUALITY_OUTPUT_INSTRUCTION = (
    "Respond with ONLY a single JSON object, no prose and no code fences, in exactly "
    'this shape:\n{"definition_of_ready": "...", "definition_of_done": "..."}\n'
    "Each value must be a plain-text list of criteria separated by newlines."
)


def composeQualityPrompt(
    team_vision: str,
    team_mission: str,
    dor_guidance: Optional[str] = None,
    dod_guidance: Optional[str] = None,
) -> str:
    """Compose a system prompt for the Quality Judgement role.

    The agent acts as the swarm's Quality Judge: it derives a Definition of
    Ready and a Definition of Done from the team's Vision/Mission, honoring any
    user guidance, and must answer with strict JSON (see QUALITY_OUTPUT_INSTRUCTION).
    """
    lines = [
        "# Role: Quality Judge",
        "You are the Quality Judge for an agent swarm. Your sole job is to define "
        "the team's quality standards:",
        "- Definition of Ready: the checklist a task must satisfy before work may "
        "start (clear outcome, acceptance criteria, dependencies identified, etc.).",
        "- Definition of Done: the bar a result must clear before it may be "
        "accepted as complete (tests pass, reviewed, aligned with the mission, etc.).",
        "",
        "# Team Vision",
        team_vision or "(no vision provided)",
        "",
        "# Team Mission",
        team_mission or "(no mission provided)",
        "",
    ]

    if dor_guidance or dod_guidance:
        lines.append("# User Guidance")
        if dor_guidance:
            lines.append("## For the Definition of Ready")
            lines.append(dor_guidance)
            lines.append("")
        if dod_guidance:
            lines.append("## For the Definition of Done")
            lines.append(dod_guidance)
            lines.append("")
        lines.append(
            "Weave this guidance into the standards you produce. It refines — but "
            "never replaces — alignment with the Vision and Mission."
        )
        lines.append("")

    lines.append("# Output format")
    lines.append(QUALITY_OUTPUT_INSTRUCTION)
    return "\n".join(lines)


def extract_quality_json(text: str) -> dict[str, str]:
    """Parse a Quality Judgement response into {"definition_of_ready", "definition_of_done"}.

    Tolerates surrounding prose and ``` code fences. Raises ValueError when no
    JSON object with both string criteria can be recovered.
    """
    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    # Strip markdown code fences if present (```json ... ``` or ``` ... ```).
    fence_start = stripped.find("```")
    if fence_start != -1:
        body = stripped[fence_start + 3:]
        if body.lower().startswith("json"):
            body = body[4:]
        fence_end = body.find("```")
        candidates.append((body[:fence_end] if fence_end != -1 else body).strip())

    # Fall back to the first balanced {...} span in the text.
    start = stripped.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(stripped[start:i + 1])
                    break

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        dor = data.get("definition_of_ready")
        dod = data.get("definition_of_done")
        if isinstance(dor, str) and isinstance(dod, str) and (dor.strip() or dod.strip()):
            return {"definition_of_ready": dor.strip(), "definition_of_done": dod.strip()}

    raise ValueError(f"no valid quality JSON in model response: {text[:200]!r}")
