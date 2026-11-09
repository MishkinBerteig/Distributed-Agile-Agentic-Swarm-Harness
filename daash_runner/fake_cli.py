"""Deterministic fake agent CLI for tests (`python3 -m daash_runner.fake_cli`).

Reads the prompt (last positional argument, or stdin), finds the task artifact path
(env DAASH_FAKE_ARTIFACT, else a line `Artifact: <path>`, else the first `*.md` path
mentioned), writes >= 120 words of plausible markdown there, and prints stream-json
lines ending with a Claude-Code-style `result` event.

DAASH_FAKE_MODE: ok | fail | error | hang | slow   (slow sleeps DAASH_FAKE_SLOW_SECONDS, default 3)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

_ARTIFACT_LINE = re.compile(r"Artifact\s*[:=]\s*[`'\"]?([^\s`'\"]+)", re.I)
_MD_PATH = re.compile(r"[`'\"(\s]([\w][\w./-]*\.md)\b")


def find_artifact(prompt: str, env_value: str = "") -> str:
    if env_value:
        return env_value
    m = _ARTIFACT_LINE.search(prompt)
    if m:
        return m.group(1).rstrip(".,;)")
    m = _MD_PATH.search(" " + prompt)
    return m.group(1) if m else "deliverable.md"


def make_content(path: str, prompt: str) -> str:
    title = os.path.splitext(os.path.basename(path))[0].replace("-", " ").replace("_", " ").title() or "Deliverable"
    paras = [
        f"# {title}",
        "## Overview",
        "This document was produced by the deterministic fake agent used to exercise the DAASH runner "
        "protocol end to end. It is plain markdown with several sections, so that simple automated checks "
        "such as a minimum word count, the presence of headings and the absence of placeholder text all pass.",
        "## Details",
        "The runner copies the shared swarm workspace to a private folder, starts this command inside it, "
        "waits for it to finish, and uploads every file whose content changed. Each step is logged, and the "
        "heartbeat keeps the lease alive while the command runs. A cancelled lease stops the command at once.",
        "## Steps",
        "1. Read the task and its acceptance criteria from the prompt.\n"
        "2. Write the requested artifact into the working directory.\n"
        "3. Reply with a short summary describing what was done and how each criterion is met.",
        "## Summary",
        "The artifact meets the stated criteria: it has a clear title, structured sections, concrete steps and "
        "a closing summary, and it contains no unfinished markers. Reviewers can verify it by reading the "
        "headings above and counting the words in the body.",
    ]
    return "\n\n".join(paras) + "\n"


def _emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def main(argv: list[str]) -> int:
    prompt = ""
    # `--flag value` pairs: drop the values of known flags
    skip = False
    pos = []
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("--model", "--system-prompt"):
            skip = True
            continue
        if a.startswith("--"):
            continue
        pos.append(a)
    if pos:
        prompt = pos[-1]
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()

    mode = os.environ.get("DAASH_FAKE_MODE", "ok")
    _emit({"type": "system", "subtype": "init", "model": "fake", "cwd": os.getcwd()})

    if mode == "hang":
        while True:
            time.sleep(1)
    if mode == "slow":
        time.sleep(float(os.environ.get("DAASH_FAKE_SLOW_SECONDS", "3")))
    if mode == "error":
        text = "API Error: 503 service unavailable: model gateway unreachable (connection refused)"
        _emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": text,
               "usage": {"input_tokens": 0, "output_tokens": 0}, "total_cost_usd": 0})
        print(text, file=sys.stderr)
        return 1
    if mode == "fail":
        _emit({"type": "result", "subtype": "error_max_turns", "is_error": True,
               "result": "I could not finish the task within the allowed turns.",
               "usage": {"input_tokens": 200, "output_tokens": 20}, "total_cost_usd": 0})
        return 1

    path = find_artifact(prompt, os.environ.get("DAASH_FAKE_ARTIFACT", ""))
    full = os.path.join(os.getcwd(), path)
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    body = make_content(path, prompt)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(body)
    summary = f"Wrote {path} ({len(body.split())} words) covering overview, details, steps and summary."
    _emit({"type": "assistant", "message": {"content": [{"type": "text", "text": summary}]}})
    _emit({"type": "result", "subtype": "success", "is_error": False, "result": summary,
           "usage": {"input_tokens": max(1, len(prompt) // 4), "output_tokens": len(body) // 4},
           "total_cost_usd": 0})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
