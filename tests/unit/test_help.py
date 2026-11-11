"""DAASHboard online help: the agent-prompts page (app/help.py)."""
from __future__ import annotations

import html
import re

from app import help as H
from app import jobs as J
from app import models
from app import prompts as P


def test_page_has_every_role_and_job():
    page = H.prompts_html()
    assert page.startswith('<header class="mast">') and f"<h1>{H.TITLE}</h1>" in page
    for _, title, _, _ in H.ROLES:
        assert f">{title}</h3>" in page
    for job in H._jobs():
        assert f'id="job-{job["id"]}"' in page
    for anchor in ("overview", "system", "memory", "jobs", "tools", "external", "rules", "code"):
        assert f'id="{anchor}"' in page and f'href="#{anchor}"' in page


def test_examples_are_rendered_from_the_live_prompt_code():
    page = html.unescape(H.prompts_html())
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
    page = H.prompts_html()
    assert f"{J.FILE_CLIP:,} characters" in page
    assert f"{J.ASSESS_TOTAL_CHARS:,} characters in all" in page
    cfg = models.SwarmConfig()
    assert f"default model <code>{cfg.role_models.quality_judgement}</code>" in page
    assert f"<code>{cfg.coordinator.model}</code>" in page
    assert f"{models.DoDThresholds().rubric}" in page


def test_tools_table_lists_every_tool():
    page = H.prompts_html()
    for name in ("list_files", "read_file", "write_file", "append_file", "delete_file",
                 "create_subtask", "remember", "recall", "propose_team", "vote_on_proposal",
                 "submit"):
        assert f"<code>{name}</code>" in page


def test_fragment_is_safe_to_embed_in_daashboard():
    """DAASHboard injects the fragment into its Help view and styles it (help.css)."""
    page = H.prompts_html()
    tags = {t.lower() for t in re.findall(r"<!?([A-Za-z][\w-]*)", page)}
    assert not tags & {"doctype", "html", "head", "body", "title", "meta", "style", "script",
                       "main", "iframe", "link"}
    # no external resources: the help works offline
    assert not re.search(r'(src|href)="https?://', page)
    # every in-page link has a target (DAASHboard turns them into #help/<id>)
    ids = set(re.findall(r' id="([^"]+)"', page))
    links = re.findall(r'href="#([^"]+)"', page)
    assert links and set(links) <= ids
    assert len(ids) == len(re.findall(r' id="', page)), "duplicate ids"
