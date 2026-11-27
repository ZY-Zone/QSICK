"""Download-source ordering for the QQ client.

The rules being pinned down, all of them from the operator:

* GitHub first, direct — **no** gh-proxy mirrors;
* 5 s on GitHub, then switch to the project's own server;
* only a complete `runtime/` counts as success.

These are tested without touching the network: the two source functions are replaced, so what is
under test is the ordering and the reporting, not `urllib`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick import runtime_fetch  # noqa: E402


@pytest.fixture()
def empty_runtime(tmp_path, monkeypatch):
    """A `runtime/` that is present but not usable, with the download switches in their default state."""
    monkeypatch.delenv("QSICK_RUNTIME_URL", raising=False)
    monkeypatch.delenv("QSICK_SKIP_RUNTIME_DOWNLOAD", raising=False)
    monkeypatch.delenv("QSICK_QQNT_GET", raising=False)
    return tmp_path / "runtime"


def test_github_is_tried_first_then_the_server(empty_runtime, monkeypatch):
    calls: list[str] = []

    def fake_github(runtime):
        calls.append("github")
        raise runtime_fetch.RuntimeDownloadError("GitHub 不可达")

    def fake_server(runtime, base):
        calls.append(f"server:{base}")
        return True

    monkeypatch.setattr(runtime_fetch, "_fetch_from_github", fake_github)
    monkeypatch.setattr(runtime_fetch, "_fetch_from_server", fake_server)

    assert runtime_fetch.ensure_runtime(empty_runtime) is True
    assert calls == ["github", f"server:{runtime_fetch.DEFAULT_BASE_URL.rstrip('/')}"]


def test_it_stops_at_the_first_source_that_works(empty_runtime, monkeypatch):
    calls: list[str] = []

    def fake_github(runtime):
        calls.append("github")
        return True

    def fake_server(runtime, base):  # pragma: no cover - must not run
        calls.append("server")
        return True

    monkeypatch.setattr(runtime_fetch, "_fetch_from_github", fake_github)
    monkeypatch.setattr(runtime_fetch, "_fetch_from_server", fake_server)

    assert runtime_fetch.ensure_runtime(empty_runtime) is True
    assert calls == ["github"], "服务器源不该在 GitHub 成功后再跑一次"


def test_an_explicit_url_skips_github_entirely(empty_runtime, monkeypatch):
    calls: list[str] = []

    def fake_github(runtime):  # pragma: no cover - must not run
        calls.append("github")
        return True

    def fake_server(runtime, base):
        calls.append(f"server:{base}")
        return True

    monkeypatch.setattr(runtime_fetch, "_fetch_from_github", fake_github)
    monkeypatch.setattr(runtime_fetch, "_fetch_from_server", fake_server)

    assert runtime_fetch.ensure_runtime(empty_runtime, base_url="http://example.invalid/x") is True
    # Trailing slash trimmed; and no GitHub attempt, because the caller named one address.
    assert calls == ["server:http://example.invalid/x"]


def test_both_failing_names_both_in_the_error(empty_runtime, monkeypatch):
    monkeypatch.setattr(runtime_fetch, "_fetch_from_github",
                        lambda runtime: (_ for _ in ()).throw(
                            runtime_fetch.RuntimeDownloadError("GitHub 不可达")))
    monkeypatch.setattr(runtime_fetch, "_fetch_from_server",
                        lambda runtime, base: (_ for _ in ()).throw(
                            runtime_fetch.RuntimeDownloadError("服务器 503")))

    with pytest.raises(runtime_fetch.RuntimeDownloadError) as excinfo:
        runtime_fetch.ensure_runtime(empty_runtime)

    message = str(excinfo.value)
    # A failure that says only "failed" costs another round of guessing; both reasons must be there.
    assert "GitHub" in message
    assert "503" in message
    assert "GitHub" in message.split("；")[0], "GitHub 应排在前面，顺序即优先级"


def test_a_complete_runtime_is_not_downloaded_again(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in runtime_fetch.REQUIRED:
        (runtime / name).write_bytes(b"x")

    def explode(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("已经是可用的 runtime，不该再下载")

    monkeypatch.setattr(runtime_fetch, "_fetch_from_github", explode)
    monkeypatch.setattr(runtime_fetch, "_fetch_from_server", explode)

    assert runtime_fetch.ensure_runtime(runtime) is False


def test_the_github_probe_is_five_seconds():
    assert runtime_fetch.GITHUB_PROBE_TIMEOUT == 5.0


def test_no_mirrors_are_configured():
    """The gh-proxy list is gone on purpose; a reappearance is a regression, not a tweak.

    Those mirrors forwarded a 300 MB installer through third-party hosts whose availability nobody
    here controls. The supported fallback is the project's own server.

    Matched as URLs, not bare names: the comment in `qqnt_get.py` explains the history and names the
    hosts it removed, which is documentation, not usage.
    """
    assert not hasattr(runtime_fetch, "MIRRORS")
    source = (ROOT / "qqnt_get.py").read_text(encoding="utf-8")
    assert "MIRRORS" not in source
    for host in ("ghfast.top", "gh-proxy.com", "ghproxy.net", "gh.llkk.cc", "github.dpik.top"):
        assert f"https://{host}" not in source, f"{host} 不该再出现在下载逻辑里"


def test_qqnt_get_only_falls_back_when_not_asked_to_stay_on_github():
    """`--github-only` exists so `runtime_fetch` can own the ordering.

    Without it, `qqnt_get.main` would silently switch to the server itself, `runtime_fetch` would
    never see a failure, and the log would claim the GitHub source worked.
    """
    source = (ROOT / "qqnt_get.py").read_text(encoding="utf-8")
    assert "--github-only" in source
    assert '"--github-only"' in (ROOT / "src" / "qsick" / "runtime_fetch.py").read_text(encoding="utf-8")
