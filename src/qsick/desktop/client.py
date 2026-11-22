"""Minimal desktop chat client for QSICK (stdlib only).

It is a GUI *front-end* over QSICK's OneBot/WebUI APIs, not a QQ protocol client.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class DesktopApiError(RuntimeError):
    pass


class DesktopApi:
    def __init__(
        self,
        onebot_base: str = "http://127.0.0.1:3000",
        webui_base: str = "http://127.0.0.1:5754",
        token: str = "",
        webui_token: str = "",
        timeout: float = 8.0,
    ) -> None:
        self.onebot_base = onebot_base.rstrip("/")
        self.webui_base = webui_base.rstrip("/")
        self.token = token
        self.webui_token = webui_token
        self.timeout = timeout

    def _request(self, url: str, payload: dict | None = None, *, headers: dict[str, str] | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise DesktopApiError(str(exc)) from exc
        if not isinstance(body, dict):
            raise DesktopApiError("unexpected response")
        return body

    def _onebot(self, action: str, payload: dict | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        body = self._request(f"{self.onebot_base}/{action}", payload or {}, headers=headers)
        if body.get("status") == "failed":
            raise DesktopApiError(str(body.get("message") or body.get("wording") or "action failed"))
        return body.get("data")

    def _webui(self, path: str) -> Any:
        headers = {"x-access-token": self.webui_token} if self.webui_token else {}
        return self._request(f"{self.webui_base}{path}", headers=headers)

    def login_info(self) -> dict:
        return dict(self._onebot("get_login_info") or {})

    def group_list(self) -> list[dict]:
        return list(self._onebot("get_group_list") or [])

    def friend_list(self) -> list[dict]:
        return list(self._onebot("get_friend_list") or [])

    def messages(self, page: int = 1) -> list[dict]:
        body = self._webui(f"/api/messages?page={max(1, page)}")
        return list(body.get("messages") or [])

    def send_group(self, group_id: int, text: str) -> dict:
        return dict(
            self._onebot(
                "send_group_msg",
                {"group_id": int(group_id), "message": [{"type": "text", "data": {"text": text}}]},
            )
            or {}
        )
