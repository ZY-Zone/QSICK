"""QZone (QQ 空间) client: pskey + g_tk in, HTTP out.

## Why this lives in Python

The credential is the only part that has to come from inside QQ: `p_skey` for `qzone.qq.com` sits behind
`TipOffService`, so the injected script exposes it as the `qzone.credentials` op. Everything after that is plain
HTTPS, which belongs on this side -- it can be tested without a session, and a new route costs no DLL rebuild.

## The two transports

Both are reachable and neither is optional, because the routes split between them:

    trpc   POST https://h5.qzone.qq.com/http2rpc/gotrpc/v1/{service}/{method}?g_tk=…
           headers: Cookie, x-oidb {"uint32_command":"0x…","uint32_service_type":"0|1"}, x-web-qua
           request and response bodies are JSON

    cgi    GET  https://h5.qzone.qq.com/proxy/domain/{host}/{path}?g_tk=…
           headers: Cookie only; the response is JSONP-wrapped and has to be unwrapped

The `x-oidb` header is not decoration: without it the gateway answers `retcode 4001 not authorized` for every
request, which was once mistaken for "this client is refused". It is the routing key.

## What is measured, and what is guessed

Reachable and verified (2026-11-23, account 3905548034):

    EntranceCtrl/BatchEntranceWhitelist   0x9741/0     retcode 0
    FeedsReader/GetFriendFeeds            0x97e5/0     retcode 0, feeds present
    FeedsReader/GetShuoshuoList           0x9a97/1     retcode 0
    FeedsReader/GetPublicRecomFeeds       0x9a38/0     retcode 0 but total 0 (account not whitelisted)
    InteractionWriter/DeleteComment       0x9989/0     retcode 10000 -> routed and authorized
    old CGI cgi_userinfo_get_all                       code 0 "获取成功", real profile

Everything else -- the exact body fields for detail/comments/likes/publishing -- is inferred from the shape of the
responses, because the server is tolerant: `GetFriendFeeds` answered `retcode 0` for a body of `{}` and for one
carrying plausible fields alike. Where a field is guesswork it is marked as such, so a failure here reads as "wrong
guess" rather than "no permission".

## Feed identifiers are nested

A feed's top level is entirely `cell_*` keys. The identifiers sit one level down, as `feed_id` (hex) and `app_id`.
Looking for `feed.id` finds nothing, and the server answers an empty-id request with `retcode 0` plus empty data --
a silent false success. :func:`normalize_feed` walks for them.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

TRPC_ROOT = "https://h5.qzone.qq.com/http2rpc/gotrpc/v1/"
WEB_ROOT = "https://h5.qzone.qq.com/proxy/domain/"
#: Writes go through a different gateway. Measured: the publish CGI on the `h5` root answers 403, while the same
#: path on `user` answers; the read CGIs are the other way round. So the root is chosen per route, not globally.
USER_ROOT = "https://user.qzone.qq.com/proxy/domain/"
#: Routes that must use `USER_ROOT`.
USER_ROOT_ROUTES = frozenset({"publish", "delete_msg", "set_profile", "set_ban", "set_msg_right"})
#: The picture upload endpoint is not behind /proxy/domain -- it is its own host.
UP_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
#: The client identifier the H5 pages send. The server may grey-release on it, so it is a constant to be updated
#: together with the client version, not a value to invent per request.
WEB_QUA = "V1_HT5_QZ_9.9.32_001_IDC_B"
ORIGIN = "https://h5.qzone.qq.com"
REFERER = "https://h5.qzone.qq.com/pc-qzone/index.html"
REQUEST_TIMEOUT = 20.0
#: A ticket is good for a while; re-asking per request would spam the kernel and the server.
CREDENTIAL_TTL = 300.0

#: route key -> (service, method, command, service_type). Service types come from the route table in
#: `QQ空间-ds/附录B` §B.4.1 and were confirmed by the three reads above.
TRPC_ROUTES: dict[str, tuple[str, str, str, str]] = {
    "capabilities": ("trpc.public_qzone.entrance_ctrl.EntranceCtrl", "BatchEntranceWhitelist", "0x9741", "0"),
    "friend_feeds": ("trpc.qzone.feeds_reader.FeedsReader", "GetFriendFeeds", "0x97e5", "0"),
    "shuoshuo_list": ("trpc.qzone.feeds_reader.FeedsReader", "GetShuoshuoList", "0x9a97", "1"),
    "feed_detail": ("trpc.qzone.feeds_reader.FeedsReader", "GetFeedDetail", "0x97b1", "1"),
    "comments": ("trpc.qzone.feeds_reader.FeedsReader", "GetComment", "0x9bfd", "1"),
    "replies": ("trpc.qzone.feeds_reader.FeedsReader", "GetReplies", "0x9bfe", "1"),
    "publish": ("trpc.qzone.feeds_writer.FeedsWriter", "PublishFeed", "0x9775", "1"),
    "forward": ("trpc.qzone.feeds_writer.FeedsWriter", "ForwardFeed", "0x969a", "1"),
    "like": ("trpc.qzone.interaction_writer.InteractionWriter", "DoLike", "0x9699", "1"),
    "unlike": ("trpc.qzone.interaction_writer.InteractionWriter", "UnLike", "0x969c", "1"),
    "add_comment": ("trpc.qzone.interaction_writer.InteractionWriter", "AddComment", "0x9676", "1"),
    "add_reply": ("trpc.qzone.interaction_writer.InteractionWriter", "AddReply", "0x9677", "1"),
    "delete_comment": ("trpc.qzone.interaction_writer.InteractionWriter", "DeleteComment", "0x9989", "0"),
    "delete_reply": ("trpc.qzone.interaction_writer.InteractionWriter", "DeleteReply", "0x998a", "0"),
    # Public qzone ("空友爱看"). Reachable, but this account answers total 0: not on the whitelist.
    "public_feeds": ("trpc.qzone.feeds_reader.FeedsReader", "GetPublicRecomFeeds", "0x9a38", "0"),
    "public_upvote": ("trpc.public_qzone.interaction.UFOComment", "Upvote", "0x9d18", "1"),
    "public_downvote": ("trpc.public_qzone.interaction.UFOComment", "Downvote", "0x9d19", "1"),
    "public_follow": ("trpc.public_qzone.relation.RelationService", "Follow", "0x9d1a", "1"),
    "public_unfollow": ("trpc.public_qzone.relation.RelationService", "Unfollow", "0x9d1b", "1"),
}

#: Old CGI routes: key -> (host + path, needs g_tk). Bodies are form-encoded on writes and query strings on reads.
CGI_ROUTES: dict[str, str] = {
    "profile": "base.qzone.qq.com/cgi-bin/user/cgi_userinfo_get_all",
    "set_profile": "w.qzone.qq.com/cgi-bin/user/cgi_apply_updateuserinfo_new",
    "own_msg_detail": "taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6",
    "set_msg_right": "taotao.qzone.qq.com/cgi-bin/emotion_cgi_update",
    "set_ban": "w.qzone.qq.com/cgi-bin/right/cgi_black_action_new",
    "msg_list": "taotao.qzone.qq.com/cgi-bin/emotion_cgi_msglist_v6",
    "publish": "taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6",
    "delete_msg": "taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6",
    "upload_image": "up.qzone.qq.com/cgi-bin/upload/cgi_upload_image",
}

_JSONP = re.compile(r"^\s*[A-Za-z_$][\w$]*\s*\((.*)\)\s*;?\s*$", re.S)


class QzoneError(Exception):
    """A QZone call that the server answered with a non-zero retcode."""

    def __init__(self, message: str, *, retcode: int = 0, route: str = "", payload: Any = None) -> None:
        super().__init__(message)
        self.retcode = retcode
        self.route = route
        self.payload = payload


@dataclass(slots=True)
class Credentials:
    uin: str
    pskey: str
    g_tk: int
    available: dict[str, int] = field(default_factory=dict)

    @property
    def cookie(self) -> str:
        return f"uin=o{self.uin}; p_uin=o{self.uin}; p_skey={self.pskey}"


def hash33(text: str) -> int:
    """`g_tk`. Three accumulation variants agree on this value, so the shape is not in doubt."""
    value = 5381
    for character in text:
        value += (value << 5) + ord(character)
        value &= 0xFFFFFFFF
    return value & 0x7FFFFFFF


def unwrap_jsonp(text: str) -> Any:
    """CGI answers arrive wrapped: `_Callback({...})`. Some answer bare JSON."""
    stripped = (text or "").strip()
    match = _JSONP.match(stripped)
    if match:
        stripped = match.group(1)
    return json.loads(stripped)


#: The picture upload answers in QQ's `jsonhtml` format: a whole HTML page whose script calls
#: `frameElement.callback({...})`. Nothing else in the CGI surface does this, so it gets its own unwrapper rather
#: than making `unwrap_jsonp` guess between three shapes.
_UPLOAD_CALLBACK = re.compile(r"frameElement\.callback\s*\(\s*(\{.*?\})\s*\)\s*;", re.S)


def unwrap_upload_response(text: str) -> Any:
    body = text or ""
    match = _UPLOAD_CALLBACK.search(body)
    if match:
        return json.loads(match.group(1))
    # Fall back to the plain shapes, so a future change of wrapper does not look like a failed upload.
    return unwrap_jsonp(body)


def _walk_id(feed: dict[str, Any]) -> tuple[str, str]:
    """The (feed_id, app_id) of a feed, which are nested rather than top level.

    Measured: the top level of a `feed` object is 43 `cell_*` keys, and the identifiers live one level down. Sending
    an empty id is not an error to the server -- it answers retcode 0 with empty data -- so this has to be right or
    the failure hides.
    """
    # Measured: the identifiers sit two levels down (`cell_common.union_id.feed_id`), so a one-level walk finds
    # nothing. Breadth-limited recursion rather than a fixed path, because the wrapper name has changed before.
    candidates: list[tuple[dict[str, Any], str]] = [(feed, "")]
    for key, value in feed.items():
        if isinstance(value, dict):
            candidates.append((value, f"{key}."))
            for inner_key, inner in value.items():
                if isinstance(inner, dict):
                    candidates.append((inner, f"{key}.{inner_key}."))
    feed_id = ""
    app_id = ""
    for mapping, prefix in candidates:
        for name in ("feed_id", "feedid", "tid", "id"):
            value = mapping.get(name)
            if isinstance(value, str) and value and not feed_id:
                feed_id = value
        for name in ("app_id", "appid"):
            value = mapping.get(name)
            if value not in (None, "") and not app_id:
                app_id = str(value)
    return feed_id, app_id


def feed_ref_of(feed: Any) -> dict[str, Any]:
    """The action handle a feed carries, which writes and detail both need.

    Measured on a live response: `cell_common.union_id` is the identity the trpc routes accept, and a plain `tid`
    is **not** enough -- `GetFeedDetail` answers `retcode 10023 无权限访问` for one. `like_key` and
    `cell_feed_transfer` are optional companions that the like route requires.
    """
    if not isinstance(feed, dict):
        return {}
    common = feed.get("cell_common") if isinstance(feed.get("cell_common"), dict) else {}
    union = common.get("union_id")
    if not isinstance(union, dict):
        return {}
    ref: dict[str, Any] = {"union_id": union}
    transfer = feed.get("cell_feed_transfer")
    if transfer not in (None, ""):
        ref["cell_feed_transfer"] = transfer
    like_key = common.get("cur_like_key")
    if isinstance(like_key, str) and like_key:
        ref["like_key"] = like_key
    domain = common.get("domain_type")
    ref["feed_action_domain_type"] = domain if isinstance(domain, int) and 1 <= domain <= 3 else 1
    return ref


def normalize_feed(raw: Any) -> dict[str, Any]:
    """Flatten one feed into something a caller can use, keeping the raw object for anything missed.

    The field names here were read off a live response rather than guessed, because the obvious guesses are all
    wrong: the author is `cell_user_info.user.uin` / `.nick` (not `cell_user_info.uin`), the text is
    `cell_summary.summary[].content`, the timestamp is `cell_common.time` (there is no `abstime`), and a like count
    is `cell_like.num` (not `like_num`).

    Nothing here may raise: a feed whose shape changed should come back with empty fields and its raw payload intact
    rather than take the whole listing down.
    """
    if not isinstance(raw, dict):
        return {"raw": raw}
    feed = raw.get("feed") if isinstance(raw.get("feed"), dict) else raw
    feed_id, app_id = _walk_id(feed)
    common = feed.get("cell_common") if isinstance(feed.get("cell_common"), dict) else {}
    user = (feed.get("cell_user_info") or {}).get("user") if isinstance(feed.get("cell_user_info"), dict) else {}
    if not isinstance(user, dict):
        user = {}
    like = feed.get("cell_like") if isinstance(feed.get("cell_like"), dict) else {}
    comment = feed.get("cell_comment") if isinstance(feed.get("cell_comment"), dict) else {}

    # The body is a list of fragments; joining them is what the post reads as.
    summary = (feed.get("cell_summary") or {}).get("summary") if isinstance(feed.get("cell_summary"), dict) else None
    fragments = summary if isinstance(summary, list) else []
    content = "".join(str(part.get("content") or "") for part in fragments if isinstance(part, dict)).strip()

    images: list[dict[str, Any]] = []
    media_sources: list[Any] = []
    media = feed.get("cell_media") if isinstance(feed.get("cell_media"), dict) else {}
    media_sources.extend(media.get("media_items") or [])
    for part in fragments:
        if isinstance(part, dict):
            media_sources.extend(part.get("media_items") or [])
    for item in media_sources:
        if not isinstance(item, dict):
            continue
        # A media entry carries *either* a picture or a clip, and the absent one is an explicit null rather than a
        # missing key -- so chaining `.get(...)` through it raises. Found by running this against a real feed.
        picture = item.get("image") if isinstance(item.get("image"), dict) else {}
        clip = item.get("video") if isinstance(item.get("video"), dict) else {}
        # Measured shape: the address is nested one more level, under `default_url`, together with its size.
        # `image.url` does not exist, which is why an attached picture first looked like no picture at all.
        best = picture.get("default_url") if isinstance(picture.get("default_url"), dict) else {}
        if not best:
            best = clip.get("default_url") if isinstance(clip.get("default_url"), dict) else {}
        url = best.get("url") or picture.get("url") or clip.get("url") or ""
        if not url:
            continue
        images.append({
            "url": url,
            "width": best.get("width") or picture.get("width") or clip.get("width") or 0,
            "height": best.get("height") or picture.get("height") or clip.get("height") or 0,
            "raw": item,
        })

    return {
        "feed_id": feed_id,
        "feed_ref": feed_ref_of(feed),
        "app_id": app_id,
        "uin": str(user.get("uin") or ""),
        "nickname": user.get("nick") or user.get("nickname") or "",
        "content": content,
        "create_time": _as_int(common.get("time")),
        "like_count": _as_int(like.get("num")),
        "comment_count": _as_int(comment.get("num")),
        "liked": bool(like.get("liked")),
        "images": images,
        "raw": raw,
    }


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class QzoneClient:
    """Calls QZone on behalf of one account.

    `caller` is the action layer's transport bridge: ``await caller("qzone.credentials", {"account": …})``. Keeping
    it as a callable rather than importing the action module lets this be tested without a session.
    """

    def __init__(self, caller, *, account: str = "", timeout: float = REQUEST_TIMEOUT) -> None:
        self._caller = caller
        self._account = str(account or "")
        self._timeout = timeout
        self._credentials: Credentials | None = None
        self._credentials_at = 0.0

    # ---------------------------------------------------------------- credentials
    async def credentials(self, *, refresh: bool = False) -> Credentials:
        if (not refresh and self._credentials is not None
                and time.monotonic() - self._credentials_at < CREDENTIAL_TTL):
            return self._credentials
        payload = await self._caller("qzone.credentials", {"account": self._account})
        if not isinstance(payload, dict) or not payload.get("pskey"):
            raise QzoneError("the session returned no p_skey for qzone.qq.com")
        self._credentials = Credentials(
            uin=str(payload.get("uin") or self._account),
            pskey=str(payload["pskey"]),
            g_tk=int(payload.get("g_tk") or hash33(str(payload["pskey"]))),
            available=dict(payload.get("available") or {}),
        )
        self._credentials_at = time.monotonic()
        return self._credentials

    # ---------------------------------------------------------------- transports
    def _headers(self, credentials: Credentials, *, oidb: tuple[str, str] | None) -> dict[str, str]:
        headers = {
            "Cookie": credentials.cookie,
            "Origin": ORIGIN,
            "Referer": REFERER,
            # A browser-like agent: the gateway is the one the H5 page talks to.
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        }
        if oidb is not None:
            headers["x-oidb"] = json.dumps({"uint32_command": oidb[0], "uint32_service_type": oidb[1]})
            headers["x-web-qua"] = WEB_QUA
        return headers

    async def _request(self, method: str, url: str, *, headers: dict[str, str],
                       body: bytes | None = None) -> tuple[int, str]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            if body is None:
                response = await client.request(method, url, headers=headers)
            else:
                response = await client.request(method, url, headers=headers, content=body)
            return response.status_code, response.text

    async def trpc(self, route: str, body: dict[str, Any] | None = None, *,
                   refresh: bool = False) -> dict[str, Any]:
        """POST one trpc route and return its `data`.

        A non-zero `retcode` raises with the server's own message: those numbers are the only thing that separates
        "wrong body" from "no permission", and flattening them loses that.
        """
        service, method, command, service_type = TRPC_ROUTES[route]
        credentials = await self.credentials(refresh=refresh)
        url = f"{TRPC_ROOT}{service}/{method}?g_tk={credentials.g_tk}"
        payload = json.dumps(body if body is not None else {})
        status, text = await self._request(
            "POST", url,
            headers={**self._headers(credentials, oidb=(command, service_type)),
                     "Content-Type": "application/json"},
            body=payload.encode("utf-8"),
        )
        if status != 200:
            raise QzoneError(f"{route}: HTTP {status}: {text[:160]}", route=route)
        try:
            answer = json.loads(text)
        except ValueError:
            raise QzoneError(f"{route}: response is not JSON: {text[:160]}", route=route) from None
        if not isinstance(answer, dict):
            raise QzoneError(f"{route}: response is not an object", route=route, payload=answer)
        retcode = _as_int(answer.get("retcode"))
        if retcode != 0:
            message = answer.get("message") or answer.get("msg") or ""
            raise QzoneError(f"{route}: retcode {retcode} {message}".strip(), route=route,
                             retcode=retcode, payload=answer)
        data = answer.get("data")
        return data if isinstance(data, dict) else answer

    async def cgi(self, route: str, params: dict[str, Any], *, post: bool = False,
                  full: bool = False) -> Any:
        """One old-CGI call. The answer is JSONP-wrapped, so it is unwrapped before parsing."""
        target = CGI_ROUTES[route]
        credentials = await self.credentials()
        root = USER_ROOT if route in USER_ROOT_ROUTES else WEB_ROOT
        url = f"{root}{target}"
        query = {"g_tk": credentials.g_tk, "uin": credentials.uin, **params}
        headers = self._headers(credentials, oidb=None)
        if post:
            import urllib.parse

            body = urllib.parse.urlencode(query).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            status, text = await self._request("POST", url, headers=headers, body=body)
        else:
            import urllib.parse

            status, text = await self._request("GET", f"{url}?{urllib.parse.urlencode(query)}", headers=headers)
        if status != 200:
            raise QzoneError(f"{route}: HTTP {status}: {text[:160]}", route=route)
        try:
            answer = unwrap_jsonp(text)
        except ValueError:
            raise QzoneError(f"{route}: response is not JSON: {text[:160]}", route=route) from None
        code = _as_int(answer.get("code")) if isinstance(answer, dict) else 0
        if code != 0:
            message = answer.get("message") or answer.get("subcode") or ""
            raise QzoneError(f"{route}: code {code} {message}".strip(), route=route,
                             retcode=code, payload=answer)
        if full:
            return answer
        return answer.get("data") if isinstance(answer, dict) else answer

    # ---------------------------------------------------------------- reads
    async def capabilities(self) -> dict[str, Any]:
        credentials = await self.credentials()
        data = await self.trpc("capabilities", {"uins": [credentials.uin], "entrance_types": [1, 15]})
        results = data.get("results") if isinstance(data, dict) else None
        return (results[0] if results else {}) if isinstance(results, list) else data

    async def friend_feeds(self, *, count: int = 10, pos: int = 0) -> dict[str, Any]:
        """Friend feed page. The identifiers are harvested so a caller can act on a feed."""
        data = await self.trpc("friend_feeds", {})
        return _feed_page(data, count=count)

    async def shuoshuo_list(self, *, uin: str = "", count: int = 10, pos: int = 0) -> dict[str, Any]:
        credentials = await self.credentials()
        host = uin or credentials.uin
        # `hostUin`/`uin` both appear in responses; sending both is harmless and covered the one measured shape.
        data = await self.trpc("shuoshuo_list", {"hostUin": host, "uin": host, "pos": pos, "num": count})
        return _feed_page(data, count=count)

    async def feed_detail(self, ref: dict[str, Any]) -> dict[str, Any]:
        """One feed, by its action handle (`feed_ref`), not by tid."""
        data = await self.trpc("feed_detail", {
            "union_id": ref.get("union_id"),
            "cell_feed_transfer": ref.get("cell_feed_transfer"),
            "feed_action_domain_type": ref.get("feed_action_domain_type", 1),
            "from_page": 1,
        })
        inner = data.get("feed") if isinstance(data.get("feed"), dict) else {}
        feed = inner.get("feed") if isinstance(inner.get("feed"), dict) else inner
        return {"feed": normalize_feed({"feed": feed}), "raw": data}

    async def comments(self, ref: dict[str, Any], *, attach_info: str = "") -> dict[str, Any]:
        """Comments under a feed. `attach_info` continues a page."""
        body: dict[str, Any] = {
            "union_id": ref.get("union_id"),
            "cell_feed_transfer": ref.get("cell_feed_transfer"),
        }
        if attach_info:
            body["attach_info"] = attach_info
        data = await self.trpc("comments", body)
        cell = data.get("cell_comment") if isinstance(data.get("cell_comment"), dict) else {}
        items = cell.get("comments") if isinstance(cell.get("comments"), list) else []
        return {"comments": [_normalize_comment(item) for item in items],
                "count": _as_int(cell.get("num")),
                "has_more": bool(data.get("has_more")),
                "attach_info": str(data.get("attach_info") or ""), "raw": data}

    async def replies(self, ref: dict[str, Any], comment_id: str, **_: Any) -> dict[str, Any]:
        data = await self.trpc("replies", {
            "union_id": ref.get("union_id"),
            "cell_feed_transfer": ref.get("cell_feed_transfer"),
            "comment_id": comment_id,
        })
        cell = data.get("cell_comment") if isinstance(data.get("cell_comment"), dict) else {}
        items = cell.get("replies") if isinstance(cell.get("replies"), list) else []
        return {"replies": [_normalize_comment(item) for item in items],
                "has_more": bool(data.get("has_more")), "raw": data}

    async def like(self, ref: dict[str, Any], *, remove: bool = False) -> dict[str, Any]:
        """Like or unlike. The like route insists on `like_key`, which only a fresh read of the feed carries."""
        like_key = ref.get("like_key")
        if not isinstance(like_key, str) or not like_key:
            raise QzoneError("this feed carries no like_key; read the feed again before liking it")
        data = await self.trpc("unlike" if remove else "like", {
            "like_key": like_key, "union_id": ref.get("union_id"), "like_type": 1,
            "cell_feed_transfer": ref.get("cell_feed_transfer"),
        })
        return {"liked": not remove, "raw": data}

    async def add_comment(self, ref: dict[str, Any], content: str, *,
                          parent: dict[str, Any] | None = None) -> dict[str, Any]:
        """Comment, or reply when `parent` (a comment object) is given."""
        credentials = await self.credentials()
        me = {"uin": credentials.uin, "uid": "", "nick": ""}
        segments = [{"content": content, "media_items": [], "name": "", "type": 0, "uid": "", "uin": "",
                     "url": "", "who": 0}]
        body: dict[str, Any] = {
            "union_id": ref.get("union_id"),
            "cell_feed_transfer": ref.get("cell_feed_transfer"),
        }
        if parent is None:
            body["comment"] = {"user": me, "content": segments}
        else:
            target = parent.get("user") if isinstance(parent.get("user"), dict) else me
            body["comment"] = parent
            body["reply"] = {"content": segments, "target_user": target, "user": me}
        data = await self.trpc("add_reply" if parent is not None else "add_comment", body)
        result = data.get("reply" if parent is not None else "comment")
        if not isinstance(result, dict):
            raise QzoneError("the server accepted the comment but returned no object for it", route="add_comment")
        return {"comment_id": str(result.get("id") or ""), "comment": result, "raw": data}

    async def profile(self, uin: str = "") -> dict[str, Any]:
        """Space profile. `uin` reads someone else's; empty reads this account's.

        The parameter is `uin`, not `vuin` -- measured: `vuin` is ignored and the answer describes the logged-in
        account regardless, which looked like "reading another user's space works" until the reply was compared
        against the requested id.
        """
        credentials = await self.credentials()
        data = await self.cgi("profile", {"uin": uin or credentials.uin, "fupdate": 1})
        return data if isinstance(data, dict) else {"raw": data}

    async def shenfen(self) -> dict[str, Any]:
        """The account's own identifier pair, which some writes need."""
        credentials = await self.credentials()
        return {"uin": credentials.uin}

    # ---------------------------------------------------------------- writes
    async def publish(self, content: str, *, images: list[str] | None = None, ugc_right: int = 1,
                      target_uins: list[str] | None = None) -> dict[str, Any]:
        """Publish a shuoshuo. `images` are richval strings from :meth:`upload_image`.

        The CGI route is used rather than `PublishFeed` over trpc because it takes the media in the same call and
        answers with the new `tid`, which a caller then needs for likes, comments and deletion.
        """
        text = str(content or "").strip()
        if not text:
            raise QzoneError("a shuoshuo needs content")
        credentials = await self.credentials()
        fields: dict[str, Any] = {
            "syn_tweet_verson": "1", "paramstr": "1", "con": text, "feedversion": "1", "ver": "1",
            "ugc_right": str(ugc_right), "to_sign": "0", "hostuin": credentials.uin,
            "code_version": "1", "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{credentials.uin}/main",
        }
        if images:
            fields["richtype"] = "1"
            fields["richval"] = "\t".join(images)
        if target_uins:
            fields["allow_uins"] = "|".join(target_uins)
            fields["who"] = "1"
        # The publish CGI puts `tid` at the top level and leaves `data` null, so the whole answer is needed.
        answer = await self.cgi("publish", fields, post=True, full=True)
        data = answer if isinstance(answer, dict) else {}
        tid = str(data.get("tid") or data.get("t1_tid") or "")
        return {"tid": tid, "content": text, "ugc_right": ugc_right,
                "images": len(images or []), "raw": data}

    async def upload_image(self, path: str) -> str:
        """Upload one picture and return its `richval`.

        `richval` is what the publish call embeds: `,albumid,lloc,lloc,type,height,width,,height,width`.
        """
        import base64
        import pathlib

        file = pathlib.Path(path)
        if not file.is_file():
            raise QzoneError(f"no such image: {path}")
        payload = base64.b64encode(file.read_bytes()).decode("ascii")
        credentials = await self.credentials()
        back = (f"http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                f"http://119.147.64.75/cgi-bin/upload/cgi_upload_image&url="
                f"https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={credentials.g_tk}")
        fields = {
            "filename": "filename", "uin": credentials.uin,
            # `skey` is part of the shape NapCat sends, but this session exposes no skey getter -- only p_skey. It is
            # sent empty rather than omitted so the field count matches what the endpoint expects.
            "skey": "", "zzpaneluin": credentials.uin,
            "p_uin": credentials.uin, "p_skey": credentials.pskey,
            "uploadtype": "1", "albumtype": "7", "exttype": "0", "refer": "shuoshuo",
            "output_type": "jsonhtml", "charset": "utf-8", "output_charset": "utf-8",
            "upload_hd": "1", "hd_width": "2048", "hd_height": "10000", "hd_quality": "96",
            "backUrls": back, "base64": "1", "jsonhtml_callback": "callback", "picfile": payload,
            "qzreferrer": f"https://user.qzone.qq.com/{credentials.uin}/main",
        }
        import urllib.parse

        body = urllib.parse.urlencode(fields).encode("utf-8")
        status, text = await self._request(
            "POST", f"{UP_URL}?g_tk={credentials.g_tk}",
            headers={**self._headers(credentials, oidb=None),
                     "Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
        if status != 200:
            raise QzoneError(f"upload_image: HTTP {status}: {text[:160]}", route="upload_image")
        try:
            answer = unwrap_upload_response(text)
        except ValueError:
            raise QzoneError(f"upload_image: response is not JSON: {text[:160]}",
                             route="upload_image") from None
        code = _as_int(answer.get("code")) if isinstance(answer, dict) else 0
        if code != 0:
            raise QzoneError(f"upload_image: code {code} {answer.get('msg') or answer.get('message') or ''}".strip(),
                             route="upload_image", retcode=code, payload=answer)
        data = answer.get("data") if isinstance(answer, dict) else None
        if not isinstance(data, dict):
            raise QzoneError("upload_image: response carries no data", route="upload_image", payload=answer)
        return richval_of(data)

    async def delete_msg(self, tid: str, *, uin: str = "") -> dict[str, Any]:
        credentials = await self.credentials()
        answer = await self.cgi("delete_msg",
                                {"tid": tid, "hostuin": uin or credentials.uin, "format": "json"}, post=True)
        return {"tid": tid, "deleted": True, "raw": answer}

    async def set_profile(self, *, name: str = "", description: str = "") -> dict[str, Any]:
        fields: dict[str, Any] = {"format": "json"}
        if name:
            fields["spacename"] = name
        if description:
            fields["signature"] = description
        if len(fields) == 1:
            raise QzoneError("set_profile needs a name or a description")
        answer = await self.cgi("set_profile", fields, post=True)
        return {"name": name, "description": description, "raw": answer}

    async def set_msg_right(self, tid: str, ugc_right: int, *, content: str = "",
                            target_uins: list[str] | None = None, uin: str = "") -> dict[str, Any]:
        """Change who can see one post.

        `con` is required -- measured, the CGI answers `code -10005 您未输入内容，随便写点什么吧` without it, because
        this endpoint rewrites the whole post rather than patching a field. So the current text has to be sent back
        unchanged, which is also why NapCat reads the post first.
        """
        if not str(content or "").strip():
            raise QzoneError("set_msg_right needs the post's current text (`con`); read the feed first and pass it")
        credentials = await self.credentials()
        fields: dict[str, Any] = {
            "tid": tid, "con": content, "ugc_right": str(ugc_right),
            "hostuin": uin or credentials.uin, "format": "json",
        }
        if target_uins:
            fields["allow_uins"] = "|".join(target_uins)
        answer = await self.cgi("set_msg_right", fields, post=True)
        return {"tid": tid, "ugc_right": ugc_right, "raw": answer}

    async def set_ban(self, target_uin: str, *, banned: bool = True) -> dict[str, Any]:
        fields = {"fuid": target_uin, "action": "1" if banned else "0", "format": "json"}
        answer = await self.cgi("set_ban", fields, post=True)
        return {"uin": target_uin, "banned": banned, "raw": answer}

    async def public_call(self, route: str, *, ref: dict[str, Any] | None = None,
                          extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """One public-qzone route ("空友爱看").

        These share the trpc gateway and differ only in route and body. Measured on this account: every
        `public_qzone_*` entrance flag is false and `GetPublicRecomFeeds` answers `total 0`, so the calls are
        reachable but there is nothing to act on -- the answers say so rather than pretending to work.
        """
        body: dict[str, Any] = {}
        if ref:
            body["union_id"] = ref.get("union_id")
            if ref.get("cell_feed_transfer") is not None:
                body["cell_feed_transfer"] = ref.get("cell_feed_transfer")
            if ref.get("like_key"):
                body["like_key"] = ref.get("like_key")
        if extra:
            body.update(extra)
        return await self.trpc(route, body)

    # ---------------------------------------------------------------- public qzone
    async def public_feeds(self, *, count: int = 10) -> dict[str, Any]:
        data = await self.trpc("public_feeds", {})
        page = _feed_page(data, count=count)
        page["available"] = bool(page["feeds"]) or page["total"] > 0
        return page


def richval_of(data: dict[str, Any]) -> str:
    """`,albumid,lloc,lloc,type,height,width,,height,width` -- the shape the publish call embeds."""
    album = data.get("albumid") or ""
    lloc = data.get("lloc") or ""
    picture_type = data.get("type") or ""
    height = data.get("height") or ""
    width = data.get("width") or ""
    return f",{album},{lloc},{lloc},{picture_type},{height},{width},,{height},{width}"


def _feed_page(data: Any, *, count: int) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"feeds": [], "raw": data}
    feeds = data.get("feeds") if isinstance(data.get("feeds"), list) else []
    normalized = [normalize_feed(item) for item in feeds]
    return {
        "feeds": normalized[:count] if count else normalized,
        "total": _as_int(data.get("total")),
        "has_more": bool(data.get("has_more")),
        "attach_info": data.get("attach_info") or "",
        "raw": data,
    }


def _normalize_comment(item: Any) -> dict[str, Any]:
    """One comment, flattened. Field names read off a live response: `id`, `user.nick`, and a content *list*."""
    if not isinstance(item, dict):
        return {"raw": item}
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    raw_content = item.get("content")
    if isinstance(raw_content, list):
        # The body arrives as rich segments; joining them is what the comment reads as.
        content = "".join(str(part.get("content") or "") for part in raw_content if isinstance(part, dict))
    else:
        content = str(raw_content or "")
    return {
        "comment_id": str(item.get("id") or item.get("commentid") or ""),
        "uin": str(user.get("uin") or item.get("uin") or ""),
        "nickname": user.get("nick") or user.get("nickname") or "",
        "content": content.strip(),
        "create_time": _as_int(item.get("time") or item.get("create_time")),
        "reply_count": _as_int(item.get("reply_num")),
        "user": user,
        "raw": item,
    }
