"""What `login.info` may and may not be used to conclude.

This file records two mistakes that pulled in opposite directions, because the second was a reaction to the
first and both are easy to reintroduce.

1. `login.info` reported an account as soon as the session *knew* one -- with stored credentials that is about
   two seconds before its kernel message listener attaches. The network config follows this call, so it opened
   while the session received nothing.

2. Requiring the injected side's own `ready` flag to gate it made things worse, not better. `ready` was derived
   from `login-boot.json`'s `stage`, and that stage is rewritten every ~20 s by the QR refresh and again by
   every listener retry. A session that had been serving for minutes reported "not confirmed" in between --
   the UI alternated between 已登录 and 未登录, and only with more than one session, because the extra load
   made the rewrites overlap.

The rule that survived: the uin is the confirmation. It exists only after QQ has authenticated that session, and
it does not flicker. `stage` is reported beside it for diagnosis and is never a gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DISPATCHER = REPO / "src" / "qsick" / "headless_actions.cjs"
PROBE = REPO / "native" / "qsick-pipe" / "src" / "ipc_probe.cpp"
HTTP = REPO / "native" / "qsick-pipe" / "src" / "qsick_http.cpp"


def login_info_block() -> str:
    text = DISPATCHER.read_text(encoding="utf-8")
    start = text.index("if (op === 'login.info')")
    return text[start:start + 2000]


class TestLoginInfoIsGatedOnTheAccountOnly:
    def test_a_missing_account_is_refused(self):
        assert "!identity.uin" in login_info_block(), "没有账号时必须拒绝"

    def test_the_stage_is_not_a_gate(self):
        """Reintroducing this would bring back the flicker.

        The stage is rewritten by the QR refresh long after the listener attached, so gating on it makes a
        working session report unready between rewrites.
        """
        block = login_info_block()
        assert "ready === false" not in block, "不能再用 ready 门控 login.info"
        assert "is not confirmed yet" not in block

    def test_the_account_comes_first(self):
        block = login_info_block()
        assert block.index("identity.uin") < block.index("resultId"), \
            "先确认账号，再构造返回值"


class TestTheInjectedSideStillReportsTheStage:
    def test_the_persistent_identity_carries_stage(self):
        text = PROBE.read_text(encoding="utf-8", errors="replace")
        assert "stage:__st0" in text, "阶段仍要对外可见，否则出问题无从查起"

    def test_the_fallback_identity_carries_it_too(self):
        # The fallback runs during the first seconds after a launch, which is when the state is most
        # interesting; it used to copy only uin/uid.
        text = HTTP.read_text(encoding="utf-8", errors="replace")
        assert "stage:__st" in text

    def test_readiness_is_still_latched(self):
        """`ready` remains in the payload even though nothing gates on it.

        It is the honest answer to "has the message listener attached", and it is latched rather than derived
        from the mutable stage, so it cannot flicker.
        """
        text = PROBE.read_text(encoding="utf-8", errors="replace")
        assert "__lb.listenerReady=true" in text
        assert "__lb.listenerReady===true" in text

    def test_saving_the_boot_file_updates_memory(self):
        # `__lb` is what the action surface reads; the file is what everything outside reads. They diverged
        # once -- `__save` only wrote the file, so `__lb.stage` never reached listener_ready.
        text = PROBE.read_text(encoding="utf-8", errors="replace")
        save = text[text.index("function __save(name,text){"):]
        # The function's own body, up to the next declaration.
        save = save[:save.index("function __append")]
        assert "login-boot.json" in save and "__lb[__bk]=__nv" in save, \
            "写 boot 文件时必须同步 __lb，否则内存与磁盘会不一致"

    def test_identity_is_never_blanked_by_a_later_write(self):
        """A write that does not know the account must not erase what we already know.

        `onSessionInitComplete` writes `uin:__uin` from a value captured when the session closure was created;
        when that ran before the account was known it wrote `uin:""`, which erased a good uin and made
        `login.info` answer "identity not available" every few seconds.
        """
        text = PROBE.read_text(encoding="utf-8", errors="replace")
        assert "__keep={uin:1,uid:1,nickname:1}" in text
        assert "__lb[__bk])continue" in text, "空值不能覆盖已知道的身份字段"


class TestATimeoutIsNotAnAnswer:
    def test_the_http_layer_reports_a_queue_timeout_as_503(self):
        # Replying 200 + ok:false made every caller treat "we could not ask" as "it said no".
        text = HTTP.read_text(encoding="utf-8", errors="replace")
        assert "503" in text and "retryable" in text

    def test_login_info_does_not_need_a_wrapper_session(self):
        """It only reads the boot identity; requiring a session turned an answerable question into an error.

        Measured: the session object is transiently unavailable while the client is busy, and both sessions of
        a launch hit it at the same moment.
        """
        text = HTTP.read_text(encoding="utf-8", errors="replace")
        assert "if(!session&&op!=='login.info')" in text
