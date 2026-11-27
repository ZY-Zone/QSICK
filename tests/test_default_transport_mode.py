"""The default transport mode.

This is the mode `start.bat` gets from a plain double-click: the script leaves `MODE` empty, runs
`uv run qsick`, and `load_config` falls back to the dataclass field when there is no `qsick.json`.

It used to be `mock`. On a fresh checkout that meant the synthetic QQ runtime started and every
message went to a fake account nobody was watching -- which looks like "the bot works but nothing
arrives", and cost a debugging round to identify.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from qsick.config import RuntimeConfig, TransportConfig, load_config  # noqa: E402


def test_the_dataclass_default_is_headless():
    assert TransportConfig().mode == "headless"


def test_a_config_without_a_transport_section_inherits_that_default():
    """A `qsick.json` that omits `transport` must not silently become mock either."""
    config = RuntimeConfig()
    assert config.transport.mode == "headless"


def test_a_missing_config_file_falls_back_to_headless(tmp_path, monkeypatch):
    """The fresh-checkout case: no `qsick.json` at all."""
    absent = tmp_path / "nope.json"
    monkeypatch.delenv("QSICK_CONFIG", raising=False)
    monkeypatch.delenv("QSICK_TRANSPORT", raising=False)
    config = load_config(str(absent))
    assert config.transport.mode == "headless"


def test_an_explicit_mode_in_the_file_still_wins(tmp_path, monkeypatch):
    """The default must not override a file that says something else."""
    monkeypatch.delenv("QSICK_TRANSPORT", raising=False)
    path = tmp_path / "qsick.json"
    path.write_text(json.dumps({"transport": {"mode": "mock"}}), encoding="utf-8")
    assert load_config(str(path)).transport.mode == "mock"


def test_the_env_override_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("QSICK_TRANSPORT", "pipe")
    path = tmp_path / "qsick.json"
    path.write_text(json.dumps({}), encoding="utf-8")
    assert load_config(str(path)).transport.mode == "pipe"


def test_the_example_config_matches_the_default():
    """`qsick.example.json` is what people copy to `qsick.json`; it must not disagree with the code."""
    example = json.loads((ROOT / "qsick.example.json").read_text(encoding="utf-8"))
    assert example["transport"]["mode"] == TransportConfig().mode
