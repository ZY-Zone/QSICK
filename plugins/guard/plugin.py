"""Reference plugin: drop messages outside the allow-list / inside the deny-list.

Config lives next to the runtime data (``data/plugins/guard/config.json``) and is
hot-reloaded when it changes, so operators can tighten a whitelist without a
restart.
"""

from __future__ import annotations

import time
from typing import Any

DEFAULTS: dict[str, Any] = {
    # "blacklist": deny listed ids; "whitelist": deny everything not listed.
    "mode": "blacklist",
    # "group" | "private" | "both"
    "scope": "both",
    "groups": {"whitelist": [], "blacklist": []},
    "users": {"whitelist": [], "blacklist": []},
    # Never blocked, even in whitelist mode (owners, test accounts, ...).
    "always_allow_users": [],
    "log_drops": True,
}


class GuardPlugin:
    """A message filter with the highest sane priority (runs before anything else)."""

    def __init__(self) -> None:
        self.ctx: Any = None
        self.config: dict[str, Any] = dict(DEFAULTS)
        self._config_mtime = 0.0
        self._checked_at = 0.0
        self._empty_whitelist_warned = False

    def on_load(self, ctx: Any) -> None:
        self.ctx = ctx
        self._read_config()
        ctx.log.info(
            "guard ready",
            mode=self.config.get("mode"),
            scope=self.config.get("scope"),
            group_allow=len(self._list("groups", "whitelist")),
            group_deny=len(self._list("groups", "blacklist")),
            user_allow=len(self._list("users", "whitelist")),
            user_deny=len(self._list("users", "blacklist")),
        )

    # ------------------------------------------------------------------ hook

    def on_message(self, message: Any, ctx: Any) -> None:
        self._maybe_reload()
        scope = str(self.config.get("scope", "both"))
        if scope != "both" and scope != message.chat_type:
            return
        if message.sender_uin in self._ids(self.config.get("always_allow_users")):
            return

        mode = str(self.config.get("mode", "blacklist")).lower()
        group_id = message.chat_id if message.is_group else 0

        if mode == "whitelist":
            allowed = (
                (message.is_group and group_id in self._list("groups", "whitelist"))
                or message.sender_uin in self._list("users", "whitelist")
            )
            if not allowed:
                self._drop(ctx, message, "not in whitelist")
            return

        if mode != "blacklist":
            ctx.log.warn("unknown guard mode; treating as blacklist", mode=mode)

        denied = (
            (message.is_group and group_id in self._list("groups", "blacklist"))
            or message.sender_uin in self._list("users", "blacklist")
        )
        if denied:
            self._drop(ctx, message, "in blacklist")

    # --------------------------------------------------------------- helpers

    def _drop(self, ctx: Any, message: Any, reason: str) -> None:
        if self.config.get("log_drops", True):
            ctx.log.debug(
                "blocked message",
                reason=reason,
                chat=message.chat_type,
                chat_id=message.chat_id,
                user=message.sender_uin,
                message_id=message.message_id,
            )
        ctx.drop(reason)

    def _list(self, section: str, key: str) -> set[int]:
        block = self.config.get(section) or {}
        if not isinstance(block, dict):
            return set()
        return self._ids(block.get(key))

    def _ids(self, values: Any) -> set[int]:
        out: set[int] = set()
        for value in values or []:
            try:
                out.add(int(value))
            except (TypeError, ValueError):
                continue
        return out

    def _read_config(self) -> None:
        raw = self.ctx.read_config(DEFAULTS)
        merged = dict(DEFAULTS)
        merged.update(raw)
        for section in ("groups", "users", "always_allow_users"):
            if not isinstance(merged.get(section), (dict, list)):
                merged[section] = DEFAULTS[section]
        self.config = merged
        try:
            self._config_mtime = self.ctx._config_path.stat().st_mtime
        except OSError:
            self._config_mtime = 0.0
        self._checked_at = time.monotonic()
        if (
            str(merged.get("mode", "blacklist")).lower() == "whitelist"
            and not self._list("groups", "whitelist")
            and not self._list("users", "whitelist")
            and not self._empty_whitelist_warned
        ):
            self._empty_whitelist_warned = True
            self.ctx.log.warn("whitelist mode with empty lists will drop every message")

    def _maybe_reload(self) -> None:
        # Cheap check; stats the file at most once per second.
        now = time.monotonic()
        if now - self._checked_at < 1.0:
            return
        self._checked_at = now
        try:
            mtime = self.ctx._config_path.stat().st_mtime
        except OSError:
            return
        if mtime > self._config_mtime:
            self._read_config()
            self.ctx.log.info("guard config reloaded")
