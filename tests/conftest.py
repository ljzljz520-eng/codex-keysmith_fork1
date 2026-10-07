"""Session-wide test environment for codex-instruct.

Capability-aware deployment resolves the installed Codex version via
``codex --version`` and fails closed without evidence.  CI/dev machines may
not have a codex binary on PATH, so every test session gets a deterministic
fake ``codex`` (rust-cli 0.160.1, model_instructions_file generation)
prepended to PATH.  Both in-process calls (``shutil.which``) and CLI
subprocesses inherit it.  Tests that exercise the fail-closed branch opt out
explicitly with ``--codex-bin /nonexistent`` or by stripping PATH.
"""

import os
import stat
import sys

import pytest

_FAKE_CODEX_VERSION = "codex-cli 0.160.1"


@pytest.fixture(scope="session", autouse=True)
def _fake_codex_on_path(tmp_path_factory):
    bin_dir = tmp_path_factory.mktemp("fake-codex-bin")
    if os.name == "nt":
        codex_path = bin_dir / "codex.cmd"
        codex_path.write_text(
            "@echo off\r\n"
            f"echo {_FAKE_CODEX_VERSION}\r\n",
            encoding="utf-8",
        )
    else:
        codex_path = bin_dir / "codex"
        codex_path.write_text(
            "#!/bin/sh\n"
            f'echo "{_FAKE_CODEX_VERSION}"\n',
            encoding="utf-8",
        )
        codex_path.chmod(codex_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    original_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{original_path}"
    yield
    os.environ["PATH"] = original_path


@pytest.fixture()
def fake_codex_factory(tmp_path):
    """Build on demand a fake codex executable reporting a chosen version."""

    def _factory(version_text: str = _FAKE_CODEX_VERSION, name: str = "codex"):
        if os.name == "nt" and not name.endswith(".cmd"):
            name = f"{name}.cmd"
        codex_path = tmp_path / name
        if os.name == "nt":
            codex_path.write_text(
                "@echo off\r\n"
                f"echo {version_text}\r\n",
                encoding="utf-8",
            )
        else:
            codex_path.write_text(
                "#!/bin/sh\n"
                f'echo "{version_text}"\n',
                encoding="utf-8",
            )
            codex_path.chmod(
                codex_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
            )
        return codex_path

    return _factory


if sys.platform == "win32":  # pragma: no cover - exercised only on Windows CI
    # Ensure .CMD resolution works with shutil.which on stock Windows runners.
    os.environ.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
