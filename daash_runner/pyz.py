"""Build the runner as one zipapp file (docs/DESIGN-0.4.md 5.9): `python3 -m daash_runner.pyz OUT`.

The coordinator image builds it once (Dockerfile) and serves it at /runner/daash-runner.pyz, so an
agent host's owner needs nothing but python3 (3.9 or newer, standard library only):

    python3 ~/daash-runner.pyz connect <url> <code>

The archive holds the `daash_runner` package (no caches) and a top-level `__main__.py` that runs
the CLI and exits with its status.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipapp

MAIN = ("import sys\n"
        "from daash_runner.__main__ import main\n"
        "sys.exit(main())\n")


def _ignore(_dir: str, names: list) -> set:
    return {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))}


def build(out_path: str, package_dir: str = "") -> str:
    """Write the zipapp to `out_path` (mode 0644) and return its absolute path."""
    package_dir = package_dir or os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.abspath(out_path)
    with tempfile.TemporaryDirectory() as stage:
        shutil.copytree(package_dir, os.path.join(stage, "daash_runner"), ignore=_ignore)
        with open(os.path.join(stage, "__main__.py"), "w", encoding="utf-8") as fh:
            fh.write(MAIN)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        zipapp.create_archive(stage, out_path, interpreter="/usr/bin/env python3", compressed=True)
    os.chmod(out_path, 0o644)
    return out_path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 -m daash_runner.pyz OUT.pyz", file=sys.stderr)
        sys.exit(2)
    print(build(sys.argv[1]))
