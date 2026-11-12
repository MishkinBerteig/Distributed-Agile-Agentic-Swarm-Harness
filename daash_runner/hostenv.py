"""Where an agent host keeps its state, and how it finds and runs the owner's agent CLIs
(docs/DESIGN-0.4.md 5.9). Standard library only; Python 3.9+.

A service (launchd, systemd) starts with a bare environment: Homebrew's /opt/homebrew/bin or an
npm global bin folder is often missing from PATH, and `openclaw` is a node script that needs
`node` on PATH. So every CLI runs with a search path built from the runner's own PATH, the PATH
saved when the owner ran `connect` (their terminal's), the owner's login shell PATH and the usual
install folders.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from typing import Mapping, Optional

STATE_DIR_NAME = ".daash-runner"
CONFIG_NAME = "config.json"
#: Install folders searched after PATH (macOS Homebrew, npm/volta/bun/n/nvm, pipx/uv tools).
COMMON_BIN_DIRS = (
    "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "~/.local/bin", "~/.volta/bin",
    "~/.bun/bin", "~/n/bin", "~/.nvm/versions/node/*/bin", "~/Library/pnpm", "~/.local/share/pnpm",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
)
#: Variables of the runner itself that never reach an agent's process.
PRIVATE_ENV = ("DAASH_RUNNER_TOKEN", "DAASH_COORDINATOR_URL", "DAASH_RUNNER_CONFIG",
               "DAASH_RUNNER_WORKDIR", "DAASH_RUNNER_MAX_JOBS", "DAASH_RUNNER_HOST",
               "DAASH_RUNNER_HOME", "DAASH_MCP_URL", "DAASH_MCP_TOKEN")


def home_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return env.get("HOME") or os.path.expanduser("~")


def expand(path: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """`~` expanded against HOME from `environ` (so tests can use a fake HOME)."""
    if path == "~" or path.startswith("~/"):
        return home_dir(environ) + path[1:]
    return path


def state_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    """~/.daash-runner, or DAASH_RUNNER_HOME."""
    env = os.environ if environ is None else environ
    return os.path.abspath(expand(env.get("DAASH_RUNNER_HOME") or f"~/{STATE_DIR_NAME}", env))


def config_path(environ: Optional[Mapping[str, str]] = None) -> str:
    return os.path.join(state_dir(environ), CONFIG_NAME)


def logs_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    return os.path.join(state_dir(environ), "logs")


def _split(path: str) -> list:
    return [p for p in (path or "").split(os.pathsep) if p]


def login_shell_path(environ: Optional[Mapping[str, str]] = None, timeout: float = 10.0) -> str:
    """The PATH of the owner's login shell ('' if it cannot be read). Runs `$SHELL -l -c`
    (zsh reads ~/.zprofile, where Homebrew puts its PATH; bash reads ~/.profile)."""
    env = dict(os.environ if environ is None else environ)
    shell = env.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
    if not os.path.isfile(shell):
        return ""
    marker = "__DAASH_PATH__="
    try:
        res = subprocess.run([shell, "-l", "-c", f'printf "%s%s" "{marker}" "$PATH"'],  # noqa: S603
                             env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    out = res.stdout or ""
    return out.rsplit(marker, 1)[1].strip() if marker in out else ""


def common_dirs(environ: Optional[Mapping[str, str]] = None) -> list:
    out = []
    for d in COMMON_BIN_DIRS:
        d = expand(d, environ)
        matches = sorted(glob.glob(d), reverse=True) if "*" in d else [d]
        out.extend(m for m in matches if os.path.isdir(m))
    return out


def search_path(environ: Optional[Mapping[str, str]] = None, saved: str = "",
                login: Optional[str] = None) -> str:
    """PATH for the owner's CLIs: the current PATH, the PATH saved at `connect`, the login
    shell's PATH (looked up when `login` is None) and the usual install folders, de-duplicated
    in that order."""
    env = os.environ if environ is None else environ
    if login is None:
        login = login_shell_path(env)
    seen, out = set(), []
    for part in _split(env.get("PATH", "")) + _split(saved) + _split(login) + common_dirs(env):
        if part not in seen:
            seen.add(part)
            out.append(part)
    return os.pathsep.join(out)


def find_cli(name: str, path: str, configured: str = "") -> Optional[str]:
    """The absolute path of an agent CLI: the configured command when it exists, else `name` on
    `path`."""
    if configured:
        if os.path.isabs(configured):
            return configured if os.access(configured, os.X_OK) else None
        return shutil.which(configured, path=path)
    return shutil.which(name, path=path)


def cli_env(path: str, environ: Optional[Mapping[str, str]] = None,
            extra: Optional[Mapping[str, str]] = None) -> dict:
    """The owner's environment for their agent CLIs: everything they have (their OpenClaw and
    Hermes settings stay in force), PATH = `path`, without the runner's own secrets."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items()
           if k not in PRIVATE_ENV}
    env["PATH"] = path
    env.setdefault("HOME", home_dir(environ))
    if extra:
        env.update(extra)
    return env


def python_for_service(environ: Optional[Mapping[str, str]] = None) -> str:
    """The python3 a service should start: `python3` on PATH when it is a link to the one running
    now (Homebrew's /opt/homebrew/bin/python3 survives upgrades; the versioned path in
    sys.executable does not), else sys.executable."""
    env = os.environ if environ is None else environ
    found = shutil.which("python3", path=env.get("PATH", ""))
    if found and sys.executable:
        try:
            if os.path.realpath(found) == os.path.realpath(sys.executable):
                return found
        except OSError:
            pass
    return sys.executable or found or "python3"
