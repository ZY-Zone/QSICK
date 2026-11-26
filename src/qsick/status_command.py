"""The `#QS` status command: one fixed line block, sent back to whoever asked.

Deliberately small and side-effect free. The responder lives in `Runtime` because it needs to send a
message, but everything that decides *whether* and *what* lives here, so it can be tested without a
session.

The reply is a plain string rather than a message segment list: the only thing it carries is text,
and building it as segments here would put the wire shape in two places.
"""

from __future__ import annotations

#: What an operator types to ask. Matched after stripping surrounding whitespace, and case-insensitive
#: on the letters: `#qs` and `#QS` are the same request, while `#QS 你好` is not a request at all.
COMMAND = "#QS"

#: Where the reply points people who want to know more.
SITE = "http://www.sickcc.online/QSICK"


def is_status_command(text: str) -> bool:
    """True when the whole message is the command.

    An exact match, not a prefix: a message that happens to start with `#QS` is ordinary
    conversation, and a bot that answers every one of those is a bot nobody can talk to.
    """
    return str(text or "").strip().upper() == COMMAND.upper()


def is_status_command_segments(segments: object) -> bool:
    """True when a OneBot message consists of nothing but the command.

    A message of `@bot #QS` is *not* the command: its content is the mention plus the text, and the
    operator who wrote `#QS` as the whole content of a message meant something different from the
    one who addressed the bot. So any segment that is not text disqualifies the message, rather than
    being skipped over as noise -- skipping is what turns "addressed to me" into "asked of me".
    """
    if isinstance(segments, str):
        return is_status_command(segments)
    if not isinstance(segments, list) or not segments:
        return False
    parts: list[str] = []
    for item in segments:
        if not isinstance(item, dict):
            return False
        if str(item.get("type") or "") != "text":
            return False
        data = item.get("data")
        parts.append(str((data or {}).get("text") or "") if isinstance(data, dict) else "")
    return is_status_command("".join(parts))


def build_reply(*, version: str, uptime: str, account: str) -> str:
    """The five lines the operator gets back.

    `uptime` is passed in rather than computed here so it is the *same* number the overview page
    shows -- that page formats it through `runtime.info()`, and a second formatter would eventually
    disagree with the first.
    """
    return "\n".join((
        "QSICK状态",
        f"QSICK版本：{version}",
        f"运行时间：{uptime}",
        f"当前账号：{account}",
        f"欢迎访问{SITE}了解更多",
    ))
