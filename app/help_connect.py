"""DAASHboard online help, second topic: "Connecting agents" (GET /help/connect; docs/DESIGN-0.4.md
section 7). Served like the prompts guide (app/help.py): an HTML fragment that DAASHboard styles
with dashboard/src/help.css. The snippets are rendered from app/approval.py and the limits are
read from the code, so the help cannot drift from what DAASH really generates. Pure: no I/O.
"""
from __future__ import annotations

import functools
import html
from types import SimpleNamespace

from app import approval, connectors
from app import prompts as P
from app.agent_tools import tool_specs
from app.api_state import AGENT_PORT
from app.config import Settings
from app.remote import MAX_FILE_BYTES, MAX_FILES

TITLE = "Connecting agents"
_S = Settings.model_fields
SAMPLE_PUBLIC = "http://daash-host.tailnet-name.ts.net:8001"
SAMPLE_PEER = "daash_a2a_EXAMPLE-peer-token"
SAMPLE_MCP = "dca_EXAMPLE-mcp-token"
SAMPLE_KEY = "daash_hermes_EXAMPLE-api-key"


def _e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _pre(text: str, label: str, open_: bool = False) -> str:
    return (f'<details class="example"{" open" if open_ else ""}><summary>{_e(label)}</summary>'
            f"<pre>{_e(text.rstrip())}</pre></details>")


def _table(head: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (f'<div class="scroll"><table><thead><tr>{th}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def _default(name: str):
    return _S[name].default


def _what() -> str:
    return (
        '<section id="what"><h2>What a connected agent is</h2>'
        "<p>A connected agent is an agent that already exists and runs somewhere else, for "
        "example your OpenClaw agent or a Hermes Agent, with its own model, memory and tools. "
        "In a swarm it fills a delivery row like any other agent: it takes tasks, works on the "
        "shared workspace, talks to its team and has its work judged. DAASH never chooses its "
        "model.</p>"
        "<p>There are two ways to connect one. Both end in <strong>one approval step</strong> "
        "by the agent's owner.</p>"
        + _table(["Way", "When", "How DAASH reaches the agent"], [
            ["<strong>Direct</strong>", "DAASH can open a connection to the agent's machine "
             "(same network, or your tailnet).", "OpenClaw and any other A2A 1.0 agent: the "
             "A2A protocol. Hermes Agent: its API server."],
            ["<strong>Through an agent host</strong>", "Nothing can connect to the agent's "
             "machine (a firewall that must not change), but it can make outgoing "
             "connections.", "The owner runs one command there. It installs a DAASH runner that "
             "dials out to DAASH, finds the agents on that machine and passes DAASH's work to "
             "them."],
        ])
        + "</section>")


def _discovery() -> str:
    ports = ", ".join(str(p) for p in connectors.DEFAULT_PORTS)
    return (
        '<section id="discovery"><h2>Finding agents</h2>'
        "<p>The Agents page lists what DAASH found under <em>Found on your network</em>. "
        "<em>Look again</em> runs discovery now; while the page is open it runs every 10 "
        "minutes. DAASH never sends a password or token while it looks.</p>"
        + _table(["DAASH looks at", "Set by"], [
            ["The machines you name (the main way)", "<code>DAASH_DISCOVERY_HOSTS</code> in "
             "<code>.env</code>: host names or addresses, separated by commas"],
            ["The machines your runners run on", "automatic"],
            ["Every address of small private networks", "<code>DAASH_DISCOVERY_SUBNETS</code>: "
             f"for example <code>10.0.0.0/24</code>; at most /{connectors.MAX_SUBNET_PREFIX} each"],
            ["On these ports", f"<code>DAASH_DISCOVERY_PORTS</code>, default "
             f"<code>{_e(_default('DISCOVERY_PORTS'))}</code> (OpenClaw's gateway, Hermes's "
             "API server)"],
        ])
        + '<h3 id="tailnet">On a tailnet</h3>'
        "<p>When DAASH runs in Docker Desktop on a Mac, its containers usually cannot reach "
        "other machines by their LAN addresses (10.0.0.x), but they can reach them by their "
        "Tailscale names. So scanning a subnet finds nothing there, and naming the machines "
        "is what works. This prints the value for <code>.env</code> from the machines of your "
        "tailnet that are online (run it on the machine where DAASH runs; it shows names "
        "only, no keys):</p>"
        + _pre("scripts/tailnet-hosts.sh\n# DAASH_DISCOVERY_HOSTS=jane-mac.tailnet-name.ts.net,"
               "m3u512gb.tailnet-name.ts.net\n# then: docker compose up -d", "Command", True)
        + "<p>Subnet scanning still helps on Linux hosts, where containers reach the LAN.</p>"
        '<h3 id="found">What counts as found</h3>'
        + _table(["Shown as", "Means"], [
            ["OpenClaw / A2A agent", "An A2A Agent Card answered at "
             "<code>/.well-known/agent-card.json</code>. Its name, version and agents are shown."],
            ["Hermes Agent", "A Hermes API server answered at <code>/health</code>."],
            ["Something is listening but does not answer", f"{_e(connectors.SILENT_DETAIL)}"],
        ])
        + '<h3 id="by-address">Add by address</h3>'
        f"<p>Type a host name (DAASH tries ports {_e(ports)}), <code>host:port</code>, or the "
        "full URL of an Agent Card or API server (for example behind HTTPS).</p>"
        "</section>")


def _approval() -> str:
    oc = SimpleNamespace(name="Jane", kind="openclaw", protocol="a2a")
    he = SimpleNamespace(name="Hermes", kind="hermes", protocol="hermes-api")
    step = approval.approval_step(oc, SAMPLE_PEER, SAMPLE_MCP, SAMPLE_PUBLIC)
    hstep = approval.approval_step(he, SAMPLE_KEY, SAMPLE_MCP, SAMPLE_PUBLIC)
    restart = (f"Then <code>{_e(approval.OPENCLAW_RESTART)}</code>." if approval.OPENCLAW_RESTART
               else "No restart is needed: the gateway picks the change up by itself within "
                    "a second or two. " + _e(approval.OPENCLAW_RELOAD_OFF_NOTE))
    return (
        '<section id="approval"><h2>The one approval step</h2>'
        "<p><em>Connect</em> creates the agent as <strong>waiting for approval</strong> and "
        "makes two new secrets: the token DAASH will send the agent, and the token the agent "
        "will send to DAASH's tools. DAASHboard then shows the owner's single step in two "
        "forms. Use either.</p>"
        "<ol><li><strong>A command to paste on the agent's machine</strong>, as the agent's "
        "owner.</li><li><strong>A message to send your agent</strong> in your usual chat, "
        "asking it to make the same change itself. This works where the agent is allowed to "
        "change its own configuration for you.</li></ol>"
        '<h3 id="approval-openclaw">OpenClaw</h3>'
        "<p>One <code>openclaw config patch</code>: it turns on the A2A channel with DAASH as "
        f"the trusted peer <code>{approval.A2A_PEER}</code> (with its own token) and adds "
        f"DAASH's tools as the MCP server <code>{approval.MCP_SERVER}</code>. Nothing else "
        f"changes. {restart}</p>"
        + _pre(step["command"], "Example: the command (with sample tokens)", True)
        + _pre(step["message"], "Example: the message to the agent")
        + '<h3 id="approval-hermes">Hermes Agent</h3>'
        "<p>Turn on Hermes's API server with a key, give it DAASH's MCP server, and restart its "
        "gateway. DAASH suggests a strong key; to use your own, put it in "
        "<code>API_SERVER_KEY</code> and paste the same key into DAASH.</p>"
        + _pre(hstep["command"], "Example: the command (with sample secrets)", True)
        + _pre(hstep["env"], "The .env lines")
        + _pre(hstep["mcp_yaml"], "The config.yaml entry")
        + '<h3 id="approval-a2a">Other A2A agents</h3>'
        "<p>A2A has no standard configuration format, so DAASH tells you what to set: the "
        "bearer token the agent must accept from DAASH, and DAASH's MCP address and token.</p>"
        '<h3 id="detection">DAASH notices by itself</h3>'
        f"<p>While an agent waits for approval, DAASH checks it every "
        f"{int(_default('CONNECTED_PENDING_CHECK_SECONDS'))} seconds for "
        f"{_default('CONNECTED_PENDING_MINUTES')} minutes. The check costs no model tokens: "
        "for A2A it asks for a task that does not exist (\"not found\" means the token was "
        "accepted); for Hermes it lists the models. When the check passes the agent becomes "
        "<strong>connected</strong> and the activity feed says so. If the hour runs out it "
        "keeps waiting for approval: <em>Try again</em> waits another hour.</p>"
        "<p>The tokens are shown only right after <em>Connect</em> and after <em>Reset "
        "tokens</em>, never again (DAASH keeps only a fingerprint of the agent's token for its "
        "tools). Lost the step? <em>Reset tokens</em> makes new ones and shows a new step; the "
        "old tokens stop working.</p>"
        "</section>")


def _host() -> str:
    return (
        '<section id="agent-host"><h2>Agents behind a firewall: agent hosts</h2>'
        "<p>For an agent on a machine nothing can connect to, DAASHboard's <em>Connect agents "
        "on another machine</em> shows one command for the owner to run there. It downloads "
        "DAASH's runner (a single Python file), connects it to DAASH with a one-time code, "
        "finds the OpenClaw and Hermes agents on that machine, gives them DAASH's tools, and "
        "installs a small background service. Nothing listens on that machine and no admin "
        "rights are needed. The agents then appear as connected agents, already approved: "
        "installing the runner was the approval.</p>"
        "<p>The runner relays each task to the agent through the agent's own command line, "
        "with the owner's own login and the agent's own model, in a folder inside the agent's "
        "workspace that it removes afterwards. Dangerous commands stay denied on the owner's "
        "machine.</p>"
        "</section>")


def _tools() -> str:
    names = [s["function"]["name"] for s in tool_specs(workspace=True)]
    files = [n for n in names if n in ("list_files", "read_file", "write_file", "append_file",
                                       "delete_file")]
    other = [n for n in names if n not in files]
    return (
        '<section id="tools"><h2>DAASH tools</h2>'
        "<p>The approval step also gives the agent DAASH's tools, through DAASH's MCP server "
        f"(<code>{approval.MCP_SERVER}</code>, at <code>/mcp</code> on DAASH's agent port). "
        "Each agent has its own token; a call works only while the agent has an active DAASH "
        "job, and every call shows up in the activity feed and in the agent's details. "
        "<em>DAASH tools: in use</em> on the Agents page means the agent really calls "
        "them.</p>"
        + _table(["Tools", "For"], [
            [", ".join(f"<code>{n}</code>" for n in other), "Memory, the team roster, team "
             "messages, the current task, subtasks and team proposals: the same tools DAASH's "
             "own agents have."],
            [", ".join(f"<code>{n}</code>" for n in files), "Directly connected agents have no "
             "copy of the files, so they read and write the shared workspace with these (the "
             "same path rules and size limits as DAASH's own agent). Agents on an agent host "
             "work on a local copy and do not get them."],
        ])
        + "<p>Teammates can message a connected agent by its row name (Delivery-2) or by its "
          "own name (Jane).</p>"
        "</section>")


def _work() -> str:
    sample = P.connected_rules("6f1c2d3e-…")
    return (
        '<section id="work"><h2>How a task runs</h2>'
        "<ol>"
        "<li>DAASH sends the agent one message: its usual system message (Vision, Mission, "
        "Definition of Done, learnings and memories) followed by the task, its acceptance "
        "criteria, any rework feedback, the workspace listing, the task's current files, "
        "unread team messages, the DAASH job id and the rules below. A2A has no separate "
        f"system channel. The message is kept under {P.CONNECTED_PROMPT_MAX_BYTES:,} bytes: "
        "file excerpts, then the listing, then the feedback, then the newest team messages "
        "give way first; the task, its criteria and the rules never do.</li>"
        "<li>The agent works with its own tools and DAASH's. Without DAASH's tools it puts "
        f"each file in its reply as a block (at most {MAX_FILES} files of "
        f"{MAX_FILE_BYTES:,} bytes each); DAASH writes them with the same rules as its own "
        "agent's file tools.</li>"
        f"<li>DAASH asks how it is going every {int(_default('CONNECTED_POLL_SECONDS'))} "
        "seconds (more slowly when the agent has several DAASH tasks), to stay under "
        "OpenClaw's limit of 30 requests a minute for each peer.</li>"
        "<li>The final reply is the summary. The task then goes to Review and is judged like "
        "any other work.</li>"
        "<li>If the work is sent back, DAASH cannot give the agent a stronger model, so it "
        "<strong>hands the task to another team member</strong> instead, when an idle one "
        "with the right capabilities exists. Otherwise the same agent tries again.</li>"
        "</ol>"
        + _pre(sample, "The rules at the end of the message")
        + _table(["Status", "Means"], [
            ["available", "The last check passed."],
            ["waiting for approval", "Connected in DAASH, not yet approved by the owner."],
            ["token rejected", "The agent refused DAASH's token: approval missing or undone. "
             "Reset tokens."],
            ["unreachable", "No answer. DAASH waits and retries without counting an attempt; "
             "after the swarm's remote fallback time it runs the task on its own agent."],
            ["offline", "An agent host's agent is no longer reported, or its runner is "
             "offline."],
            ["disabled", "You turned it off. Its rows use DAASH's own agent meanwhile."],
        ])
        + "<p>One connected agent can fill several delivery rows. Its <em>max jobs</em> caps how "
          "many tasks it works on at once, across all of them.</p>"
        "</section>")


def _network() -> str:
    return (
        '<section id="network"><h2>Networking</h2>'
        f"<p>Connected agents and remote runners reach DAASH on its <strong>agent port</strong> "
        f"({AGENT_PORT}), which serves only <code>/health</code>, <code>/mcp</code> and "
        "<code>/runner/*</code>. The admin API and DAASHboard stay on their own ports.</p>"
        + _table(["Setting", "Set it to"], [
            ["<code>DAASH_AGENT_BIND_ADDR</code>", "Where the agent port listens: "
             "<code>127.0.0.1</code> (default, this machine only), this machine's Tailscale "
             "address, or <code>0.0.0.0</code>."],
            ["<code>DAASH_AGENT_PUBLIC_URL</code>", "The address other machines use for it, "
             f"for example <code>{_e(SAMPLE_PUBLIC)}</code>. The approval step puts it into "
             "the agent's MCP setting. When it is not set, DAASHboard warns you and the step "
             "has a placeholder."],
            ["<code>DAASH_DISCOVERY_HOSTS</code>", "The machines discovery looks at."],
        ])
        + "<p>Tailscale is the easy way: every machine reaches every other by name, and "
          "nothing is exposed to the internet. After changing <code>.env</code>, restart with "
          "<code>docker compose up -d</code>.</p>"
        "</section>")


def _limits() -> str:
    return (
        '<section id="limits"><h2>Limitations</h2><ul>'
        "<li>A2A cannot cancel a running task. When you pause the swarm, DAASH closes the job: "
        "the agent's DAASH tools stop working for it and a late reply is ignored.</li>"
        "<li>An OpenClaw gateway forgets its A2A tasks when it restarts. DAASH notices and sends "
        "the task again, without counting an attempt.</li>"
        "<li>A2A messages are text only, and OpenClaw accepts at most 64 KiB per message.</li>"
        "<li>OpenClaw offers MCP tools to its model through its own tool search, so an agent "
        "may need a nudge to use DAASH's tools; the work message names them.</li>"
        "<li>DAASH does not see a connected agent's model or token use, so they are not in the "
        "token and cost totals.</li>"
        "<li>Command checks need a runner; a directly connected agent has none, so tasks with "
        "command checks go to agents that can run code.</li>"
        "<li>Changing a connected agent's capabilities applies to teams created afterwards.</li>"
        "<li>The Swarm Coordinator, the judges and planning always run on DAASH's own agent.</li>"
        "</ul></section>")


@functools.cache
def connect_html() -> str:
    toc = [("what", "What a connected agent is", False), ("discovery", "Finding agents", False),
           ("tailnet", "On a tailnet", True), ("by-address", "Add by address", True),
           ("approval", "The one approval step", False),
           ("approval-openclaw", "OpenClaw", True), ("approval-hermes", "Hermes Agent", True),
           ("detection", "DAASH notices by itself", True),
           ("agent-host", "Agents behind a firewall", False), ("tools", "DAASH tools", False),
           ("work", "How a task runs", False), ("network", "Networking", False),
           ("limits", "Limitations", False)]
    sub_cls = ' class="sub"'
    nav = "".join(f'<a href="#{i}"{sub_cls if sub else ""}>{_e(t)}</a>' for i, t, sub in toc)
    return (
        f'<header class="mast"><span class="eyebrow">DAASHboard help</span><h1>{_e(TITLE)}</h1>'
        '<p class="lede">How to bring agents that already exist, such as your OpenClaw and '
        "Hermes agents, into a swarm: finding them, the owner's one approval step, DAASH's "
        "tools, and the network settings.</p></header>"
        f'<div class="layout"><nav class="toc" aria-label="Contents">{nav}</nav>'
        '<div class="help-main">'
        + _what() + _discovery() + _approval() + _host() + _tools() + _work() + _network()
        + _limits() + "</div></div>")
