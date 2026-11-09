"""DAASHboard online help: the agent-prompts page (app/help.py)."""
from __future__ import annotations

import html
import re

from app import help as H
from app import jobs as J
from app import models
from app import prompts as P


def test_page_has_every_role_and_job():
    page = H.prompts_page()
    assert page.startswith("<title>DAASH Agent Prompts</title>")
    for _, title, _, _ in H.ROLES:
        assert f">{title}</h3>" in page
    for job in H._jobs():
        assert f'id="job-{job["id"]}"' in page
    for anchor in ("overview", "system", "memory", "jobs", "tools", "external", "rules", "code"):
        assert f'id="{anchor}"' in page and f'href="#{anchor}"' in page


def test_examples_are_rendered_from_the_live_prompt_code():
    page = html.unescape(H.prompts_page())
    # the exact current wording, not a copy that could drift
    assert P.SCOPE_RULE.strip() in page
    assert P.work_prompt(H.TASK, H.TASK.criteria, None, "", (), capabilities=H.NATIVE) in page
    assert P.bootstrap_prompt(models.Limits().max_teams, True) in page
    assert H._system("mission_delivery") in page
    assert "[command check: python3 -m unittest]" in page
    assert "path=None" not in page
    # every job's example is non-empty and starts like the job it documents
    for job in H._jobs():
        text = job["example"]()
        assert len(text) > 100
        assert "Job:" in text


def test_limits_and_models_come_from_the_code():
    page = H.prompts_page()
    assert f"{J.FILE_CLIP:,} characters" in page
    assert f"{J.ASSESS_TOTAL_CHARS:,} characters in all" in page
    cfg = models.SwarmConfig()
    assert f"default model <code>{cfg.role_models.quality_judgement}</code>" in page
    assert f"<code>{cfg.coordinator.model}</code>" in page
    assert f"{models.DoDThresholds().rubric}" in page


def test_tools_table_lists_every_tool():
    page = H.prompts_page()
    for name in ("list_files", "read_file", "write_file", "append_file", "delete_file",
                 "create_subtask", "remember", "recall", "propose_team", "vote_on_proposal",
                 "submit"):
        assert f"<code>{name}</code>" in page


def test_full_document_and_theme_tokens():
    doc = H.prompts_page(full_document=True)
    assert doc.startswith("<!doctype html>") and doc.endswith("</html>")
    assert 'name="viewport"' in doc
    # every colour is a token with a dark value, and the body sets its background
    assert "@media (prefers-color-scheme: dark)" in doc and ':root[data-theme="dark"]' in doc
    assert "body { margin: 0; background: var(--bg)" in doc
    # no external resources: the page works offline inside DAASHboard
    assert not re.search(r'(src|href)="https?://', doc)
