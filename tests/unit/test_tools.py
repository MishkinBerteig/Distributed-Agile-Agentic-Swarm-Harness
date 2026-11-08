"""Sandboxed tool layer (app/tools.py)."""
from __future__ import annotations

import json
import os

import pytest

from app.tools import MAX_FILE_BYTES, Tool, ToolError, ToolRegistry, safe_path, workspace_tools


def make(tmp_path):
    reg = ToolRegistry()
    for t in workspace_tools(tmp_path, reg):
        reg.add(t)
    return reg


def test_safe_path_ok_and_rejections(tmp_path):
    assert safe_path(tmp_path, "a/b.txt") == tmp_path.resolve() / "a" / "b.txt"
    assert safe_path(tmp_path, ".") == tmp_path.resolve()
    for bad in ("/etc/passwd", "../x", "a/../../x", "..", "C:\\x"):
        with pytest.raises(ToolError):
            safe_path(tmp_path, bad)


def test_safe_path_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(outside, ws / "link")
    with pytest.raises(ToolError):
        safe_path(ws, "link/secret.txt")
    with pytest.raises(ToolError):
        safe_path(ws, "link")


async def test_write_read_list_append_delete(tmp_path):
    reg = make(tmp_path)
    out = await reg.call("write_file", json.dumps({"path": "docs/a.md", "content": "hello"}))
    assert not out.error and "docs/a.md" in out.output
    assert reg.files_written == ["docs/a.md"]
    assert (await reg.call("append_file", '{"path": "docs/a.md", "content": " world"}')).output
    assert (await reg.call("read_file", '{"path": "docs/a.md"}')).output == "hello world"
    listing = (await reg.call("list_files", "{}")).output
    assert "docs/a.md" in listing and "11 bytes" in listing
    assert "docs/a.md" in (await reg.call("list_files", '{"path": "docs"}')).output
    assert (await reg.call("delete_file", '{"path": "docs/a.md"}')).output.startswith("deleted")
    assert reg.files_written == []
    assert (await reg.call("list_files", "")).output == "(empty)"


async def test_errors_become_text(tmp_path):
    reg = make(tmp_path)
    for name, args in [("nope", "{}"), ("read_file", "{bad json"), ("read_file", "[1]"),
                       ("read_file", '{"path": "missing.txt"}'),
                       ("read_file", '{"path": "../x"}'), ("write_file", '{"path": "x"}')]:
        out = await reg.call(name, args)
        assert out.error and out.output.startswith("Error"), (name, args)


async def test_handler_exception_is_caught():
    async def boom(args):
        raise RuntimeError("kaput")

    reg = ToolRegistry([Tool("boom", "d", {"type": "object"}, boom)])
    out = await reg.call("boom", "{}")
    assert out.error and "kaput" in out.output


async def test_terminal_flag_and_specs():
    async def submit(args):
        return "ok " + args.get("summary", "")

    reg = ToolRegistry([Tool("submit", "finish", {"type": "object"}, submit, terminal=True)])
    out = await reg.call("submit", '{"summary": "x"}')
    assert out.terminal and out.output == "ok x"
    spec = reg.specs()[0]
    assert spec["type"] == "function" and spec["function"]["name"] == "submit"
    assert reg.terminal_name() == "submit"


async def test_read_truncates_and_size_limit(tmp_path):
    reg = make(tmp_path)
    (tmp_path / "big.txt").write_text("x" * 25_000)
    out = (await reg.call("read_file", '{"path": "big.txt"}')).output
    assert "truncated" in out and len(out) < 20_200
    big = await reg.call("write_file", json.dumps({"path": "h.txt", "content": "y" * (MAX_FILE_BYTES + 1)}))
    assert big.error and not (tmp_path / "h.txt").exists()


async def test_list_cap_500(tmp_path):
    reg = make(tmp_path)
    for i in range(510):
        (tmp_path / f"f{i:04d}.txt").write_text("a")
    out = (await reg.call("list_files", "{}")).output.splitlines()
    assert len(out) == 501 and "10 more files" in out[-1]


async def test_symlink_escape_blocked_in_tools(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_text("secret")
    ws = tmp_path / "ws"
    ws.mkdir()
    os.symlink(outside, ws / "link")
    reg = make(ws)
    assert (await reg.call("read_file", '{"path": "link/s.txt"}')).error
    assert (await reg.call("write_file", '{"path": "link/n.txt", "content": "x"}')).error
    assert "secret" not in (await reg.call("list_files", "{}")).output
    assert not (outside / "n.txt").exists()
