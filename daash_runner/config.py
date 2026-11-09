"""Runner config file loading (docs/DESIGN-0.3.md section 7).

    {"name": "...", "harnesses": {"<name>": {"command": "codex", "models": [...],
      "env": {"K": "V or ${ENV_VAR}"}, "args": [...], "timeout_seconds": 1800}}}
"""
from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any, Mapping

KNOWN_HARNESSES = ("openclaude", "claude-code", "codex", "fake")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_COMMANDS = {
    "openclaude": "openclaude",
    "claude-code": "claude",
    "codex": "codex",
    "fake": "python3",
}


def expand(value: Any, environ: Mapping[str, str] | None = None) -> Any:
    """Recursively replace ${VAR} in strings with environ[VAR] ('' when unset)."""
    env = os.environ if environ is None else environ
    if isinstance(value, str):
        return _VAR.sub(lambda m: env.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, env) for k, v in value.items()}
    return value


def load_config(path: str | None, environ: Mapping[str, str] | None = None) -> dict:
    """Load and expand the config; with no path, a default exposing every known CLI found on PATH."""
    if path:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        raw = {"harnesses": {n: {"command": c} for n, c in DEFAULT_COMMANDS.items() if n != "fake"}}
    cfg = expand(raw, environ)
    if not isinstance(cfg.get("harnesses"), dict):
        raise ValueError("config needs a 'harnesses' object")
    out: dict[str, dict] = {}
    for name, h in cfg["harnesses"].items():
        h = dict(h or {})
        h.setdefault("command", DEFAULT_COMMANDS.get(name, name))
        h["models"] = [str(m) for m in h.get("models") or []]
        h["env"] = {str(k): str(v) for k, v in (h.get("env") or {}).items()}
        h["args"] = [str(a) for a in h.get("args") or []]
        h["timeout_seconds"] = int(h.get("timeout_seconds") or 1800)
        out[name] = h
    cfg["harnesses"] = out
    return cfg


def available_harnesses(cfg: dict) -> list[dict]:
    """Harnesses announced in hello: known names whose command is on PATH."""
    found = []
    for name, h in cfg["harnesses"].items():
        if name not in KNOWN_HARNESSES:
            continue
        if shutil.which(h["command"]):
            found.append({"harness": name, "models": list(h["models"])})
    return found
