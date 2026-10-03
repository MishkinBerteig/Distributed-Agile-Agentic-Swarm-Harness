"""Prompt composition for the alignment hierarchy.

Produces system prompts that inject Vision and Mission context into agent
executions so every decision can be traced back to the swarm's purpose.
"""
from __future__ import annotations

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
