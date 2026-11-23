"""Shared plumbing for the QZone actions.

Every action here is a thin adapter: resolve the feed identity the caller gave, hand it to
:class:`qsick.protocol.qzone.QzoneClient`, and map the client's failures onto OneBot retcodes.

Three shapes are accepted for identifying a feed, because NapCat accepts them and callers already pass them:

  * ``tid``            the feed id itself (hex string)
  * ``feed_ref``       an object returned by a previous listing (``{tid, uin, appid}``)
  * ``target_uin``     whose feed it is, for reads that work per user

They are not interchangeable -- a like needs the id, not the owner -- so :func:`feed_target` returns all of it and
each caller states what it actually requires.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ... import models
from ...registry import ActionError
from ....protocol.qzone import QzoneClient, QzoneError
from ..base import Action, call


@dataclass(slots=True)
class FeedTarget:
    tid: str = ""
    uin: str = ""
    app_id: str = ""


def client(ctx: Any) -> QzoneClient:
    """A client bound to the account this request named."""

    async def caller(op: str, params: dict[str, Any]) -> dict[str, Any]:
        return await call(ctx, op, params, op)

    return QzoneClient(caller, account=str(getattr(ctx, "account", "") or ""))


def feed_target(params: dict[str, Any]) -> FeedTarget:
    """Resolve whichever identity the caller supplied."""
    tid = str(params.get("tid") or "").strip()
    uin = str(params.get("target_uin") or "").strip()
    app_id = str(params.get("appid") or params.get("app_id") or "").strip()
    ref = params.get("feed_ref")
    if isinstance(ref, dict):
        tid = tid or str(ref.get("tid") or ref.get("feed_id") or "").strip()
        uin = uin or str(ref.get("uin") or ref.get("host_uin") or "").strip()
        app_id = app_id or str(ref.get("appid") or ref.get("app_id") or "").strip()
    return FeedTarget(tid=tid, uin=uin, app_id=app_id)


class QzoneAction(Action):
    """Runs one client call and maps its errors.

    The mapping matters: a server `retcode` is the only thing that separates "the body was wrong" from "this account
    may not do that", and collapsing both into one number would make the second look like the first.
    """

    async def run(self, ctx: Any, **params: Any) -> Any:
        try:
            return await self.handle(ctx, **params)
        except QzoneError as exc:
            raise ActionError(f"qzone: {exc}", models.UPSTREAM) from None

    async def handle(self, ctx: Any, **params: Any) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError


async def _recent_feeds(qzone: QzoneClient) -> list[dict[str, Any]]:
    """Both listings we can read, flattened.

    A caller that only has a tid has to be looked up, and the post may be in either list -- the friend feed
    (`feeds`) or this account's own posts (`msglist`). Reading both is what makes a `tid`-only call work at all.
    """
    found: list[dict[str, Any]] = []
    for read in (lambda: qzone.friend_feeds(count=20), lambda: qzone.shuoshuo_list(count=20)):
        try:
            page = await read()
        except QzoneError:
            continue
        for key in ("feeds", "msglist"):
            found.extend(entry for entry in (page.get(key) or []) if isinstance(entry, dict))
    return found


async def find_feed(ctx: Any, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The `feed_ref` plus the normalised feed itself.

    Writes need more than an identity -- changing a post's visibility rewrites it, so the current text has to come
    along -- hence the pair rather than just the handle.
    """
    ref = params.get("feed_ref")
    if isinstance(ref, dict) and isinstance(ref.get("union_id"), dict):
        feed = params.get("feed") if isinstance(params.get("feed"), dict) else {}
        if not feed:
            # A handle alone says who the post is, not what it says; ask for the body so a rewriting call has it.
            # `feed_detail` answers with the item itself (`{tid, content, ..., feed}`), so the text is on that, not
            # inside the nested `feed` -- reading the wrong one was why `set_qzone_msg_right` insisted it had none.
            try:
                feed = await client(ctx).feed_detail(ref)
            except QzoneError:
                feed = {}
        return ref, feed
    tid = str(params.get("tid") or "").strip()
    if not tid:
        raise ActionError("qzone: pass `feed_ref` (from a listing) or a `tid` the listings still have",
                          models.BAD_REQUEST)
    for feed in await _recent_feeds(client(ctx)):
        if feed.get("tid") == tid and isinstance(feed.get("feed_ref"), dict) and feed["feed_ref"]:
            return feed["feed_ref"], feed
    raise ActionError(
        f"qzone: no handle for tid {tid} in the recent listings; read the feed again and pass its `feed_ref`",
        models.NOT_FOUND)


async def resolve_ref(ctx: Any, params: dict[str, Any]) -> dict[str, Any]:
    """A usable `feed_ref` for this request.

    Writes and detail take the handle a feed carries (`union_id` and friends), not the feed id: measured,
    `GetFeedDetail` answers `retcode 10023 无权限访问` when handed a bare tid. A caller that only has a tid is
    therefore looked up in the listings we can read, and told to re-read when it is not there.
    """
    ref = params.get("feed_ref")
    if isinstance(ref, dict) and isinstance(ref.get("union_id"), dict):
        return ref
    if isinstance(params.get("union_id"), dict):
        return params
    tid = str(params.get("tid") or "").strip()
    if not tid:
        raise ActionError("qzone: pass `feed_ref` (from a listing) or a `tid` that a listing still has",
                          models.BAD_REQUEST)
    for feed in await _recent_feeds(client(ctx)):
        if feed.get("tid") == tid and isinstance(feed.get("feed_ref"), dict) and feed["feed_ref"]:
            return feed["feed_ref"]
    raise ActionError(
        f"qzone: no handle for tid {tid} in the recent listings; read the feed again and pass its `feed_ref`",
        models.NOT_FOUND)


def require_tid(target: FeedTarget) -> FeedTarget:
    if not target.tid:
        raise ActionError(
            "qzone: this needs the feed itself; pass `tid` (or a `feed_ref` from a listing), not only `target_uin`",
            models.BAD_REQUEST)
    return target
