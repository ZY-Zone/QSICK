"""The build output is versioned, and the two version strings must agree.

Both rules are checked against the scripts rather than against a build artefact: the artefact only
exists after a build, while these are the invariants that keep it correct.

Why a versioned directory: `dist/QSICK-1.0/`. Two builds of different versions can sit side by side
without one silently replacing the other, and a folder found months later says which release it is.

Why the mismatch check: `pyproject.toml` and `src/qsick/__init__.py` each hold the version, and
nothing else ties them together. Change one and `pip`'s idea of the version silently diverges from
what `qsick --version` and OneBot's `get_version_info` report. The build is the last place this can
be caught cheaply.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _build_script() -> str:
    # `utf-8-sig`: build.ps1 carries a BOM, without which PowerShell 5.1 reads it as ANSI and the
    # Chinese text becomes a parse error.
    return (ROOT / "build.ps1").read_text(encoding="utf-8-sig")


def _spec() -> str:
    return (ROOT / "packaging" / "qsick.spec").read_text(encoding="utf-8")


def test_the_build_reads_the_version_from_pyproject():
    build = _build_script()
    assert "pyproject.toml" in build
    assert re.search(r"\$Version\s*=", build), "版本号必须从 pyproject.toml 读出，不能写死"


def test_the_output_directory_carries_the_version():
    build = _build_script()
    assert '$AppName = "QSICK-$Version"' in build
    assert "$AppDir = Join-Path $DistRoot $AppName" in build


def test_the_spec_names_the_collect_directory_the_same_way():
    """`COLLECT(name=...)` is what actually creates the folder; the two must not drift."""
    spec = _spec()
    assert 'APP_NAME = f"QSICK-{VERSION}"' in spec
    assert "name=APP_NAME" in spec
    assert re.search(r"^VERSION = _version\(\)", spec, re.MULTILINE)


def test_the_exe_itself_keeps_its_plain_name():
    """Versioning the folder is enough. Renaming the exe would break shortcuts and scripts."""
    spec = _spec()
    assert 'name="QSICK"' in spec, "EXE 仍然叫 QSICK，只有目录带版本号"
    assert 'name="QSICK-"' not in spec


def test_a_version_mismatch_stops_the_build():
    build = _build_script()
    assert "版本号不一致" in build
    assert "src\\qsick\\__init__.py" in build
    # A mismatch must abort, not warn: a warning scrolls past and the artefact ships anyway.
    assert re.search(r'if \(\$InitVersion -ne \$Version\) \{\s*throw', build)


def test_both_version_strings_currently_agree():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    runtime = re.search(r'^__version__\s*=\s*"([^"]+)"',
                        (ROOT / "src" / "qsick" / "__init__.py").read_text(encoding="utf-8"),
                        re.MULTILINE)
    assert declared and runtime, "两处版本号都要能读到"
    assert declared.group(1) == runtime.group(1)
