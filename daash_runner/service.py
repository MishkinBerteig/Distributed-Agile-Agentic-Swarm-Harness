"""The agent host's background service (docs/DESIGN-0.4.md 5.9): a per-user service that keeps
`python3 ~/daash-runner.pyz run` going, without admin rights.

* macOS: a LaunchAgent `~/Library/LaunchAgents/org.daash.runner.plist` (RunAtLoad, KeepAlive),
  output in `~/.daash-runner/logs/`, loaded with `launchctl bootstrap gui/<uid>` (older macOS:
  `launchctl load -w`).
* Linux: a systemd user unit `~/.config/systemd/user/daash-runner.service`
  (`systemctl --user enable --now`); logs in `~/.daash-runner/logs/` and `journalctl --user`.

The file contents are pure functions of their inputs (unit-tested); installing runs the commands
through an injectable `run`.
"""
from __future__ import annotations

import os
import plistlib
import sys
from typing import Callable, Mapping, Optional, Sequence

from . import hostenv
from .discovery import call

LABEL = "org.daash.runner"
UNIT = "daash-runner.service"
LAUNCHD = "launchd"
SYSTEMD = "systemd"


def service_kind(platform: str = "") -> Optional[str]:
    platform = platform or sys.platform
    if platform == "darwin":
        return LAUNCHD
    if platform.startswith("linux"):
        return SYSTEMD
    return None


def service_file(kind: str, environ: Optional[Mapping[str, str]] = None) -> str:
    if kind == LAUNCHD:
        return hostenv.expand(f"~/Library/LaunchAgents/{LABEL}.plist", environ)
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME") or hostenv.expand("~/.config", environ)
    return os.path.join(base, "systemd", "user", UNIT)


def run_command(python: str, entry: Sequence[str]) -> list:
    """The service's command line: python3 + the zipapp (or `-m daash_runner`) + `run`."""
    return [python, *entry, "run"]


def launchd_plist(argv: Sequence[str], home: str, logs: str, path_env: str,
                  workdir: str = "") -> bytes:
    data = {
        "Label": LABEL,
        "ProgramArguments": list(argv),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "WorkingDirectory": workdir or home,
        "EnvironmentVariables": {"PATH": path_env, "HOME": home, "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": os.path.join(logs, "service.out.log"),
        "StandardErrorPath": os.path.join(logs, "service.err.log"),
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=False)


def _systemd_quote(arg: str) -> str:
    if arg and all(c.isalnum() or c in "/._-+:=@," for c in arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def systemd_unit(argv: Sequence[str], home: str, logs: str, path_env: str) -> str:
    cmd = " ".join(_systemd_quote(a) for a in argv)
    return (
        "[Unit]\n"
        "Description=DAASH runner (agent host): relays DAASH work to this machine's agents\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={cmd}\n"
        f"WorkingDirectory={_systemd_quote(home)}\n"
        f"Environment={_systemd_quote('PATH=' + path_env)}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=always\n"
        "RestartSec=15\n"
        f"StandardOutput=append:{os.path.join(logs, 'service.out.log')}\n"
        f"StandardError=append:{os.path.join(logs, 'service.err.log')}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install(kind: str, argv: Sequence[str], path_env: str,
            environ: Optional[Mapping[str, str]] = None, run: Optional[Callable] = None,
            uid: Optional[int] = None) -> tuple:
    """Write the service file and start the service. (ok, path, [messages])."""
    home = hostenv.home_dir(environ)
    logs = hostenv.logs_dir(environ)
    os.makedirs(logs, exist_ok=True)
    path = service_file(kind, environ)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    msgs = []
    if kind == LAUNCHD:
        uid = os.getuid() if uid is None else uid
        domain = f"gui/{uid}"
        call(["launchctl", "bootout", f"{domain}/{LABEL}"], environ, timeout=30, run=run)
        with open(path, "wb") as fh:
            fh.write(launchd_plist(argv, home, logs, path_env))
        code, out, err = call(["launchctl", "bootstrap", domain, path], environ, timeout=30, run=run)
        if code != 0:   # older macOS
            code, out, err = call(["launchctl", "load", "-w", path], environ, timeout=30, run=run)
        if code != 0:
            return False, path, [f"wrote {path}, but launchctl could not load it: "
                                 f"{(err or out).strip()[-300:]}"]
        msgs.append(f"installed and started the LaunchAgent {path} (label {LABEL})")
    elif kind == SYSTEMD:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(systemd_unit(argv, home, logs, path_env))
        call(["systemctl", "--user", "daemon-reload"], environ, timeout=30, run=run)
        code, out, err = call(["systemctl", "--user", "enable", "--now", UNIT], environ,
                              timeout=60, run=run)
        if code != 0:
            return False, path, [f"wrote {path}, but `systemctl --user enable --now {UNIT}` failed: "
                                 f"{(err or out).strip()[-300:]}"]
        msgs.append(f"installed and started the systemd user service {path}")
        msgs.append("to keep it running while you are logged out: loginctl enable-linger $USER")
    else:
        return False, "", [f"no user service manager known for {sys.platform}; run "
                           "`python3 ~/daash-runner.pyz run` yourself (e.g. in tmux)"]
    return True, path, msgs


def uninstall(kind: str, path: str = "", environ: Optional[Mapping[str, str]] = None,
              run: Optional[Callable] = None, uid: Optional[int] = None) -> tuple:
    """Stop the service and remove its file. (ok, [messages])."""
    path = path or service_file(kind, environ)
    msgs = []
    if kind == LAUNCHD:
        uid = os.getuid() if uid is None else uid
        code, _, _ = call(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], environ, timeout=30,
                          run=run)
        if code != 0 and os.path.exists(path):
            call(["launchctl", "unload", "-w", path], environ, timeout=30, run=run)
        msgs.append(f"stopped the LaunchAgent {LABEL}")
    elif kind == SYSTEMD:
        call(["systemctl", "--user", "disable", "--now", UNIT], environ, timeout=60, run=run)
        msgs.append(f"stopped and disabled {UNIT}")
    if path and os.path.exists(path):
        os.remove(path)
        msgs.append(f"removed {path}")
        if kind == SYSTEMD:
            call(["systemctl", "--user", "daemon-reload"], environ, timeout=30, run=run)
    return True, msgs


def status(kind: str, environ: Optional[Mapping[str, str]] = None,
           run: Optional[Callable] = None, uid: Optional[int] = None) -> str:
    path = service_file(kind, environ) if kind else ""
    if not kind:
        return "no service manager on this platform"
    if not os.path.exists(path):
        return "not installed"
    if kind == LAUNCHD:
        uid = os.getuid() if uid is None else uid
        code, out, _ = call(["launchctl", "print", f"gui/{uid}/{LABEL}"], environ, timeout=15, run=run)
        if code != 0:
            return f"installed ({path}) but not loaded"
        state = next((ln.split("=", 1)[1].strip() for ln in out.splitlines()
                      if ln.strip().startswith("state =")), "loaded")
        return f"{state} ({path})"
    code, out, _ = call(["systemctl", "--user", "is-active", UNIT], environ, timeout=15, run=run)
    return f"{(out.strip() or 'unknown')} ({path})"
