"""``qsick`` entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

from . import __version__
from .config import RuntimeConfig, load_config
from .runtime import Runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qsick",
        description="QSICK: injectable OneBot v11 runtime for QQ NT",
    )
    parser.add_argument("--config", default="", help="path to qsick.json")
    parser.add_argument("--mock", action="store_true", help="run the synthetic QQ runtime")
    parser.add_argument("--pipe", action="store_true", help="attach to the native payload")
    parser.add_argument("--log-level", default="", help="TRACE/DEBUG/INFO/WARN/ERROR")
    parser.add_argument("--data-dir", default="", help="override the data directory")
    parser.add_argument("--no-serve", action="store_true", help="run without HTTP/WebUI (tests)")
    parser.add_argument("--version", action="version", version=f"qsick {__version__}")
    return parser


def apply_args(config: RuntimeConfig, args: argparse.Namespace) -> RuntimeConfig:
    if args.mock and args.pipe:
        raise SystemExit("choose one of --mock / --pipe")
    if args.mock:
        config.transport.mode = "mock"
    if args.pipe:
        config.transport.mode = "pipe"
    if args.log_level:
        config.log.level = args.log_level.upper()
    if args.data_dir:
        config.data_dir = str(Path(args.data_dir).expanduser())
    return config


async def run(config: RuntimeConfig, *, serve: bool = True) -> None:
    runtime = Runtime(config)
    try:
        await runtime.start(serve=serve)
    except Exception as exc:  # noqa: BLE001 - startup errors must be readable
        print(f"qsick: failed to start: {type(exc).__name__}: {exc}", file=sys.stderr)
        with contextlib.suppress(Exception):
            await runtime.stop()
        raise SystemExit(1) from exc

    info = runtime.info()
    print(f"QSICK {info['version']} · transport={info['transport']} · uin={info['uin']}")
    if serve:
        print(f"  OneBot   http://{info['onebot']['host']}:{info['onebot']['port']}{info['onebot']['http_path']}")
        print(f"  OneBot   ws://{info['onebot']['host']}:{info['onebot']['port']}{info['onebot']['ws_path']}")
        print(f"  WebUI    http://{info['webui']['host']}:{info['webui']['port']}/")
    try:
        await runtime.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runtime.stop()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = apply_args(load_config(args.config or None), args)
    try:
        asyncio.run(run(config, serve=not args.no_serve))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
