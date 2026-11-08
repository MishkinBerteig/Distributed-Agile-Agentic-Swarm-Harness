import os

import pytest

from app import checks
from app.models import AcceptanceCriterion


def crit(kind, path=None, arg=None):
    return AcceptanceCriterion(id="c", task_id="t", ordinal=1, text="x",
                               check_kind=kind, check_path=path, check_arg=arg)


# --- is_deterministic / validate_check_spec ----------------------------------

def test_is_deterministic():
    for k in ("file_exists", "file_contains", "min_words", "json_valid"):
        assert checks.is_deterministic(k)
    assert not checks.is_deterministic("rubric")
    assert not checks.is_deterministic("nonsense")


@pytest.mark.parametrize("kind,path,arg", [
    ("rubric", None, None), ("rubric", "a.md", None),
    ("file_exists", "a.md", None), ("json_valid", "d/a.json", None),
    ("file_contains", "a.md", r"^# \w+"), ("min_words", "a.md", "50"), ("min_words", "a.md", " 7 "),
])
def test_validate_ok(kind, path, arg):
    assert checks.validate_check_spec(kind, path, arg) is None


@pytest.mark.parametrize("kind,path,arg,fragment", [
    ("bogus", "a", None, "Unknown check kind"),
    ("file_exists", None, None, "path"),
    ("file_exists", "  ", None, "path"),
    ("json_valid", "/etc/passwd", None, "absolute"),
    ("file_exists", "../x", None, ".."),
    ("file_exists", "a/../../x", None, ".."),
    ("file_exists", "a\\..\\x", None, ".."),
    ("file_contains", "a", None, "regular expression"),
    ("file_contains", "a", "", "regular expression"),
    ("file_contains", "a", "(unclosed", "invalid regular expression"),
    ("min_words", "a", None, "positive integer"),
    ("min_words", "a", "abc", "positive integer"),
    ("min_words", "a", "0", "positive integer"),
    ("min_words", "a", "-3", "positive integer"),
    ("min_words", "a", "2.5", "positive integer"),
    ("rubric", "/abs", None, "absolute"),
])
def test_validate_errors(kind, path, arg, fragment):
    err = checks.validate_check_spec(kind, path, arg)
    assert err is not None and fragment in err


# --- run_check: basics -------------------------------------------------------

def test_rubric_raises(tmp_path):
    with pytest.raises(ValueError):
        checks.run_check(crit("rubric"), tmp_path)


def test_file_exists(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("hi")
    assert checks.run_check(crit("file_exists", "sub/a.txt"), tmp_path).passed
    r = checks.run_check(crit("file_exists", "sub/missing.txt"), tmp_path)
    assert not r.passed and "does not exist" in r.evidence
    r = checks.run_check(crit("file_exists", "sub"), tmp_path)
    assert not r.passed and "not a regular file" in r.evidence


def test_file_contains_case_insensitive_multiline(tmp_path):
    (tmp_path / "a.md").write_text("# Title\n\nThe QUICK brown fox\nlast line\n")
    r = checks.run_check(crit("file_contains", "a.md", "quick brown"), tmp_path)
    assert r.passed and r.evidence == "The QUICK brown fox"
    r = checks.run_check(crit("file_contains", "a.md", r"^last"), tmp_path)   # ^ is per line
    assert r.passed and r.evidence == "last line"
    r = checks.run_check(crit("file_contains", "a.md", "zebra"), tmp_path)
    assert not r.passed and "No match" in r.evidence


def test_file_contains_evidence_truncated(tmp_path):
    (tmp_path / "a.txt").write_text("needle " + "x" * 1000)
    r = checks.run_check(crit("file_contains", "a.txt", "needle"), tmp_path)
    assert r.passed and len(r.evidence) <= checks.EVIDENCE_MAX_CHARS and r.evidence.endswith("...")


def test_file_contains_bad_regex_and_missing_arg(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    r = checks.run_check(crit("file_contains", "a.txt", "(oops"), tmp_path)
    assert not r.passed and "regular expression" in r.evidence
    assert not checks.run_check(crit("file_contains", "a.txt", None), tmp_path).passed


def test_file_contains_missing_file(tmp_path):
    r = checks.run_check(crit("file_contains", "nope.txt", "x"), tmp_path)
    assert not r.passed and "does not exist" in r.evidence


def test_min_words(tmp_path):
    (tmp_path / "a.md").write_text("one two  three\nfour\tfive")
    assert checks.run_check(crit("min_words", "a.md", "5"), tmp_path).passed
    r = checks.run_check(crit("min_words", "a.md", "6"), tmp_path)
    assert not r.passed and "5 words" in r.evidence
    assert not checks.run_check(crit("min_words", "a.md", "abc"), tmp_path).passed
    (tmp_path / "empty.md").write_text("")
    assert not checks.run_check(crit("min_words", "empty.md", "1"), tmp_path).passed


def test_json_valid(tmp_path):
    (tmp_path / "ok.json").write_text('{"a": [1, 2]}')
    (tmp_path / "bad.json").write_text('{"a": ')
    (tmp_path / "empty.json").write_text("")
    assert checks.run_check(crit("json_valid", "ok.json"), tmp_path).passed
    r = checks.run_check(crit("json_valid", "bad.json"), tmp_path)
    assert not r.passed and "not valid JSON" in r.evidence
    assert not checks.run_check(crit("json_valid", "empty.json"), tmp_path).passed


def test_non_utf8_does_not_raise(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe hello \x00 world")
    assert checks.run_check(crit("file_contains", "b.bin", "hello"), tmp_path).passed
    assert not checks.run_check(crit("json_valid", "b.bin"), tmp_path).passed


def test_enum_kind_accepted(tmp_path):
    from app.models import CheckKind
    (tmp_path / "a").write_text("x")
    c = crit("file_exists", "a")
    c.check_kind = CheckKind.FILE_EXISTS
    assert checks.run_check(c, tmp_path).passed


# --- path safety -------------------------------------------------------------

def test_path_traversal_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.txt").write_text("top secret")
    for p in ("../secret.txt", "a/../../secret.txt", "..", "./../secret.txt"):
        r = checks.run_check(crit("file_contains", p, "secret"), ws)
        assert not r.passed and ".." in r.evidence, p


def test_absolute_path_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    r = checks.run_check(crit("file_exists", str(outside)), ws)
    assert not r.passed and "absolute" in r.evidence
    assert not checks.run_check(crit("file_exists", "\\windows\\x"), ws).passed
    assert not checks.run_check(crit("file_exists", "C:\\x"), ws).passed


def test_nul_and_empty_path(tmp_path):
    assert not checks.run_check(crit("file_exists", "a\x00b"), tmp_path).passed
    assert not checks.run_check(crit("file_exists", ""), tmp_path).passed
    assert not checks.run_check(crit("file_exists", None), tmp_path).passed


def test_symlink_escape_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.txt").write_text("top secret")
    os.symlink(tmp_path / "secret.txt", ws / "link.txt")
    os.symlink(tmp_path, ws / "dirlink")
    r = checks.run_check(crit("file_contains", "link.txt", "secret"), ws)
    assert not r.passed and "outside the workspace" in r.evidence
    r = checks.run_check(crit("file_exists", "dirlink/secret.txt"), ws)
    assert not r.passed and "outside the workspace" in r.evidence


def test_symlink_inside_workspace_allowed(tmp_path):
    (tmp_path / "real.txt").write_text("hello")
    os.symlink(tmp_path / "real.txt", tmp_path / "alias.txt")
    assert checks.run_check(crit("file_contains", "alias.txt", "hello"), tmp_path).passed


def test_dot_slash_path_ok(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert checks.run_check(crit("file_exists", "./a.txt"), tmp_path).passed


# --- size cap ----------------------------------------------------------------

def test_read_cap_two_megabytes(tmp_path):
    big = tmp_path / "big.txt"
    with big.open("wb") as f:
        f.write(b"word " * (checks.MAX_READ_BYTES // 5 + 10))
        f.write(b"\nTAILMARK")
    assert big.stat().st_size > checks.MAX_READ_BYTES
    r = checks.run_check(crit("file_contains", "big.txt", "TAILMARK"), tmp_path)
    assert not r.passed and "2 MB" in r.evidence                # beyond the cap, not read
    r = checks.run_check(crit("json_valid", "big.txt"), tmp_path)
    assert not r.passed and "2 MB" in r.evidence
    r = checks.run_check(crit("min_words", "big.txt", "100"), tmp_path)
    assert r.passed and "2 MB" in r.evidence


def test_file_contains_rejects_overlong_pattern():
    assert checks.validate_check_spec("file_contains", "a.md", "a" * 300) is None
    err = checks.validate_check_spec("file_contains", "a.md", "a" * 301)
    assert err and "too long" in err
