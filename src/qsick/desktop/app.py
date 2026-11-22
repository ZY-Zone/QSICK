"""Tkinter chat shell for QSICK — sky/white, line style, minimal."""

from __future__ import annotations

import os
import sys
import time
import tkinter as tk
from tkinter import messagebox

from .client import DesktopApi, DesktopApiError

BG = "#f7fbff"
PANEL = "#ffffff"
LINE = "#dbeafe"
TEXT = "#0f172a"
MUTED = "#5b6b82"
SKY = "#0ea5e9"


def main(argv: list[str] | None = None) -> int:
    onebot = os.environ.get("QSICK_ONEBOT", "http://127.0.0.1:3000")
    webui = os.environ.get("QSICK_WEBUI", "http://127.0.0.1:5754")
    token = os.environ.get("QSICK_ACCESS_TOKEN", "")
    webui_token = os.environ.get("QSICK_WEBUI_TOKEN", "")
    api = DesktopApi(onebot, webui, token, webui_token)

    root = tk.Tk()
    root.title("QSICK Desktop")
    root.geometry("1040x700")
    root.configure(bg=BG)

    header = tk.Frame(root, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
    header.pack(fill=tk.X)
    tk.Label(header, text="QSICK", bg=PANEL, fg=SKY, font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT, padx=14, pady=9)
    status = tk.Label(header, text="未连接", bg=PANEL, fg=MUTED, font=("Segoe UI", 9))
    status.pack(side=tk.RIGHT, padx=14)

    body = tk.Frame(root, bg=BG)
    body.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

    left = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1, width=240)
    left.pack(side=tk.LEFT, fill=tk.Y)
    left.pack_propagate(False)
    tk.Label(left, text="会话", bg=PANEL, fg=MUTED, anchor="w", font=("Segoe UI", 9)).pack(fill=tk.X, padx=10, pady=(8, 4))
    chats = tk.Listbox(
        left, bg=PANEL, fg=TEXT, selectbackground="#e0f2fe", selectforeground=TEXT,
        highlightthickness=0, borderwidth=0, font=("Segoe UI", 10),
    )
    chats.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    right = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
    right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))
    title = tk.Label(right, text="选择一个会话", bg=PANEL, fg=TEXT, anchor="w", font=("Segoe UI", 11, "bold"))
    title.pack(fill=tk.X, padx=12, pady=(8, 4))
    log = tk.Text(right, bg=PANEL, fg=TEXT, highlightthickness=0, borderwidth=0, font=("Segoe UI", 10), state=tk.DISABLED)
    log.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
    log.tag_configure("meta", foreground=MUTED)
    log.tag_configure("self", foreground=SKY)

    composer = tk.Frame(right, bg=PANEL)
    composer.pack(fill=tk.X, padx=12, pady=(4, 12))
    entry = tk.Entry(composer, bg=BG, fg=TEXT, relief=tk.FLAT, highlightbackground=LINE, highlightthickness=1, font=("Segoe UI", 10))
    entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
    send = tk.Button(
        composer, text="发送", bg=SKY, fg="white", relief=tk.FLAT, activebackground="#0284c7",
        font=("Segoe UI", 9, "bold"), padx=16,
    )
    send.pack(side=tk.RIGHT, padx=(8, 0))

    state: dict[str, object] = {"groups": [], "selected": None, "messages": [], "self_id": 0}

    def append(text: str, tag: str = "") -> None:
        log.configure(state=tk.NORMAL)
        log.insert(tk.END, text + "\n", tag)
        log.see(tk.END)
        log.configure(state=tk.DISABLED)

    def render_messages() -> None:
        log.configure(state=tk.NORMAL)
        log.delete("1.0", tk.END)
        log.configure(state=tk.DISABLED)
        selected = state["selected"]
        for message in state["messages"]:
            if selected is not None and int(message.get("chat_id") or 0) != int(selected):
                continue
            sender = message.get("sender_name") or message.get("sender_uin") or "?"
            body_text = message.get("text") or ""
            stamp = time.strftime("%H:%M", time.localtime(float(message.get("time") or 0)))
            append(f"[{stamp}] {sender}: {body_text}")

    def refresh() -> None:
        try:
            info = api.login_info()
            state["self_id"] = int(info.get("user_id") or 0)
            status.configure(text=f"在线 {info.get('nickname') or ''} ({state['self_id']})", fg="#0f9f6e")
            groups = api.group_list()
            if groups != state["groups"]:
                state["groups"] = groups
                chats.delete(0, tk.END)
                for group in groups:
                    chats.insert(tk.END, f"{group.get('group_name') or group.get('group_id')}")
            state["messages"] = api.messages()
            render_messages()
        except DesktopApiError as exc:
            status.configure(text=f"离线：{exc}", fg="#dc2626")
        root.after(3000, refresh)

    def select_chat(_event=None) -> None:
        index = chats.curselection()
        if not index:
            return
        group = state["groups"][index[0]]
        state["selected"] = group.get("group_id")
        title.configure(text=f"{group.get('group_name') or group.get('group_id')}  ·  {group.get('group_id')}")
        render_messages()

    def do_send(_event=None) -> None:
        text = entry.get().strip()
        if not text or state["selected"] is None:
            return
        try:
            api.send_group(int(state["selected"]), text)
            entry.delete(0, tk.END)
            append(f"我: {text}", "self")
        except DesktopApiError as exc:
            messagebox.showerror("发送失败", str(exc))

    chats.bind("<<ListboxSelect>>", select_chat)
    send.configure(command=do_send)
    entry.bind("<Return>", do_send)
    root.after(200, refresh)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
