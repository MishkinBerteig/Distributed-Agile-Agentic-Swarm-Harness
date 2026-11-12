"""Connected agents without a database (docs/DESIGN-0.4.md section 5): reply file blocks, the
owner's approval step, the connected work prompt and its size rule, handover and max_jobs,
config validation, delivery rows, the MCP tool set per protocol, the Help topic and the API view."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import approval, help_connect, models, policy, prompts, services
from app import connected_service as CS
from app import mcp_server as M
from app import remote as R
from app.models import ConnectedAgent, HarnessSpec, SwarmConfig
from app.routing import validate_config
from tests.unit.agent_fakes import make_agent, make_task

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def ca(**kw) -> ConnectedAgent:
    base = dict(id="ca-1", name="Jane", protocol="a2a", url="http://jane:18789",
                endpoint="http://jane:18789/a2a/v1", token="daash_a2a_secret",
                mcp_token_hash="h", created_at=NOW, kind="openclaw")
    base.update(kw)
    return ConnectedAgent(**base)


# -- reply file blocks ------------------------------------------------------------------------------

def test_blocks_parse_files_deletions_and_summary():
    text = ("I did it.\n<<<FILE docs/a.md>>>\n# A\nOverview\n<<<END FILE>>>\n"
            "  <<<FILE b.txt>>>\nline1\n\nline3\n<<<END FILE>>>\n<<<DELETE old.md>>>\nAll good.")
    b = R.parse_file_blocks(text)
    assert b.files == [("docs/a.md", b"# A\nOverview"), ("b.txt", b"line1\n\nline3")]
    assert b.deleted == ["old.md"] and not b.problems
    assert b.summary == "I did it.\n\nAll good."


def test_blocks_reject_bad_paths_and_report_problems():
    text = "\n".join(f"<<<FILE {p}>>>\nx\n<<<END FILE>>>" for p in (
        "/etc/passwd", "../up.txt", ".daash/staging/x", ".git/config", "ok.md")) + \
        "\n<<<FILE open.md>>>\nno end"
    b = R.parse_file_blocks(text)
    assert [p for p, _ in b.files] == ["ok.md"]
    assert len(b.problems) == 5 and "no <<<END FILE>>> line" in b.problems[-1]


def test_blocks_limits_and_later_wins():
    text = ("<<<FILE a.md>>>\nfirst\n<<<END FILE>>>\n<<<FILE a.md>>>\nsecond\n<<<END FILE>>>\n"
            "<<<FILE big.md>>>\n" + "x" * 50 + "\n<<<END FILE>>>\n"
            "<<<FILE c.md>>>\nc\n<<<END FILE>>>\n<<<FILE d.md>>>\nd\n<<<END FILE>>>")
    b = R.parse_file_blocks(text, max_files=2, max_file_bytes=20)
    assert b.files == [("a.md", b"second"), ("c.md", b"c")]
    assert any("big.md" in p for p in b.problems) and any("d.md" in p for p in b.problems)
    # a deletion after a write of the same path wins, and the reverse
    b = R.parse_file_blocks("<<<FILE a.md>>>\nx\n<<<END FILE>>>\n<<<DELETE a.md>>>")
    assert b.files == [] and b.deleted == ["a.md"]


def test_rules_quoted_in_prose_are_not_blocks():
    """The prompt mentions the markers inline; an agent quoting them writes no file."""
    rules = prompts.connected_rules("job-1")
    b = R.parse_file_blocks("Summary. " + rules)
    assert b.files == [] and b.deleted == []


def test_apply_blocks_stays_inside(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "gone.md").write_text("bye")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    written, gone = R.apply_file_blocks(root, [("a/b.md", b"hi"), ("link/evil.md", b"x")],
                                        ["gone.md", "missing.md"])
    assert written == ["a/b.md"] and gone == ["gone.md"]
    assert (root / "a/b.md").read_text() == "hi" and not (outside / "evil.md").exists()


# -- the owner's approval step ------------------------------------------------------------------------

def _patch_from(command: str) -> dict:
    m = re.search(r"<<'DAASH_PATCH'\n(.*)\nDAASH_PATCH", command, re.S)
    return json.loads(m.group(1))


def test_openclaw_step_is_one_patch_with_both_tokens():
    step = approval.approval_step(ca(), "daash_a2a_X", "dca_Y", "http://daash.ts.net:8001/")
    assert step["command"].startswith("openclaw config patch --stdin <<'DAASH_PATCH'\n")
    patch = _patch_from(step["command"])
    assert patch["channels"]["a2a"]["enabled"] is True
    assert patch["channels"]["a2a"]["peers"]["daash"]["token"] == "daash_a2a_X"
    mcp = patch["mcp"]["servers"]["daash"]
    assert mcp == {"url": "http://daash.ts.net:8001/mcp", "transport": "streamable-http",
                   "headers": {"Authorization": "Bearer dca_Y"}}
    assert step["patch"] == patch and step["a2a_token"] == "daash_a2a_X"
    assert step["mcp_token"] == "dca_Y" and not step["warnings"]
    assert "Jane" in step["message"] and json.dumps(patch, indent=2) in step["message"]
    assert step["restart"] == approval.OPENCLAW_RESTART


def test_step_warns_without_a_public_url():
    step = approval.approval_step(ca(), "a", "dca_b", "")
    assert step["mcp_url"] == "http://<this-machine>:8001/mcp"
    assert step["warnings"] and "DAASH_AGENT_PUBLIC_URL" in step["warnings"][0]


def test_hermes_step():
    h = ca(protocol="hermes-api", kind="hermes", name="Hermes (box)", endpoint=None)
    step = approval.approval_step(h, "daash_hermes_K", "dca_Z", "http://d:8001")
    assert "API_SERVER_ENABLED=true\nAPI_SERVER_KEY=daash_hermes_K" in step["env"]
    assert "API_SERVER_HOST=0.0.0.0" in step["env"]
    assert 'url: "http://d:8001/mcp"' in step["mcp_yaml"]
    assert 'Authorization: "Bearer dca_Z"' in step["mcp_yaml"]
    assert step["command"].endswith(approval.HERMES_RESTART) and step["api_key"] == "daash_hermes_K"


def test_generic_a2a_step():
    step = approval.approval_step(ca(kind="a2a"), "tok", "dca_1", "http://d:8001")
    assert "tok" in step["command"] and "http://d:8001/mcp" in step["command"]


def test_new_tokens_are_strong_and_distinct():
    a, b = approval.new_peer_token(), approval.new_peer_token()
    assert a != b and len(a) > 40 and a.startswith("daash_a2a_")
    assert approval.new_api_key().startswith("daash_hermes_")


# -- the connected work prompt ---------------------------------------------------------------------------

def _task(**kw):
    t = make_task()
    return t.model_copy(update=kw) if kw else t


def test_prompt_order_and_rules():
    t = _task()
    text, carried = prompts.connected_work_prompt(
        "# Vision\nPrimes.", t, t.criteria, "Fix the README.", "a.md (3 bytes)",
        {"a.md": "old"}, [SimpleNamespace(from_name="Delivery-2", audience="direct", text="hi")],
        job_id="job-42", capabilities=["edit_files"])
    assert text.startswith("# Vision\nPrimes.\n\n---\n\nJob: do this task for DAASH")
    order = [text.index(s) for s in ("Job:", f"Task {t.id}", "Acceptance criteria",
                                     "previous attempt was rejected", "Existing workspace files",
                                     "Current content of this task's files",
                                     "Messages from your team", "Your DAASH job id: job-42",
                                     "pass job=job-42", "Finish with a short summary")]
    assert order == sorted(order)
    assert len(carried) == 1


def test_prompt_trims_in_order_and_never_cuts_the_task():
    t = _task(description="Write the module. " * 5)
    msgs = [SimpleNamespace(from_name=f"D{i}", audience="direct", text="m" * 300) for i in range(6)]
    args = ("S" * 3000, t, t.criteria, "F" * 3000, "L" * 3000, {"a.md": "E" * 3000}, msgs)
    full, carried = prompts.connected_work_prompt(*args, job_id="j")
    assert len(carried) == 6
    budget = len(full.encode()) - 2500
    text, carried = prompts.connected_work_prompt(*args, job_id="j", max_bytes=budget)
    assert len(text.encode()) <= budget
    assert "E" * 3000 not in text and "F" * 3000 in text and len(carried) == 6   # excerpts first
    budget = len(full.encode()) - 8000
    text, carried = prompts.connected_work_prompt(*args, job_id="j", max_bytes=budget)
    assert len(text.encode()) <= budget and "L" * 3000 not in text and "F" * 3000 not in text
    tiny, carried = prompts.connected_work_prompt(*args, job_id="j", max_bytes=2500)
    assert carried == [] and "S" * 3000 not in tiny
    for must in (t.name, t.description.strip(), prompts._crit_lines(t.criteria),
                 prompts.connected_rules("j")):
        assert must in tiny


def test_prompt_without_file_tools_for_agent_hosts():
    assert "list_files, read_file" not in prompts.connected_rules("j", file_tools=False)


# -- handover and max_jobs ----------------------------------------------------------------------------

def _row(aid, name, harness="daash", provider="default", state="idle", **kw):
    return make_agent(aid, name, harness=harness, state=state).model_copy(
        update={"provider": provider, **kw})


def test_handover_picks_least_recently_active_capable_other_agent():
    jane = _row("a1", "Delivery-1", "connected", "ca-1", state="busy")
    jane2 = _row("a2", "Delivery-2", "connected", "ca-1")
    d3 = _row("a3", "Delivery-3", last_active_at=NOW)
    d4 = _row("a4", "Delivery-4", last_active_at=NOW - timedelta(hours=1))
    task = _task(assigned_agent_id="a1")
    assert policy.handover_target(jane, task, [jane, jane2, d3, d4], [task]).id == "a4"
    needs = _task(assigned_agent_id="a1", required_capabilities=["run_code"])
    coder = _row("a5", "Delivery-5", capabilities=["edit_files", "run_code"],
                 last_active_at=NOW)
    assert policy.handover_target(jane, needs, [jane, d3, d4, coder], [needs]).id == "a5"
    assert policy.handover_target(jane, needs, [jane, jane2, d4], [needs]) is None


def test_connected_load_counts_in_progress_across_rows():
    r1 = _row("a1", "Delivery-1", "connected", "ca-1")
    r2 = _row("a2", "Delivery-2", "connected", "ca-1")
    r3 = _row("a3", "Delivery-3")
    tasks = [_task(id="t1", status="In-Progress", assigned_agent_id="a1"),
             _task(id="t2", status="In-Progress", assigned_agent_id="a2"),
             _task(id="t3", status="Review", assigned_agent_id="a2"),
             _task(id="t4", status="In-Progress", assigned_agent_id="a3")]
    assert policy.connected_load([r1, r2, r3], tasks) == {"ca-1": 2}


# -- config and delivery rows ------------------------------------------------------------------------

def _cfg(*specs, allowed=("daash", "connected")):
    c = SwarmConfig()
    c.allowed_harnesses = list(allowed)
    c.delivery_harnesses = list(specs)
    return c


def test_validate_connected_rows():
    ok = _cfg(HarnessSpec(harness="connected", connected_agent="ca-1"), HarnessSpec())
    assert validate_config(ok) == []
    assert validate_config(ok, {"ca-1": ca()}) == []
    assert "does not exist" in " ".join(validate_config(ok, {}))
    assert "disabled" in " ".join(validate_config(ok, {"ca-1": ca(enabled=False)}))
    none = _cfg(HarnessSpec(harness="connected"))
    assert "names no connected agent" in " ".join(validate_config(none))
    not_allowed = _cfg(HarnessSpec(harness="connected", connected_agent="ca-1"),
                       allowed=("daash",))
    assert "not in allowed_harnesses" in " ".join(validate_config(not_allowed))
    wrong = _cfg(HarnessSpec(harness="codex", connected_agent="ca-1"),
                 allowed=("daash", "codex"))
    assert "use harness 'connected'" in " ".join(validate_config(wrong))
    coord = _cfg()
    coord.coordinator.harness = "connected"
    assert "coordinator.harness cannot be 'connected'" in " ".join(validate_config(coord))


def test_delivery_spec_and_capabilities_of_connected_rows():
    cfg = _cfg(HarnessSpec(harness="connected", connected_agent="ca-1"),
               HarnessSpec(harness="connected", connected_agent="ca-2",
                           capabilities=["edit_files", "web_research"]))
    assert services.delivery_spec(cfg, 1) == ("connected", "ca-1")
    assert services.delivery_capabilities(cfg, 1, ["edit_files", "run_code"]) == \
        ["edit_files", "run_code"]
    assert services.delivery_capabilities(cfg, 1) == ["edit_files"]
    assert services.delivery_capabilities(cfg, 2, ["edit_files", "run_code"]) == \
        ["edit_files", "web_research"]
    only = _cfg(allowed=("connected", "daash"))
    assert services.delivery_spec(only, 1) == ("daash", "default")


# -- MCP: file tools only for direct agents ----------------------------------------------------------

def test_mcp_file_tools_only_for_directly_connected_agents():
    direct = M.Identity("connected", connected=ca())
    hosted = M.Identity("connected", connected=ca(protocol="runner", url=None, endpoint=None,
                                                  runner_id="r1", local_ref="main"))
    job = M.Identity("job", job_id="j")
    names = lambda ident: {t["name"] for t in M.mcp_tools(M.has_file_tools(ident),
                                                          ident.kind == "connected")}
    assert {"write_file", "read_file"} <= names(direct)
    assert not {"write_file", "read_file", "list_files"} & names(hosted)
    assert not {"write_file"} & names(job)
    assert "edit the shared swarm workspace" in M.instructions(direct)
    assert "edit the shared" not in M.instructions(hosted)


# -- the API view and status ------------------------------------------------------------------------

def test_view_never_shows_secrets_and_derives_status():
    v = CS.agent_view(ca(last_error="[token_rejected] HTTP 401", status="connected"), now=NOW)
    assert "token" not in v and "mcp_token_hash" not in v
    assert "daash_a2a_secret" not in json.dumps(v)
    assert (v["availability"], v["error_kind"], v["last_error"]) == ("token_rejected",
                                                                      "token_rejected", "HTTP 401")
    assert v["label"] == "Jane (A2A)" and v["has_token"] is True
    assert CS.availability_status(ca(last_error="[unreachable] x")) == "unreachable"
    assert CS.availability_status(ca(last_ok_at=NOW)) == "available"
    assert CS.availability_status(ca()) == "unchecked"
    assert CS.availability_status(ca(enabled=False)) == "disabled"
    assert CS.availability_status(ca(status="pending")) == "pending"
    hosted = ca(protocol="runner", runner_id="r1")
    assert CS.availability_status(hosted, runner_online=False) == "offline"
    exp = CS.agent_view(ca(status="pending", pending_until=NOW - timedelta(minutes=1)), now=NOW)
    assert exp["pending_expired"] is True


# -- Help: "Connecting agents" -----------------------------------------------------------------------

def test_connect_help_fragment_is_safe_and_rendered_from_the_code():
    page = help_connect.connect_html()
    assert page.startswith('<header class="mast">') and f"<h1>{help_connect.TITLE}</h1>" in page
    tags = {t.lower() for t in re.findall(r"<!?([A-Za-z][\w-]*)", page)}
    assert not tags & {"doctype", "html", "head", "body", "title", "meta", "style", "script",
                       "main", "iframe", "link"}
    assert not re.search(r'(src|href)="https?://', page)
    ids = set(re.findall(r' id="([^"]+)"', page))
    links = re.findall(r'href="#([^"]+)"', page)
    assert links and set(links) <= ids
    text = html.unescape(page)
    oc = approval.approval_step(SimpleNamespace(name="Jane", kind="openclaw", protocol="a2a"),
                                help_connect.SAMPLE_PEER, help_connect.SAMPLE_MCP,
                                help_connect.SAMPLE_PUBLIC)
    assert oc["command"] in text
    for must in ("DAASH_DISCOVERY_HOSTS", "scripts/tailnet-hosts.sh", "DAASH_AGENT_BIND_ADDR",
                 "DAASH_AGENT_PUBLIC_URL", "Tailscale", "write_file", "send_message",
                 "hands the task to another team member"):
        assert must in text


async def test_teammates_address_a_connected_agent_by_its_own_name():
    from app import agent_tools as A
    from tests.unit.agent_fakes import World
    w = World()
    jane_row = w.d2.model_copy(update={"harness": "connected", "provider": "ca-1"})
    w.repos.agents.by_id[jane_row.id] = jane_row
    w.repos.connected.by_id["ca-1"] = ca()
    aliases, labels = await A.connected_names(w.repos, "s1", "tm1")
    assert aliases == {"jane": jane_row.id}
    assert labels[jane_row.id] == "Jane, a connected agent over A2A"
    assert await A.connected_names(w.repos, "s1", "tm2") == ({}, labels)
    reg = A.ToolRegistry(A.build_agent_tools(w.ctx(name_aliases=aliases, agent_labels=labels),
                                             workspace=False))
    out = await reg.call("send_message", {"to": "Jane", "text": "hello"})
    assert not out.error and out.output == "message sent to Delivery-2"
    roster = (await reg.call("team_roster", {})).output
    assert "- Delivery-2: Mission Delivery, Jane, a connected agent over A2A" in roster


def test_default_names():
    from app import connectors as C
    oc = C.parse_card({"name": "OpenClaw", "description": "OpenClaw agent gateway",
                       "skills": [{"id": "main", "name": "main"}]})
    assert CS.default_name(oc, "openclaw", "janes-mac-mini.tail1.ts.net") == \
        "OpenClaw (janes-mac-mini)"
    named = C.parse_card({"name": "OpenClaw", "skills": [{"id": "jane", "name": "Jane"}]})
    assert CS.default_name(named, "openclaw", "x") == "Jane"
    several = C.parse_card({"name": "Team gate", "skills": [{"id": "a", "name": "A"},
                                                            {"id": "b", "name": "B"}]})
    assert CS.default_name(several, "a2a", "10.0.0.5") == "Team gate"
    assert CS.default_name(C.CardInfo(name=""), "a2a", "10.0.0.5") == "A2A agent (10.0.0.5)"
