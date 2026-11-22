"""JSON 指针工具：把原始 payload 摊平成 (路径, 类型, 值) 序列，再按任意路径取回。

为什么不直接在 SQL 里用 JSON1 查 ``raw_json``：JSON1 查到的永远是"这条 JSON 现在长
什么样"，而这里的目的是"十年后字段改了名、嵌套又深了一层，旧数据里那一层还取得回来"。
摊平之后路径本身也是数据，schema 演进不影响旧行。

每个节点（含容器）都有一行，容器行的 ``value_json`` 为 NULL。多存这一行的原因有两个：
空 `{}` 和空 `[]` 必须能区分；有子节点时也要靠这一行确认父节点是对象还是数组——否则
一个用数字做键的对象（``{"12345": {...}}``）会被误还原成列表，那属于静默改数据。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

OBJECT = "object"
ARRAY = "array"
STRING = "string"
NUMBER = "number"
BOOLEAN = "boolean"
NULL = "null"
# 非 JSON 原生对象（bytes、set……）。平台偶尔会塞进来，打标记保留而不是丢弃。
EXOTIC = "exotic"
# 环引用：调用方直接传进来的 Python 对象可能有环，JSON 里不会有。
CYCLE = "cycle"
# 超过深度上限：留一条"这里还有东西"的记录，避免深嵌套把内存吃光。
TRUNCATED = "truncated"

CONTAINER_TYPES = (OBJECT, ARRAY)
SCALAR_TYPES = (STRING, NUMBER, BOOLEAN, NULL, EXOTIC, CYCLE, TRUNCATED)

# 深度上限压在 CPython 默认递归限制（1000）之下：``json.dumps`` 是递归实现，超了会
# 抛 RecursionError，那样连"存下来"这一步都做不到。真实协议 payload 的嵌套深度在
# 十几层量级，512 只会在恶意构造的输入上触发。
MAX_DEPTH = 512


# ------------------------------------------------------------------- pointers


def escape_token(token: str) -> str:
    """RFC 6901 转义：``~`` -> ``~0``，``/`` -> ``~1``。"""
    return str(token).replace("~", "~0").replace("/", "~1")


def unescape_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def parse_path(path: Any) -> list[str]:
    """把调用方写的路径统一成 token 序列。

    同时接受 RFC 6901 指针（``/sender/card``）、带 ``$`` 的 JSONPath 简写
    （``$.sender.card``）和点/方括号写法（``elements[0].data.text``）。这些都是同一串
    token 的不同外壳，只留一个入口，调用方不用记本库的方言；键里含 ``/`` 或 ``.`` 时
    用指针形式加转义即可表达。

    解析不了的路径返回 ``[]``（等价于整条消息）而不抛异常：对调用方来说"路径写错了"
    和"查不到"是同一件事，没必要逼它去区分两种空。
    """
    if path is None:
        return []
    if isinstance(path, (list, tuple)):
        return [str(token) for token in path]
    if isinstance(path, str):
        return [unescape_token(token) for token in _pointer_tokens(path)]
    return [str(path)]


def _pointer_tokens(path: str) -> list[str]:
    text = path.strip()
    if text.startswith("$"):
        text = text[1:]
    if text in ("", "/"):
        return []
    if text.startswith("/"):
        return text.split("/")[1:]
    return _dotted_tokens(text)


def _dotted_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for chunk in text.replace("[", ".[").split("."):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.startswith("[") and chunk.endswith("]"):
            inner = chunk[1:-1].strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
                inner = inner[1:-1]
            tokens.append(inner)
        else:
            tokens.append(chunk)
    return tokens


def to_pointer(tokens: Sequence[str]) -> str:
    return "".join("/" + escape_token(token) for token in tokens)


def depth_of(pointer: str) -> int:
    return pointer.count("/")


# ------------------------------------------------------------------ serialize


def classify(value: Any) -> str:
    if isinstance(value, Mapping):
        return OBJECT
    if isinstance(value, (list, tuple)):
        return ARRAY
    if isinstance(value, str):
        return STRING
    if isinstance(value, bool):
        return BOOLEAN
    if isinstance(value, (int, float)):
        return NUMBER
    if value is None:
        return NULL
    return EXOTIC


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def sanitize(value: Any, *, max_depth: int = MAX_DEPTH) -> Any:
    """把超深子树换成截断标记，让 payload 能被 JSON 序列化。

    ``json.dumps`` 内部是递归的，几千层嵌套会直接 RecursionError——那样连"存下来"都
    做不到。这里先在 512 层（真实协议从不接近）截断并打标记，剩下的部分照原样保留；
    宁可留一条"这里还有更深的层"的证据，也不要整条消息写入失败。
    """
    if max_depth <= 0:
        return {"__truncated_depth__": 0}
    return _sanitize(value, max_depth, 0, set())


def _sanitize(node: Any, max_depth: int, depth: int, on_path: set[int]) -> Any:
    kind = classify(node)
    if kind not in CONTAINER_TYPES:
        return node
    if depth >= max_depth:
        return {"__truncated_depth__": depth}
    if id(node) in on_path:
        return {"__circular_ref__": depth}
    on_path.add(id(node))
    try:
        if kind == OBJECT:
            return {str(key): _sanitize(child, max_depth, depth + 1, on_path) for key, child in node.items()}
        return [_sanitize(child, max_depth, depth + 1, on_path) for child in node]
    finally:
        on_path.discard(id(node))


def _json_default(obj: Any) -> Any:
    """序列化兜底：打标记而不是转成 str，否则类型信息在入库那一步就丢了。

    "存下来的和收到的不一样"比"存不下来"更危险——前者会让事后审计悄悄得出错的结论。
    """
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return {"__bytes_b64__": base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, (set, frozenset)):
        # 元素本身已是字符串，不要再 dump 一次，否则会多出一层引号转义。
        return {"__set__": [repr(item) for item in sorted(obj, key=repr)]}
    return {"__unserializable__": type(obj).__name__, "repr": repr(obj)}


def flatten(payload: Any, *, max_depth: int = MAX_DEPTH) -> list[tuple[str, str, str | None]]:
    """payload -> ``[(指针, 类型, JSON 文本), ...]``，每个节点一行，容器行文本为 NULL。

    用显式栈而不是递归：畸形或恶意输入可以嵌套几千层，递归会先把解释器打穿。环检测只
    沿当前路径追踪（用哨兵标记出栈），而不是全局记录见过的对象——否则一份被多处共享的
    子对象（例如 ``native`` 同时挂在两个键下）会被误判成环，那才是真的丢数据。
    """
    frames: list[tuple[str, str, str | None]] = []
    on_path: set[int] = set()
    # 出栈标记用 (POP, id) 二元组区分于 (pointer, node, depth) 三元组：容器处理完它的
    # 子节点之后才把自己从 on_path 摘掉，否则共享同一子对象的兄弟节点会被误判成环。
    POP = "\x00pop"
    stack: list[tuple] = [("", payload, 0)]
    while stack:
        item = stack.pop()
        if len(item) == 2 and item[0] == POP:
            on_path.discard(item[1])
            continue
        pointer, node, depth = item
        kind = classify(node)
        if depth > max_depth:
            frames.append((pointer, TRUNCATED, dump({"__truncated_depth__": depth})))
            continue
        if kind not in CONTAINER_TYPES:
            if kind == NULL:
                frames.append((pointer, NULL, None))
            else:
                frames.append((pointer, kind, dump(node)))
            continue
        if id(node) in on_path:
            frames.append((pointer, CYCLE, dump({"__circular_ref__": pointer})))
            continue
        # 容器行本身也要落：空容器与容器类型都靠它，见模块说明。
        frames.append((pointer, kind, None))
        on_path.add(id(node))
        stack.append((POP, id(node)))
        entries = list(node.items()) if kind == OBJECT else list(enumerate(node))
        for token, child in reversed(entries):
            stack.append((pointer + "/" + escape_token(token), child, depth + 1))
    return frames


def decode_value(value_type: str, value_json: str | None) -> Any:
    if value_json is None:
        if value_type == OBJECT:
            return {}
        if value_type == ARRAY:
            return []
        return None
    try:
        return json.loads(value_json)
    except (TypeError, ValueError):
        # 半截行（断电、被人手改过库）时把原文还回去，比抛异常有用。
        return value_json


# --------------------------------------------------------------------- rebuild


def rebuild(rows: Iterable[tuple[str, str, str | None]], root_type: str | None = None) -> Any:
    """把相对同一父路径的多行还原成嵌套结构。

    ``rows`` 的路径是相对那个父路径的（``sender/card``、``0/data/text``）。容器的类型
    取自它自己那一行，所以数字键对象不会被还原成列表。
    """
    entries = [(_relative_tokens(path), kind, text) for path, kind, text in rows]
    if not entries:
        return None
    kinds = {tuple(tokens): kind for tokens, kind, _text_value in entries}
    if root_type not in CONTAINER_TYPES:
        root_type = _infer_root(tokens for tokens, _kind, _text_value in entries if tokens)
    root: Any = [] if root_type == ARRAY else {}
    # 先建容器槽位再填叶子：否则空容器会消失，而且父行的空 dict 会覆盖已填好的子节点。
    for tokens, kind, _text_value in entries:
        if kind in CONTAINER_TYPES and tokens:
            _slot(root, tokens, kinds)
    for tokens, kind, text in entries:
        if kind in CONTAINER_TYPES or not tokens:
            continue
        _slot(root, tokens, kinds)
        _set_leaf(_walk(root, tokens), tokens[-1], decode_value(kind, text))
    return root


def _relative_tokens(path: str) -> list[str]:
    return [unescape_token(token) for token in path.split("/") if token]


def _infer_root(token_lists: Iterable[Sequence[str]]) -> str:
    for tokens in token_lists:
        if tokens and not _is_index(tokens[0]):
            return OBJECT
        if tokens:
            return ARRAY
    return OBJECT


def _is_index(token: str) -> bool:
    return token.isdigit()


def _slot(root: Any, tokens: Sequence[str], kinds: Mapping[tuple[str, ...], str]) -> Any:
    """确保 ``tokens`` 这条路径上的每个祖先都存在且类型正确，返回最深一级的容器。"""
    node = root
    for position, token in enumerate(tokens[:-1]):
        want = kinds.get(tuple(tokens[: position + 1]), OBJECT)
        node = _child_slot(node, token, want)
    return node


def _walk(root: Any, tokens: Sequence[str]) -> Any:
    node = root
    for token in tokens[:-1]:
        node = _child(node, token)
    return node


def _child_slot(node: Any, token: str, want: str) -> Any:
    if isinstance(node, list):
        index = int(token) if _is_index(token) else len(node)
        while len(node) <= index:
            node.append(None)
        if not isinstance(node[index], (dict, list)):
            node[index] = [] if want == ARRAY else {}
        return node[index]
    existing = node.get(token) if isinstance(node, dict) else None
    if not isinstance(existing, (dict, list)):
        existing = [] if want == ARRAY else {}
        node[token] = existing
    return existing


def _child(node: Any, token: str) -> Any:
    if isinstance(node, list):
        return node[int(token)] if _is_index(token) and int(token) < len(node) else None
    return node.get(token) if isinstance(node, dict) else None


def _set_leaf(node: Any, token: str, value: Any) -> None:
    if isinstance(node, list):
        index = int(token) if _is_index(token) else len(node)
        while len(node) <= index:
            node.append(None)
        node[index] = value
        return
    if isinstance(node, dict):
        node[token] = value
