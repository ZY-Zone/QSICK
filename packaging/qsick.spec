# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for QSICK.

What is frozen here, and what is not
------------------------------------
Only the interpreter and the third-party dependencies are frozen. The `qsick` package itself is
shipped as *source* beside the exe (see `build.ps1`), because QSICK resolves its directories from
`Path(__file__)` -- `parents[2]` of a module under `src/qsick/` is the project root, and `runtime/`,
`temp/`, `downloads/`, `protocols/` and the native DLLs all hang off it. Freezing the package into
the archive would put `__file__` inside the extraction directory, and every one of those paths would
point at a directory that disappears when the process exits.

So the build output is:

    dist/QSICK/QSICK.exe        this spec
    dist/QSICK/_internal/       python + dependencies (PyInstaller)
    dist/QSICK/src/             the qsick package, copied by build.ps1 from `git ls-files`
    dist/QSICK/protocols/       the protocol registry, likewise
    dist/QSICK/native/          qsick_pipe.dll / qsick_payload.dll / inithook.node
    dist/QSICK/build/QQNT.dll   the QQNT host shim
    dist/QSICK/plugins/         bundled plugins
    dist/QSICK/data|logs|.../   created empty; the app writes here

`packaging/launcher.py` puts `<app>/src` on `sys.path` before importing, so a frozen run sees the same
layout a checkout has.

Why every dependency is listed by hand
--------------------------------------
PyInstaller discovers imports by reading the entry script. The entry script does not import
`fastapi` -- it imports `qsick`, which arrives at runtime from a path PyInstaller never sees -- so the
dependency graph would come out empty and the exe would fail on its first import. Listing the direct
dependencies here makes them roots of the analysis, and their own imports are followed from there.
"""

from PyInstaller.utils.hooks import collect_all

import os
import sys

#: PyInstaller runs this file with cwd set to wherever the user invoked it; `SPECPATH` is the
#: directory holding this file, which is the only stable anchor for the entry script.
ENTRY = os.path.join(SPECPATH, "launcher.py")

#: Direct dependencies from `pyproject.toml`, plus the distribution names that differ from the import
#: names (`python-multipart` is imported as `python_multipart`, and older code says `multipart`).
#:
#: `collect_all` rather than a plain hidden import: it also takes each package's submodules and data
#: files, which matters for the ones that load submodules by string (`uvicorn.loops.auto`,
#: `uvicorn.protocols.http.auto`, `anyio`'s backends).
DEPENDENCIES = (
    "fastapi",
    "starlette",
    "uvicorn",
    "jinja2",
    "pydantic",
    "pydantic_core",
    "python_multipart",
    "multipart",
    "websockets",
    "httpx",
    "httpcore",
    "anyio",
    "h11",
    "click",
    "certifi",
    "idna",
    "sniffio",
    "typing_extensions",
    "annotated_types",
    "typing_inspection",
)

datas = []
binaries = []
hiddenimports = []

for package in DEPENDENCIES:
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception as exc:  # noqa: BLE001 - a missing optional name must not abort the build
        print(f"[qsick.spec] skipping {package}: {exc}")
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# Modules the standard library imports lazily by name. Encodings are pulled in by `httpx`/`uvicorn`
# for URL and header handling; without them the failure is at request time, not at start.
hiddenimports += [
    "encodings.idna",
    "encodings.punycode",
    "email.mime.multipart",
    "email.mime.text",
    "http.cookiejar",
    "json.decoder",
    "logging.handlers",
    "urllib.request",
    "ctypes.wintypes",
]

# Qt/notebook/test scaffolding pulled in transitively and never used. Excluding them keeps the build
# small and avoids shipping a second GUI toolkit inside a web-UI app.
excludes = [
    "tkinter",
    "unittest",
    "pydoc_data",
    "pytest",
    "_pytest",
    "IPython",
    "matplotlib",
    "numpy",
    "pandas",
    "PIL",
]


a = Analysis(
    [ENTRY],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="QSICK",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # No console window -- this is what `隐藏窗口启动.bat` was for. `launcher.py` redirects
    # stdout/stderr to `logs/console.log`, because a windowed process otherwise loses every message.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="QSICK",
)
