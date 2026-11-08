"""Tool layer for the native "daash" agent harness (docs/DESIGN-0.1.md section 8).

A `Tool` is a named async function the model may call. `ToolRegistry` exposes
OpenAI-format specs and executes calls; failures never raise, they come back as
error text the model can act on. `workspace_tools` gives sandboxed file tools.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

MAX_FILE_BYTES = 1_000_000
READ_LIMIT_CHARS = 20_000
LIST_LIMIT = 500


class ToolError(Exception):
    """A tool call failed in a way the model should be told about."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict], Awaitable[str]]
    terminal: bool = False


@dataclass
class ToolOutcome:
    output: str
    terminal: bool = False
    error: bool = False


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.files_written: list[str] = []
        for t in tools or []:
            self.add(t)

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def has_terminal(self) -> bool:
        return any(t.terminal for t in self._tools.values())

    def terminal_name(self) -> str | None:
        return next((t.name for t in self._tools.values() if t.terminal), None)

    def specs(self) -> list[dict]:
        return [
            {"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": t.parameters}}
            for t in self._tools.values()
        ]

    async def call(self, name: str, arguments_json: str | dict | None) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutcome(f"Error: unknown tool {name!r}. Available tools: "
                               f"{', '.join(self._tools) or '(none)'}.", error=True)
        if isinstance(arguments_json, dict):
            args: Any = arguments_json
        else:
            raw = (arguments_json or "").strip() or "{}"
            try:
                args = json.loads(raw)
            except ValueError as exc:
                return ToolOutcome(f"Error: arguments for {name} are not valid JSON ({exc}). "
                                   "Call the tool again with a valid JSON object.", error=True)
        if not isinstance(args, dict):
            return ToolOutcome(f"Error: arguments for {name} must be a JSON object.", error=True)
        try:
            out = await tool.handler(args)
        except ToolError as exc:
            return ToolOutcome(f"Error: {exc}", error=True)
        except Exception as exc:  # noqa: BLE001 - tools must never crash the loop
            return ToolOutcome(f"Error: {name} failed: {type(exc).__name__}: {exc}", error=True)
        return ToolOutcome(str(out), terminal=tool.terminal)


def safe_path(root: Path, rel: str) -> Path:
    """Resolve `rel` inside `root`; reject absolute paths, `..` and symlink escapes."""
    if not isinstance(rel, str):
        raise ToolError("path must be a string")
    rel = rel.strip() or "."
    if rel.startswith(("/", "\\")) or Path(rel).is_absolute() or (len(rel) > 1 and rel[1] == ":"):
        raise ToolError(f"absolute paths are not allowed: {rel!r}; use a path relative to the workspace")
    base = Path(root).resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise ToolError(f"path escapes the workspace: {rel!r}") from None
    return target


def _str_arg(args: dict, key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str):
        raise ToolError(f"missing or non-string argument {key!r}")
    return val


def workspace_tools(root: Path, registry: ToolRegistry) -> list[Tool]:
    """File tools sandboxed to `root`. Writes are recorded in registry.files_written."""
    root = Path(root)
    base = root.resolve()

    def rel_of(p: Path) -> str:
        return p.relative_to(base).as_posix()

    def record(p: Path) -> None:
        r = rel_of(p)
        if r not in registry.files_written:
            registry.files_written.append(r)

    async def list_files(args: dict) -> str:
        target = safe_path(root, args.get("path") or ".")
        if not target.exists():
            return "(empty: path does not exist)"
        if target.is_file():
            return f"{rel_of(target)}  {target.stat().st_size} bytes"
        lines: list[str] = []
        total = 0
        for p in sorted(target.rglob("*")):
            try:
                real = p.resolve()
                real.relative_to(base)
            except (ValueError, OSError):
                continue
            if not real.is_file():
                continue
            total += 1
            if len(lines) < LIST_LIMIT:
                lines.append(f"{rel_of(real)}  {real.stat().st_size} bytes")
        if not lines:
            return "(empty)"
        if total > LIST_LIMIT:
            lines.append(f"... {total - LIST_LIMIT} more files not shown")
        return "\n".join(lines)

    async def read_file(args: dict) -> str:
        target = safe_path(root, _str_arg(args, "path"))
        if not target.is_file():
            raise ToolError(f"no such file: {args['path']}")
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > READ_LIMIT_CHARS:
            return (text[:READ_LIMIT_CHARS]
                    + f"\n[truncated: showing {READ_LIMIT_CHARS} of {len(text)} characters]")
        return text

    def check_size(content: str) -> None:
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ToolError(f"content exceeds the {MAX_FILE_BYTES} byte file limit")

    async def write_file(args: dict) -> str:
        target = safe_path(root, _str_arg(args, "path"))
        content = _str_arg(args, "content")
        check_size(content)
        if target == base or target.is_dir():
            raise ToolError("path is a directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        record(target)
        return f"wrote {rel_of(target)} ({len(content)} characters)"

    async def append_file(args: dict) -> str:
        target = safe_path(root, _str_arg(args, "path"))
        content = _str_arg(args, "content")
        if target == base or target.is_dir():
            raise ToolError("path is a directory")
        existing = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        check_size(existing + content)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(content)
        record(target)
        return f"appended to {rel_of(target)} ({len(content)} characters)"

    async def delete_file(args: dict) -> str:
        target = safe_path(root, _str_arg(args, "path"))
        if not target.is_file():
            raise ToolError(f"no such file: {args['path']}")
        target.unlink()
        r = rel_of(target)
        if r in registry.files_written:
            registry.files_written.remove(r)
        return f"deleted {r}"

    path_prop = {"type": "string", "description": "Path relative to the workspace root"}
    content_prop = {"type": "string", "description": "Text content"}
    return [
        Tool("list_files", "List files in the workspace (recursive) with sizes.",
             {"type": "object", "properties": {"path": {**path_prop, "default": "."}}}, list_files),
        Tool("read_file", "Read a text file from the workspace.",
             {"type": "object", "properties": {"path": path_prop}, "required": ["path"]}, read_file),
        Tool("write_file", "Create or overwrite a file in the workspace (parent folders are created).",
             {"type": "object", "properties": {"path": path_prop, "content": content_prop},
              "required": ["path", "content"]}, write_file),
        Tool("append_file", "Append text to a file in the workspace (created if missing).",
             {"type": "object", "properties": {"path": path_prop, "content": content_prop},
              "required": ["path", "content"]}, append_file),
        Tool("delete_file", "Delete a file in the workspace.",
             {"type": "object", "properties": {"path": path_prop}, "required": ["path"]}, delete_file),
    ]
