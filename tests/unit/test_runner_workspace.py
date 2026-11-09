"""daash_runner.workspace: safe extraction, snapshots, change detection, result file encoding."""
import base64
import io
import os
import tarfile

import pytest

from daash_runner import workspace


def make_tar(path, members):
    """members: list of (name, bytes | ('link', target) | 'dir')."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            if data == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif isinstance(data, tuple):
                ti.type = tarfile.SYMTYPE if data[0] == "link" else tarfile.LNKTYPE
                ti.linkname = data[1]
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))


def test_extract_regular_files(tmp_path):
    tar = tmp_path / "a.tar.gz"
    make_tar(tar, [("guides", "dir"), ("guides/a.md", b"hello"), ("./b.md", b"b")])
    dest = tmp_path / "ws"
    assert workspace.safe_extract(str(tar), str(dest)) == 2
    assert (dest / "guides" / "a.md").read_bytes() == b"hello"
    assert (dest / "b.md").read_bytes() == b"b"


@pytest.mark.parametrize("name", ["../x", "a/../../x", "/abs/x", "a/../b/../../c"])
def test_extract_rejects_escape(tmp_path, name):
    tar = tmp_path / "a.tar.gz"
    make_tar(tar, [(name, b"x")])
    with pytest.raises(workspace.UnsafeArchive):
        workspace.safe_extract(str(tar), str(tmp_path / "ws"))
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("kind", ["link", "hard"])
def test_extract_rejects_links(tmp_path, kind):
    tar = tmp_path / "a.tar.gz"
    make_tar(tar, [("l", (kind, "/etc/passwd"))])
    with pytest.raises(workspace.UnsafeArchive):
        workspace.safe_extract(str(tar), str(tmp_path / "ws"))


def test_extract_size_cap(tmp_path):
    tar = tmp_path / "a.tar.gz"
    make_tar(tar, [("a", b"x" * 100)])
    with pytest.raises(workspace.UnsafeArchive):
        workspace.safe_extract(str(tar), str(tmp_path / "ws"), max_bytes=10)


def _write(root, rel, data=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_snapshot_ignores_state_folders_and_links(tmp_path):
    for rel in ("a.md", "d/b.md", ".git/x", ".claude/y", ".codex/z", ".openclaude/w", "node_modules/m/i.js", ".daash/q"):
        _write(tmp_path, rel)
    os.symlink("a.md", tmp_path / "link.md")
    snap = workspace.snapshot(str(tmp_path))
    assert set(snap) == {"a.md", "d/b.md"}


def test_diff_changed_new_deleted(tmp_path):
    _write(tmp_path, "keep.md", b"1")
    _write(tmp_path, "edit.md", b"1")
    _write(tmp_path, "gone.md", b"1")
    before = workspace.snapshot(str(tmp_path))
    _write(tmp_path, "edit.md", b"2")
    _write(tmp_path, "new/n.md", b"n")
    _write(tmp_path, ".claude/state.json", b"s")
    os.remove(tmp_path / "gone.md")
    changed, deleted = workspace.diff(before, workspace.snapshot(str(tmp_path)))
    assert changed == ["edit.md", "new/n.md"]
    assert deleted == ["gone.md"]


def test_encode_files_roundtrip(tmp_path):
    _write(tmp_path, "a/b.md", b"\x00\x01binary")
    out = workspace.encode_files(str(tmp_path), ["a/b.md"], {})
    assert out == [{"path": "a/b.md", "content_b64": base64.b64encode(b"\x00\x01binary").decode()}]


def test_encode_limits(tmp_path):
    for i in range(3):
        _write(tmp_path, f"f{i}", b"x" * 10)
    names = ["f0", "f1", "f2"]
    with pytest.raises(workspace.LimitExceeded, match="limit is 2"):
        workspace.encode_files(str(tmp_path), names, {"max_files": 2})
    with pytest.raises(workspace.LimitExceeded, match="per-file"):
        workspace.encode_files(str(tmp_path), names, {"max_file_bytes": 5})
    with pytest.raises(workspace.LimitExceeded, match="total"):
        workspace.encode_files(str(tmp_path), names, {"max_total_bytes": 25})
    assert len(workspace.encode_files(str(tmp_path), names, {"max_total_bytes": 30})) == 3


def test_snapshot_ignores_generated_python_caches(tmp_path):
    from daash_runner.workspace import snapshot
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "m.cpython-311.pyc").write_bytes(b"\x00")
    (tmp_path / "stray.pyc").write_bytes(b"\x00")
    (tmp_path / "m.py").write_text("x = 1\n")
    assert set(snapshot(str(tmp_path))) == {"m.py"}
