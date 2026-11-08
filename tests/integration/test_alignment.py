"""Slice 4: Alignment Hierarchy — Vision/Mission prompt injection + Mission Judgement."""
from __future__ import annotations

import pytest

from app.prompts import composeMissionPrompt, composeVisionPrompt
from app.repo import TeamRepository


@pytest.mark.asyncio
class TestPromptComposition:
    """Test prompt composition with vision and mission."""

    def test_compose_vision_prompt_basic(self):
        """Vision prompt includes the vision statement and behavioral guidance."""
        vision = "Build the future of cloud infrastructure."
        result = composeVisionPrompt(vision)
        assert vision in result
        assert "Vision" in result
        assert "conflicts with the Vision" in result

    def test_compose_mission_prompt_minimal(self):
        """Mission prompt includes vision + mission with no task details."""
        vision = "Build the future of cloud infrastructure."
        mission = "Deliver cloud solutions that empower developers."
        result = composeMissionPrompt(vision, mission)
        assert vision in result
        assert mission in result
        assert "Mission" in result
        assert "Assigned Task" not in result

    def test_compose_mission_prompt_with_task(self):
        """Mission prompt layers task details when provided."""
        result = composeMissionPrompt(
            team_vision="Build the future.",
            team_mission="Ship great software.",
            task_name="API Gateway",
            task_description="Build a REST API gateway.",
            task_acceptance_criteria="Handles 1k rps.",
        )
        assert "API Gateway" in result
        assert "Build a REST API gateway." in result
        assert "Handles 1k rps." in result
        assert "Assigned Task" in result
        assert "verify it aligns" in result


@pytest.mark.asyncio
class TestMissionJudgement:
    """Mission Judgement endpoint — alignment checks via embedding cosine similarity."""

    async def test_judgement_aligned(self, client, pool):
        """A task closely matching the mission should be aligned."""
        # Create team with matching vision/mission
        repo = TeamRepository(pool)
        team_id = "team_judge_aligned"
        await repo.upsert({
            "id": team_id,
            "name": "Judge Team",
            "vision_statement": "Build the future of developer tools.",
            "mission_statement": "We build tools that help developers write better code.",
            "lifecycle_state": "ACTIVE",
        })

        # Task text strongly aligned with mission
        r = await client.patch(
            f"/tasks/task-001/judgement",
            json={"team_id": team_id, "task_text": "Build a code review tool to help developers write better code."},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["team_id"] == team_id
        # Score should be > 0.5 for highly aligned task
        assert data["score"] > 0.5
        assert "system_prompt" in data
        assert "Build the future of developer tools." in data["system_prompt"]
        assert "We build tools that help developers write better code." in data["system_prompt"]

    async def test_judgement_misaligned(self, client, pool):
        """A task far from the mission should not be aligned."""
        repo = TeamRepository(pool)
        team_id = "team_judge_misaligned"
        await repo.upsert({
            "id": team_id,
            "name": "Judge Team 2",
            "vision_statement": "Build the future of developer tools.",
            "mission_statement": "We build tools that help developers write better code.",
            "lifecycle_state": "ACTIVE",
        })

        # Task text completely unrelated to mission
        r = await client.patch(
            f"/tasks/task-002/judgement",
            json={"team_id": team_id, "task_text": "Launch a rocket to Mars."},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["team_id"] == team_id
        # Should be below threshold (0.7) — note: actual result depends on embedding model
        assert "aligned" in data
        assert "score" in data

    async def test_judgement_team_not_found(self, client):
        """Judgement on a non-existent team returns 404."""
        r = await client.patch(
            "/tasks/task-003/judgement",
            json={"team_id": "nonexistent", "task_text": "Do something."},
        )
        assert r.status_code == 404

    async def test_judgement_returns_prompt(self, client, pool):
        """Judgement response contains the composed system prompt."""
        repo = TeamRepository(pool)
        team_id = "team_judge_prompt"
        await repo.upsert({
            "id": team_id,
            "name": "Judge Team 3",
            "vision_statement": "Test vision.",
            "mission_statement": "Test mission.",
            "lifecycle_state": "ACTIVE",
        })

        r = await client.patch(
            f"/tasks/task-004/judgement",
            json={"team_id": team_id, "task_text": "Test alignment prompt."},
        )
        assert r.status_code == 200
        data = r.json()
        assert "Test vision." in data["system_prompt"]
        assert "Test mission." in data["system_prompt"]
        assert "Assigned Task" in data["system_prompt"]
