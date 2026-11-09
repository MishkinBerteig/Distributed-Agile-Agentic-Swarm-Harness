"""Vision review prompt and parser (docs/DESIGN-0.3.md section 10)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.prompts import parse_vision_review, vision_review_prompt

NAMES = ["Core", "Docs"]


def test_prompt_has_vision_dod_teams_and_unique_job_line():
    teams = [SimpleNamespace(name="Core", status="accomplished", mission_statement="Build it"),
             SimpleNamespace(name="Docs", status="failed", mission_statement="Document it")]
    done = {"Core": [SimpleNamespace(id="t1", name="Write core", status="Done", criteria=[])]}
    text = vision_review_prompt("Make a garden guide", "- Guide and code agree", teams,
                                done, "core.py (10 bytes)", {"core.py": "print(1)"})
    assert text.startswith("Job: review the whole swarm against the Vision")
    for needle in ("Make a garden guide", "- Guide and code agree", 'Team "Core" [accomplished]',
                   'Team "Docs" [failed]', "Write core", "core.py (10 bytes)", "print(1)",
                   "certainty", "concrete gap"):
        assert needle in text, needle


def test_parse_met_and_gaps():
    met = parse_vision_review(
        {"verdict": "met", "confidence": 0.995, "summary": "ok", "gaps": []}, NAMES, 0.99)
    assert met.verdict == "met" and met.gaps == []
    gaps = parse_vision_review(
        {"verdict": "gaps", "confidence": 0.6, "summary": "x",
         "gaps": [{"team": "Docs", "focus": "Describe the API"}]}, NAMES, 0.99)
    assert gaps.gaps == [("Docs", "Describe the API")]


def test_parse_rejects_unknown_team_and_lists_valid_names():
    with pytest.raises(ValueError) as e:
        parse_vision_review({"verdict": "gaps", "confidence": 0.6,
                             "gaps": [{"team": "Nope", "focus": "f"}]}, NAMES, 0.99)
    assert '"Core"' in str(e.value) and '"Docs"' in str(e.value)


def test_parse_empty_gaps_below_threshold_is_an_error():
    for obj in ({"verdict": "gaps", "confidence": 0.5, "gaps": []},
                {"verdict": "met", "confidence": 0.9, "gaps": []}):
        with pytest.raises(ValueError, match="gaps"):
            parse_vision_review(obj, NAMES, 0.99)


def test_parse_bad_verdict_and_empty_focus():
    with pytest.raises(ValueError):
        parse_vision_review({"verdict": "maybe", "confidence": 1}, NAMES)
    with pytest.raises(ValueError, match="focus"):
        parse_vision_review({"verdict": "gaps", "confidence": 0.5,
                             "gaps": [{"team": "Core", "focus": ""}]}, NAMES)
