"""maxStore 的表结构。

两张表各管一件事：

* ``messages``——一条消息一行，放"能走索引的列 + 完整原文"。原文列是权威副本，
  即便索引列哪天算错了，也还能从 ``raw_json`` 重建。
* ``raw_frames``——同一条消息按 JSON 指针摊平后的每一行。存在的理由是"以后 schema
  变了旧数据还查得到"，所以它是冗余的，冗余正是它的功能。

ID 用 TEXT 主键而不是 INTEGER：QQ 的 message_id 是 uint64，超过 2^63 的 ID 用 int
主键要么报错要么失真，而协议层的职责恰好是不失真。整数镜像列 ``message_id`` 只为
方便按数值关联，不作为唯一依据。
"""

from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_key   TEXT PRIMARY KEY,
    message_id    INTEGER,
    seq           INTEGER NOT NULL DEFAULT 0,
    time          INTEGER NOT NULL DEFAULT 0,
    chat_type     TEXT    NOT NULL DEFAULT '',
    chat_id       INTEGER,
    chat_id_text  TEXT    NOT NULL DEFAULT '',
    sender_uin    INTEGER,
    sender_uid    TEXT    NOT NULL DEFAULT '',
    sender_name   TEXT    NOT NULL DEFAULT '',
    card          TEXT    NOT NULL DEFAULT '',
    role          TEXT    NOT NULL DEFAULT '',
    account       TEXT    NOT NULL DEFAULT '',
    protocol      TEXT    NOT NULL DEFAULT '',
    elements_json TEXT    NOT NULL DEFAULT '[]',
    raw_json      TEXT    NOT NULL DEFAULT '{}',
    meta_json     TEXT    NOT NULL DEFAULT '{}',
    element_count INTEGER NOT NULL DEFAULT 0,
    frame_count   INTEGER NOT NULL DEFAULT 0,
    stored_at     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_maxstore_chat ON messages(chat_type, chat_id, time);
CREATE INDEX IF NOT EXISTS idx_maxstore_time ON messages(time);
CREATE INDEX IF NOT EXISTS idx_maxstore_stored ON messages(stored_at);
CREATE INDEX IF NOT EXISTS idx_maxstore_mid ON messages(message_id);

CREATE TABLE IF NOT EXISTS raw_frames (
    message_key TEXT NOT NULL,
    path        TEXT NOT NULL,
    value_type  TEXT NOT NULL,
    value_json  TEXT,
    depth       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_key, path)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_maxstore_frame_path ON raw_frames(path);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
"""

# 删除消息时一并清掉它的摊平行，避免孤儿行把库撑大。
SCHEMA_DROP_FRAMES = "DELETE FROM raw_frames WHERE message_key = ?"
