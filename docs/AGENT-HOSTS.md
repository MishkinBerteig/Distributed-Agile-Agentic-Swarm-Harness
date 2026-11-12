# Connecting agents behind a firewall: agent hosts

Some agents can make outbound connections but nobody can connect to them. An example is an OpenClaw
agent on a production machine whose firewall must not change. DAASH reaches such an agent through
an **agent host**: you run one command on the agent's machine. The command installs a small DAASH
runner there, and the runner:

* dials out to DAASH, so nothing on the machine listens for connections;
* finds the machine's agents (OpenClaw agents, Hermes Agent profiles);
* hands DAASH's work to them through their own command-line tools.

The agents keep their own models, credentials, memory, tools and safety rules. DAASH adds two
things: a working folder for each job, and (unless you say no) DAASH's tools, which give the agent
memory, team messages and the team roster.

Design: docs/DESIGN-0.4.md section 5.9. Runner details: docs/RUNNER.md, "Agent hosts".

## What you need

On the **DAASH machine**:

* DAASH 0.4 running (`docker compose up --build -d`). The image contains the runner download.
* The agent port reachable from the agent's machine. The usual setup is Tailscale, with these
  lines in DAASH's `.env`:
  ```
  DAASH_AGENT_BIND_ADDR=0.0.0.0              # or this machine's Tailscale IP
  DAASH_AGENT_PUBLIC_URL=http://<daash-machine>.<tailnet>.ts.net:8001
  ```
  After changing `.env`, run `docker compose up --build -d` again. Port 8001 serves only `/health`,
  DAASH's MCP server and the runner protocol. The admin API (8000) stays private.

On the **agent's machine** (macOS or Linux):

* `python3` 3.9 or newer. macOS's own `/usr/bin/python3` (3.9) works, and so does Homebrew's.
  Nothing else is installed: the runner is one file and uses only the Python standard library.
* `curl`.
* The agent, working as usual:
  * OpenClaw: `openclaw agents list` shows it, and its gateway is running.
  * Hermes Agent: `hermes profile list` shows the profile, and `hermes chat` works.
* A user account that stays logged in, because the runner runs as a per-user service.
* No admin rights. The firewall needs no change.

## The one command

1. **Get a connect code.** It works once and expires after 60 minutes.
   * In DAASHboard: *Agents & models → Connect agents on another machine*. Copy the command it
     shows.
   * Or on the DAASH machine:
     ```
     curl -s -X POST http://127.0.0.1:8000/runner-enrollments \
          -H "Authorization: Bearer $DAASH_API_TOKEN"
     ```
     Leave out the header if `DAASH_API_TOKEN` is not set. The answer's `command` field is the
     command.

2. **Run it on the agent's machine**, in a terminal, as the user the agents run as:
   ```
   curl -fsSLo ~/daash-runner.pyz http://<daash>:8001/runner/daash-runner.pyz && python3 ~/daash-runner.pyz connect http://<daash>:8001 <CODE>
   ```
   Options you can add at the end:
   * `--no-tools`: do not give the agents DAASH's tools.
   * `--no-service`: do not install a background service. You then run
     `python3 ~/daash-runner.pyz run` yourself.
   * `--name NAME`: the runner's name in DAASH. The default is the host name.

3. **Read what it printed.** It lists:
   * the agents it found and whether each is available;
   * how they appear in DAASH;
   * the DAASH tools it added;
   * the service it installed;
   * every file it changed.

The agents now appear in DAASHboard under *Agents & models → Connected agents* with protocol
`runner` and status `connected`. Installing the runner counts as your approval, so there is no
second step. Swarm delivery rows can now pick them.

## What it changes on the agent's machine

| What | Where | Undone by `disconnect` |
|---|---|---|
| The runner itself | `~/daash-runner.pyz` | no (delete it yourself) |
| Its settings and runner token (mode 0600) | `~/.daash-runner/config.json` | yes |
| Logs | `~/.daash-runner/logs/` | no, kept on purpose |
| Job folders (logs of each job, kept 3 days) | `~/.daash-runner/jobs/` | yes |
| DAASH tools for an OpenClaw agent | `mcp.servers.daash` in `~/.openclaw/openclaw.json`, added with `openclaw mcp add` | yes (`openclaw mcp unset daash`) |
| DAASH tools for a Hermes profile | `mcp_servers.daash` in the profile's `config.yaml` (written with `hermes config set`), and `DAASH_MCP_TOKEN` in the profile's `.env` | yes |
| Background service (macOS) | `~/Library/LaunchAgents/org.daash.runner.plist` | yes |
| Background service (Linux) | `~/.config/systemd/user/daash-runner.service` | yes |
| While a job runs (OpenClaw) | a folder `<agent workspace>/daash-jobs/<job>/`, removed after the job; a session `agent:<id>:daash:<job id>` in the agent's session list | folder: yes |

The command never overwrites a `daash` MCP server that points somewhere else. It leaves that server
alone, and the agent works without DAASH tools.

OpenClaw keeps MCP servers for all its agents in one place. So on a machine with several OpenClaw
agents, only the default agent gets DAASH tools. The others still work for DAASH, without the tools.

## Check that it works

On the agent's machine:

```
python3 ~/daash-runner.pyz status        # DAASH reachable, service running, agents and their tools
tail -f ~/.daash-runner/logs/runner.log   # "hello ok", then "leased job ..." when work arrives
openclaw mcp list                         # shows: daash   (OpenClaw agents)
hermes config get mcp_servers.daash.url   # DAASH's MCP URL (Hermes profiles)
```

On the DAASH machine, check either of these:

* DAASHboard: *Agents & models*. The agent is `connected`, and its runner is online.
* The code's result:
  ```
  curl -s http://127.0.0.1:8000/runner-enrollments/<id> -H "Authorization: Bearer $DAASH_API_TOKEN"
  ```
  `runner` and `agents` are filled in once the machine has said hello.

Once a swarm gives the agent a task:

* the runner log shows `relaying to <agent> ...`;
* the swarm's activity shows the agent's DAASH tool calls, for example `remember`;
* the task moves to Review with the files the agent wrote.

## Logs

| File | What is in it |
|---|---|
| `~/.daash-runner/logs/runner.log` | the runner: hellos, discovery problems, leases, relays, results (rotates at 2 MB) |
| `~/.daash-runner/logs/service.out.log`, `service.err.log` | what the service printed (launchd / systemd) |
| `~/.daash-runner/jobs/<job id>/` | per job: `message.md` (what the agent was sent), `stdout.log` and `stderr.log` (the agent CLI's output) |
| Linux only | `journalctl --user -u daash-runner` |

## Undo

```
python3 ~/daash-runner.pyz disconnect
```

This command:

* stops and removes the service;
* removes the DAASH tools it added (and only those);
* tells DAASH, which disables this runner and shows the agents as `offline`;
* deletes the config and the job folders.

It keeps the logs. Delete what is left if you like: `rm -rf ~/.daash-runner ~/daash-runner.pyz`.

You can connect again later with a new code. DAASH then recognises the same agents on the same
machine and keeps their entries, so swarm settings that name them keep working.

**If `~/daash-runner.pyz` is gone**, undo by hand:

* macOS:
  ```
  launchctl bootout gui/$(id -u)/org.daash.runner
  rm ~/Library/LaunchAgents/org.daash.runner.plist
  openclaw mcp unset daash
  rm -rf ~/.daash-runner
  ```
* Linux:
  ```
  systemctl --user disable --now daash-runner.service
  rm ~/.config/systemd/user/daash-runner.service
  ```
* Hermes, for each profile (set `HERMES_HOME` to the profile's folder first):
  ```
  hermes config unset mcp_servers.daash
  ```
  Then delete the `DAASH_MCP_TOKEN` line from the profile's `.env`.

Then disable or delete the runner in DAASHboard (Runners panel).

## Worked example: Fitz on a production Mac

This example uses:

* **Fitz**: an OpenClaw agent (agent id `main`, identity name "Fitz") on **m3u512gb**, a production
  Mac (Apple Silicon). Its firewall must not change, and nothing can connect to it.
* OpenClaw 2026.10.9 at `/opt/homebrew/bin/openclaw`. Its gateway runs on `127.0.0.1:18789`, and
  Fitz's model is `instant` on the Mac's local LLM gateway.
* Homebrew's Python at `/opt/homebrew/bin/python3`.
* DAASH on **m5m128gb**, both machines on the same tailnet.

### On m5m128gb (DAASH)

1. In the DAASH folder, add these lines to `.env`:
   ```
   DAASH_AGENT_BIND_ADDR=0.0.0.0
   DAASH_AGENT_PUBLIC_URL=http://m5m128gb.tail391a32.ts.net:8001
   ```
   Restart: `docker compose up --build -d`.
2. Check that the agent port answers on the tailnet:
   ```
   curl -s http://m5m128gb.tail391a32.ts.net:8001/health
   ```
   The answer is `{"status":"ok","service":"agent-api"}`.
3. Create a connect code, in DAASHboard (*Agents & models → Connect agents on another machine*) or
   with:
   ```
   curl -s -X POST http://127.0.0.1:8000/runner-enrollments -H "Authorization: Bearer $DAASH_API_TOKEN"
   ```
   Copy the `command` it returns. It looks like this, with your own code at the end:
   ```
   curl -fsSLo ~/daash-runner.pyz http://m5m128gb.tail391a32.ts.net:8001/runner/daash-runner.pyz && python3 ~/daash-runner.pyz connect http://m5m128gb.tail391a32.ts.net:8001 dre_XXXXXXXXXXXXXXXXXXXXXXXX
   ```
   The code works once, for 60 minutes.

### On m3u512gb (Fitz's Mac), in Terminal, as Fitz's owner

1. Check the basics. Each line should answer.
   ```
   python3 --version                                       # 3.9 or newer
   openclaw --version                                      # OpenClaw 2026.10.9 ...
   openclaw agents list                                    # shows main (Fitz)
   curl -s http://127.0.0.1:18789/health                   # {"ok":true,"status":"live"}
   curl -s http://m5m128gb.tail391a32.ts.net:8001/health   # {"status":"ok","service":"agent-api"}
   ```
   If `curl` to m5m128gb fails, Tailscale is not up on one of the two Macs, or DAASH's port 8001 is
   not published. See step 1 on m5m128gb.
2. Paste the command from DAASH and press Enter. It prints something like:
   ```
   Connecting this machine (m3u512gb) to DAASH at http://m5m128gb.tail391a32.ts.net:8001 ...
   Enrolled as runner 'm3u512gb'.

   Agents found on this machine: 1
     - Fitz (openclaw 'main'): available - OpenClaw 2026.10.9 (8b18b55) agent 'main', model ...; gateway live on 127.0.0.1:18789

   In DAASH they appear as:
     - Fitz (connected)

   DAASH tools (MCP server 'daash'):
     - Fitz: added MCP server 'daash' (http://m5m128gb.tail391a32.ts.net:8001/mcp) to OpenClaw's config

   Service: installed and started the LaunchAgent /Users/<you>/Library/LaunchAgents/org.daash.runner.plist (label org.daash.runner)

   Changed on this machine:
     - wrote /Users/<you>/.daash-runner/config.json (mode 0600; holds this machine's DAASH runner token)
     - openclaw:main: DAASH tools added
     - /Users/<you>/Library/LaunchAgents/org.daash.runner.plist: user service that runs the DAASH runner
   Logs: /Users/<you>/.daash-runner/logs
   Undo everything: python3 ~/daash-runner.pyz disconnect
   ```
   If there are other agents, it also lists any Hermes Agent profiles it found.
3. Verify:
   ```
   python3 ~/daash-runner.pyz status
   openclaw mcp list                                        # - daash
   launchctl print gui/$(id -u)/org.daash.runner | grep -m1 "state ="   # state = running
   tail -n 20 ~/.daash-runner/logs/runner.log               # hello ok: runner 'm3u512gb'
   ```
   DAASHboard now lists **Fitz** as a connected agent with protocol `runner`. Pick "Fitz" for a
   delivery row of a swarm. The model column reads "its own model": Fitz keeps using `instant` on
   its own gateway.
4. When a swarm gives Fitz a task, the runner does the following:
   * copies the swarm's files into `~/.openclaw/workspace/daash-jobs/<job>/` (Fitz's workspace);
   * runs one turn through Fitz's gateway:
     ```
     openclaw agent --agent main --session-key daash:<job id> --message-file ... --json --timeout ...
     ```
   * uploads what Fitz changed;
   * removes the folder.

   You can follow the job in `~/.daash-runner/logs/runner.log`. Each job runs in its own Fitz
   session, so Fitz's main conversation is not touched.

### Undo on m3u512gb

```
python3 ~/daash-runner.pyz disconnect
rm -f ~/daash-runner.pyz            # optional
```

Fitz is then `offline` in DAASH, and its OpenClaw config is back as it was, without the `daash`
MCP server.

## Troubleshooting

* **"This connect code was already used" / "expired" / "not valid".** Codes work once, for 60
  minutes. Create a new one.
* **"cannot reach DAASH at ..."**
  * Check `curl http://<daash>:8001/health` from the agent's machine.
  * Check Tailscale on both machines.
  * On the DAASH machine, check `DAASH_AGENT_BIND_ADDR` and `docker compose ps agent-api`.
* **"This machine is already connected".** Run `python3 ~/daash-runner.pyz disconnect` first.
* **An agent is "NOT available" or `offline` in DAASH.**
  * OpenClaw: its gateway is not reachable on loopback. Run `openclaw gateway status` or
    `openclaw gateway start`.
  * The runner reports again every 5 minutes. Restarting the service reports at once:
    * macOS: `launchctl kickstart -k gui/$(id -u)/org.daash.runner`
    * Linux: `systemctl --user restart daash-runner`
* **"`openclaw mcp add daash` failed".** OpenClaw checks the MCP server before saving it.
  * Check that the agent's machine reaches `http://<daash>:8001/mcp`.
  * The runner tries again at its next hello.
  * `status` shows the reason.
* **"OpenClaw already has an MCP server named 'daash' (...)".** Someone else's `daash` server is
  kept. To use DAASH's:
  1. remove it (`openclaw mcp unset daash`);
  2. set `"tools_skipped": {}` in `~/.daash-runner/config.json`;
  3. restart the service.
* **Jobs fail with "Gateway not reachable" or "Couldn't connect to the AI service".** That is the
  agent's own gateway or model server. DAASH counts it as an infrastructure problem, so it does not
  use up one of the task's attempts, and tries again later.
* **"launchctl could not load it" (macOS).** A LaunchAgent loads only into the session of a user
  who is logged in on the Mac's screen.
  1. Run `connect` in Terminal on the Mac itself, or over Screen Sharing, or over SSH while you
     are also logged in on the Mac's screen.
  2. If the plist was written but not loaded, load it with
     `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.daash.runner.plist`.
* **The service does not start (macOS).**
  * See `~/.daash-runner/logs/service.err.log` and
    `launchctl print gui/$(id -u)/org.daash.runner`.
  * The plist runs the `python3` you used for `connect`, with the PATH your terminal had then.
  * If you removed that Python, run `disconnect`, then `connect` again.
* **macOS asks whether "python3" may access a folder.** The OpenClaw workspace is in a protected
  folder (Documents, Desktop). Allow it, or move the workspace.
* **The service stops when you log out (Linux).** Run `loginctl enable-linger $USER` once.
* **Limit what is shared.** In `~/.daash-runner/config.json`, set
  `"share_agents": ["main", "hermes:default"]`. Entries can be an agent id, `kind:id`, or a name.
  Then restart the service. Agents no longer shared go `offline` in DAASH.

## Security notes

* Each job's message tells the agent to work only inside its job folder. OpenClaw and Hermes run
  their tools as you, though, and do not enforce that. An agent with shell access could touch other
  files, just as it could when you ask it directly. Connect agents you would trust with that work.
  Hermes runs without `--yolo`, so its dangerous-command approvals stay on.
* DAASH's command checks run in the job folder, as you. These are acceptance tests such as
  `test -s guide.md` or `pytest -q`.
* `~/.daash-runner/config.json` holds the runner token, which lets this machine fetch jobs and swarm
  files. Each agent's DAASH tool token lets that agent act only on its own DAASH jobs. Both stop
  working when you disconnect, or when you disable the runner in DAASHboard.
* Traffic uses plain HTTP over Tailscale (WireGuard-encrypted). Without Tailscale, put DAASH's
  agent port behind HTTPS.
