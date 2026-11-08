"""Slice 5.0: Quality Judgement — DoR/DoD generation through the harness adapter."""
from __future__ import annotations

import json

import pytest

from app.adapter import AgentResponse, HarnessAdapterError
from app.prompts import composeQualityPrompt, extract_quality_json
from app.repo import TeamRepository


class _StubAdapter:
    """Stands in for the composite adapter; records turns and returns a canned response."""

    def __init__(self, text: str = "", error: Exception | None = None):
        self.turns = []
        self._text = text
        self._error = error

    async def execute(self, turn):
        self.turns.append(turn)
        if self._error:
            raise self._error
        return AgentResponse(text=self._text, model="stub-model", finish_reason="stop")


QUALITY_JSON = json.dumps({
    "definition_of_ready": "- Outcome is clear\n- Acceptance criteria written\n- Dependencies identified",
    "definition_of_done": "- Tests pass in Docker\n- Reviewed by verifier role\n- Mission-aligned",
})


class TestQualityPrompt:
    """composeQualityPrompt + extract_quality_json unit coverage (asyncio_mode=auto)."""

    def test_prompt_includes_vision_mission_and_format(self):
        result = composeQualityPrompt(
            team_vision="Ship verified value daily.",
            team_mission="Automate the boring parts of engineering.",
        )
        assert "Quality Judge" in result
        assert "Ship verified value daily." in result
        assert "Automate the boring parts of engineering." in result
        assert "definition_of_ready" in result
        assert "definition_of_done" in result

    def test_prompt_omits_guidance_when_absent(self):
        result = composeQualityPrompt("v", "m")
        assert "User Guidance" not in result

    def test_prompt_includes_guidance_sections(self):
        result = composeQualityPrompt(
            "v", "m",
            dor_guidance="Every task names its verifier.",
            dod_guidance="Evidence attached before Done.",
        )
        assert "User Guidance" in result
        assert "Every task names its verifier." in result
        assert "Evidence attached before Done." in result

    def test_extract_plain_json(self):
        out = extract_quality_json(QUALITY_JSON)
        assert out["definition_of_ready"].startswith("- Outcome is clear")
        assert "Mission-aligned" in out["definition_of_done"]

    def test_extract_fenced_json(self):
        text = "Here you go:\n```json\n" + QUALITY_JSON + "\n```\nDone!"
        out = extract_quality_json(text)
        assert "definition_of_ready" in out

    def test_extract_bare_backticks_with_prose(self):
        text = "Sure! ``" + QUALITY_JSON + "`` hope that helps"
        out = extract_quality_json(text)
        assert "Outcome is clear" in out["definition_of_ready"]

    def test_extract_surrounding_prose(self):
        text = "The standards are: " + QUALITY_JSON + " — let me know!"
        out = extract_quality_json(text)
        assert out["definition_of_done"].startswith("- Tests pass")

    @pytest.mark.parametrize("bad", [
        "",
        "I cannot generate that.",
        '{"other": "field"}',
        '{"definition_of_ready": null, "definition_of_done": ""}',
        '{"definition_of_ready": 42, "definition_of_done": ["a"]}',
    ])
    def test_extract_rejects_invalid(self, bad):
        with pytest.raises(ValueError):
            extract_quality_json(bad)


@pytest.mark.asyncio
class TestQualityEndpoints:
    """POST/GET /teams/{id}/quality through the harness adapter (stubbed)."""

    async def _make_team(self, pool, team_id="team_q1", **kw):
        repo = TeamRepository(pool)
        return await repo.upsert({
            "id": team_id,
            "name": kw.get("name", "Quality Team"),
            "vision_statement": kw.get("vision", "Ship verified value daily."),
            "mission_statement": kw.get("mission", "Automate the boring parts."),
            "lifecycle_state": kw.get("state", "ACTIVE"),
        })

    async def test_post_generates_and_persists(self, client, pool, monkeypatch):
        import app.adapter as adapter_module
        await self._make_team(pool)
        stub = _StubAdapter(text=QUALITY_JSON)
        monkeypatch.setattr(adapter_module, "_active_adapter", stub)

        r = await client.post("/teams/team_q1/quality")
        assert r.status_code == 201
        data = r.json()
        assert "Outcome is clear" in data["definition_of_ready"]
        assert "Mission-aligned" in data["definition_of_done"]
        assert data["model"] == "stub-model"

        # Turn carried a quality-judge prompt built from the team's vision/mission.
        turn = stub.turns[0]
        assert turn.metadata["role"] == "quality_judge"
        assert "Ship verified value daily." in turn.system_prompt
        assert "Automate the boring parts." in turn.system_prompt

        # GET reads back what was persisted.
        g = await client.get("/teams/team_q1/quality")
        assert g.status_code == 200
        assert g.json()["definition_of_ready"] == data["definition_of_ready"]
        assert g.json()["definition_of_done"] == data["definition_of_done"]

    async def test_guidance_reaches_prompt(self, client, pool, monkeypatch):
        import app.adapter as adapter_module
        await self._make_team(pool)
        stub = _StubAdapter(text=QUALITY_JSON)
        monkeypatch.setattr(adapter_module, "_active_adapter", stub)

        r = await client.post(
            "/teams/team_q1/quality",
            json={"dor_guidance": "Every task must name its verifier.",
                  "dod_guidance": "No local venvs — Docker only."},
        )
        assert r.status_code == 201
        prompt = stub.turns[0].system_prompt
        assert "Every task must name its verifier." in prompt
        assert "No local venvs — Docker only." in prompt

    async def test_regenerate_overwrites(self, client, pool, monkeypatch):
        import app.adapter as adapter_module
        await self._make_team(pool)
        stub = _StubAdapter(text=QUALITY_JSON)
        monkeypatch.setattr(adapter_module, "_active_adapter", stub)
        await client.post("/teams/team_q1/quality")

        second = json.dumps({"definition_of_ready": "READY V2", "definition_of_done": "DONE V2"})
        stub._text = second
        r = await client.post("/teams/team_q1/quality")
        assert r.status_code == 201
        assert r.json()["definition_of_ready"] == "READY V2"

        g = await client.get("/teams/team_q1/quality")
        assert g.json()["definition_of_done"] == "DONE V2"

    async def test_unparseable_response_is_502(self, client, pool, monkeypatch):
        import app.adapter as adapter_module
        await self._make_team(pool)
        stub = _StubAdapter(text="Sorry, I can't define quality.")
        monkeypatch.setattr(adapter_module, "_active_adapter", stub)

        r = await client.post("/teams/team_q1/quality")
        assert r.status_code == 502
        # Nothing persisted — GET still shows nulls.
        g = await client.get("/teams/team_q1/quality")
        assert g.json()["definition_of_ready"] is None

    async def test_adapter_error_is_502(self, client, pool, monkeypatch):
        import app.adapter as adapter_module
        await self._make_team(pool)
        stub = _StubAdapter(error=HarnessAdapterError("LMStudio unreachable", code="unreachable"))
        monkeypatch.setattr(adapter_module, "_active_adapter", stub)

        r = await client.post("/teams/team_q1/quality")
        assert r.status_code == 502

    async def test_busy_error_is_409(self, client, pool, monkeypatch):
        import app.adapter as adapter_module
        await self._make_team(pool)
        stub = _StubAdapter(error=HarnessAdapterError("queue full", code="busy"))
        monkeypatch.setattr(adapter_module, "_active_adapter", stub)

        r = await client.post("/teams/team_q1/quality")
        assert r.status_code == 409

    async def test_unknown_team_is_404(self, client):
        r = await client.post("/teams/nope/quality")
        assert r.status_code == 404
        g = await client.get("/teams/nope/quality")
        assert g.status_code == 404

    async def test_publishes_quality_signal(self, client, pool, monkeypatch, bus):
        import app.adapter as adapter_module
        await self._make_team(pool)
        stub = _StubAdapter(text=QUALITY_JSON)
        monkeypatch.setattr(adapter_module, "_active_adapter", stub)

        r = await client.post("/teams/team_q1/quality")
        assert r.status_code == 201
        signals = await bus.recent_signals()
        kinds = [s.kind for s in signals]
        assert "quality.generated" in kinds
