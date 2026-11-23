"""WebUI password authentication: hashed password, 30-minute sessions, and a login history.

## Why this exists alongside `webui.access_token`

The token is a shared secret you paste into scripts; it is fine for an API client and awkward for a person opening a
browser. This adds the thing a browser needs -- a password you type once, a session cookie, and a record of who
logged in from where -- without taking the token away. Either credential opens the door.

## Storage

`data/webui/auth.json`, 0600 where the platform supports it:

    {"password": {"algo": "scrypt", "salt": ..., "hash": ..., "n": ..., "r": ..., "p": ...},
     "logins": [{"at": 1700000000, "ip": "127.0.0.1", "ok": true, "agent": "..."}]}

The password is never stored, only a scrypt digest with its parameters, so a leaked file does not hand over the
password and a future parameter change can be recognised rather than silently mismatching.

## Sessions

Held in memory and lost on restart, which is the right default for a local console: restarting QSICK should not
leave a usable cookie behind. A session lasts 30 minutes and is renewed by use -- a dashboard you are actively
watching should not log you out mid-task, while one you walked away from should expire.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: How long a session stays valid without use. Renewed on every authenticated request.
SESSION_SECONDS = 30 * 60

#: scrypt parameters. `n` is the cost; 2**14 keeps a login under ~100 ms on a laptop while making an offline guess
#: expensive. Stored with each hash so raising them later does not invalidate existing passwords.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1

#: Keep the history bounded: it is a convenience list, not an audit log with retention guarantees.
MAX_LOGINS = 100

COOKIE_NAME = "qsick_session"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str, *, salt: bytes | None = None,
                  n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> dict[str, Any]:
    """Digest a password into the stored form. The plaintext is not kept anywhere."""
    if not password:
        raise ValueError("password may not be empty")
    seed = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=seed, n=n, r=r, p=p, dklen=32)
    return {"algo": "scrypt", "salt": _b64(seed), "hash": _b64(digest), "n": n, "r": r, "p": p}


def verify_password(password: str, record: dict[str, Any] | None) -> bool:
    """Constant-time check of `password` against a stored record."""
    if not record or not isinstance(record, dict):
        return False
    try:
        expected = _unb64(str(record["hash"]))
        salt = _unb64(str(record["salt"]))
        digest = hashlib.scrypt(
            str(password or "").encode("utf-8"), salt=salt,
            n=int(record.get("n", SCRYPT_N)), r=int(record.get("r", SCRYPT_R)),
            p=int(record.get("p", SCRYPT_P)), dklen=len(expected),
        )
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


@dataclass
class LoginRecord:
    at: float
    ip: str
    ok: bool
    agent: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.at)),
            "ip": self.ip,
            "ok": self.ok,
            "agent": self.agent,
        }


@dataclass
class Session:
    token: str
    created: float
    last_seen: float
    ip: str = ""

    def expires_at(self) -> float:
        return self.last_seen + SESSION_SECONDS

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.expires_at()


@dataclass
class AuthStore:
    """Password, sessions and login history for the WebUI."""

    path: Path
    password: dict[str, Any] | None = None
    logins: list[LoginRecord] = field(default_factory=list)
    sessions: dict[str, Session] = field(default_factory=dict)
    #: Set for one render after the password is first created, so the page can say "已设置" instead of asking again.
    _loaded: bool = False

    # ------------------------------------------------------------------ load/save

    def load(self) -> None:
        self._loaded = True
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        password = data.get("password")
        self.password = password if isinstance(password, dict) and password.get("hash") else None
        logins: list[LoginRecord] = []
        for item in data.get("logins") or []:
            if not isinstance(item, dict):
                continue
            try:
                logins.append(LoginRecord(
                    at=float(item.get("at") or 0),
                    ip=str(item.get("ip") or ""),
                    ok=bool(item.get("ok")),
                    agent=str(item.get("agent") or ""),
                ))
            except (TypeError, ValueError):
                continue
        self.logins = logins[-MAX_LOGINS:]

    def save(self) -> None:
        payload: dict[str, Any] = {"password": self.password,
                                   "logins": [item.to_dict() for item in self.logins[-MAX_LOGINS:]]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if os.name != "nt":
                # Windows ACLs are a different mechanism; chmod there is a no-op that raises on some builds.
                self.path.chmod(0o600)
        except OSError:
            # A console that cannot write its auth file still runs; it just will not remember the password.
            pass

    # ------------------------------------------------------------------ password

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    def set_password(self, password: str) -> None:
        self.password = hash_password(password)
        self.save()
        # Every existing session was authorised by the old password, so none of them survive the change.
        self.sessions.clear()

    def check(self, password: str) -> bool:
        return verify_password(password, self.password)

    def clear_password(self) -> None:
        self.password = None
        self.sessions.clear()
        self.save()

    # ------------------------------------------------------------------ sessions

    def start_session(self, ip: str = "") -> Session:
        now = time.time()
        session = Session(token=secrets.token_urlsafe(32), created=now, last_seen=now, ip=ip)
        self.sessions[session.token] = session
        self._drop_expired(now)
        return session

    def touch(self, token: str) -> Session | None:
        """Return the session for `token`, renewing its expiry, or None when it is unknown or stale."""
        session = self.sessions.get(str(token or ""))
        if session is None:
            return None
        now = time.time()
        if session.expired(now):
            self.sessions.pop(session.token, None)
            return None
        session.last_seen = now
        return session

    def end_session(self, token: str) -> None:
        self.sessions.pop(str(token or ""), None)

    def active_sessions(self) -> list[dict[str, Any]]:
        now = time.time()
        self._drop_expired(now)
        return [
            {
                "ip": item.ip,
                "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.created)),
                "expires_in": max(0, int(item.expires_at() - now)),
            }
            for item in sorted(self.sessions.values(), key=lambda value: value.created, reverse=True)
        ]

    def _drop_expired(self, now: float | None = None) -> None:
        moment = now or time.time()
        for token in [key for key, value in self.sessions.items() if value.expired(moment)]:
            self.sessions.pop(token, None)

    # ------------------------------------------------------------------ history

    def note_login(self, ip: str, ok: bool, agent: str = "") -> None:
        self.logins.append(LoginRecord(at=time.time(), ip=ip, ok=ok, agent=agent[:120]))
        if len(self.logins) > MAX_LOGINS:
            del self.logins[: len(self.logins) - MAX_LOGINS]
        self.save()

    def recent_logins(self, limit: int = 20) -> list[dict[str, Any]]:
        return [item.to_dict() for item in reversed(self.logins[-limit:])]
