"""UI contracts without starting runtime or touching real config/accounts."""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[1] / "src/qsick/webui/templates"


def render(enabled):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    return env.get_template("chat.html").render(
        page="chat", chat_enabled=enabled,
        info={"version": "test", "chat_enabled": enabled, "logged_in": False,
              "webui_auth": False, "transport": "test", "onebot": {"host": "localhost", "port": 0},
              "webui": {"host": "localhost", "port": 0}},
    )


def test_workspace_has_conversation_filters_and_composer():
    html = render(True)
    for value in ('id="conversation-search"', 'data-filter="private"', 'id="chat-latest"',
                  '<textarea id="chat-input"', 'id="account-menu"', '/static/chat.js?', '/static/chat.css?'):
        assert value in html
    assert 'data-page="chat"' in html
    assert 'id="chat-enable-btn"' not in html
    assert 'window.open' not in html


def test_gate_keeps_countdown_and_does_not_start_chat_polling():
    html = render(False)
    assert 'id="chat-enable-btn" class="btn" disabled' in html
    assert '确认启用 (3)' in html
    assert '/static/chat.js?' not in html
    assert '<textarea id="chat-input"' not in html


def test_send_mode_menu_offers_both_shortcuts():
    html = render(True)
    assert 'id="chat-send-mode"' in html
    assert 'id="chat-send-menu"' in html
    assert 'data-mode="enter"' in html
    assert 'data-mode="ctrl-enter"' in html
    assert "按 Enter 键发送消息" in html
    assert "按 Ctrl + Enter 键发送消息" in html


def test_login_child_reports_ready_and_parent_closes_overlay():
    accounts = (TEMPLATES / "accounts.html").read_text(encoding="utf-8")
    login = (TEMPLATES / "login_qr.html").read_text(encoding="utf-8")
    assert "qsick:login-ready" in login
    assert "postMessage" in login
    assert "qsick:login-ready" in accounts
    assert 'data.type !== "qsick:login-ready"' in accounts
    # Origin is checked before acting on the message.
    assert "event.origin !== window.location.origin" in accounts
