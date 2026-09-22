"""极简 TOML 解析器 —— 仅在 tomllib / tomli 都不可用时作为兜底。

只实现配置文件实际用到的 TOML 子集：
  * 注释 `#`
  * 表头 `[a.b.c]`
  * 键值对 `key = value`
  * 基本字符串 "..."、字面量字符串 '...'（含 `\\` 转义）
  * 整数（含下划线）、浮点、布尔、数组（可跨行）
  * 内联表 `{a = 1, b = 2}`

有意不实现：数组表 `[[x]]`、多行字符串、日期时间。
遇到不支持的结构会抛 `TomlLiteError`，而不是静默给出错误结果。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


class TomlLiteError(ValueError):
    pass


_BARE_KEY = re.compile(r"^[A-Za-z0-9_\-]+$")
_INT = re.compile(r"^[+-]?\d[\d_]*$")
_HEX = re.compile(r"^0x[0-9A-Fa-f_]+$")
_OCT = re.compile(r"^0o[0-7_]+$")
_BIN = re.compile(r"^0b[01_]+$")
_FLOAT = re.compile(r"^[+-]?(\d[\d_]*)?\.\d[\d_]*([eE][+-]?\d+)?$|^[+-]?\d[\d_]*[eE][+-]?\d+$")


def _strip_comment(line: str) -> str:
    """去掉行内注释，但尊重引号内的 #。"""
    out: List[str] = []
    quote: str = ""
    escaped = False
    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote == '"':
            out.append(ch)
            escaped = True
            continue
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).strip()


def _split_key_value(text: str) -> Tuple[str, str]:
    """按第一个不在引号内的 `=` 切分。"""
    quote = ""
    escaped = False
    for idx, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "=":
            return text[:idx].strip(), text[idx + 1 :].strip()
    raise TomlLiteError("缺少 '=': %r" % text)


def _parse_key(raw: str) -> List[str]:
    parts: List[str] = []
    for chunk in raw.split("."):
        chunk = chunk.strip()
        if not chunk:
            raise TomlLiteError("空键名: %r" % raw)
        if chunk[0] in "\"'":
            parts.append(_parse_string(chunk))
        elif _BARE_KEY.match(chunk):
            parts.append(chunk)
        else:
            raise TomlLiteError("非法键名: %r" % chunk)
    return parts


def _parse_string(raw: str) -> str:
    if len(raw) < 2 or raw[0] != raw[-1] or raw[0] not in "\"'":
        raise TomlLiteError("字符串未闭合: %r" % raw)
    body = raw[1:-1]
    if raw[0] == "'":
        return body
    out: List[str] = []
    idx = 0
    while idx < len(body):
        ch = body[idx]
        if ch != "\\":
            out.append(ch)
            idx += 1
            continue
        idx += 1
        if idx >= len(body):
            raise TomlLiteError("字符串以反斜杠结尾: %r" % raw)
        esc = body[idx]
        idx += 1
        simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "b": "\b", "f": "\f"}
        if esc in simple:
            out.append(simple[esc])
        elif esc == "u":
            out.append(chr(int(body[idx : idx + 4], 16)))
            idx += 4
        elif esc == "U":
            out.append(chr(int(body[idx : idx + 8], 16)))
            idx += 8
        else:
            raise TomlLiteError("未知转义 \\%s" % esc)
    return "".join(out)


def _split_top_level(text: str) -> List[str]:
    """按顶层逗号切分（忽略引号与嵌套括号内的逗号）。"""
    items: List[str] = []
    buf: List[str] = []
    depth = 0
    quote = ""
    escaped = False
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote == '"':
            buf.append(ch)
            escaped = True
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        raise TomlLiteError("空值")
    if raw[0] == "[":
        if raw[-1] != "]":
            raise TomlLiteError("数组未闭合: %r" % raw)
        body = raw[1:-1].strip()
        if not body:
            return []
        return [_parse_value(item) for item in _split_top_level(body)]
    if raw[0] == "{":
        if raw[-1] != "}":
            raise TomlLiteError("内联表未闭合: %r" % raw)
        body = raw[1:-1].strip()
        table: Dict[str, Any] = {}
        if not body:
            return table
        for item in _split_top_level(body):
            raw_key, raw_val = _split_key_value(item)
            _assign(table, _parse_key(raw_key), _parse_value(raw_val))
        return table
    if raw[0] in "\"'":
        return _parse_string(raw)
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _HEX.match(raw):
        return int(raw.replace("_", ""), 16)
    if _OCT.match(raw):
        return int(raw.replace("_", ""), 8)
    if _BIN.match(raw):
        return int(raw.replace("_", ""), 2)
    if _INT.match(raw):
        return int(raw.replace("_", ""))
    if _FLOAT.match(raw):
        return float(raw.replace("_", ""))
    raise TomlLiteError("无法识别的值: %r" % raw)


def _assign(root: Dict[str, Any], path: List[str], value: Any) -> None:
    node = root
    for key in path[:-1]:
        nxt = node.get(key)
        if nxt is None:
            nxt = {}
            node[key] = nxt
        if not isinstance(nxt, dict):
            raise TomlLiteError("键 %r 已被标量占用" % key)
        node = nxt
    node[path[-1]] = value


def loads(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    current: List[str] = []
    pending: Tuple[List[str], str] | None = None  # (key path, 累积中的值文本)

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw_line)

        if pending is not None:
            key_path, acc = pending
            acc = (acc + " " + line).strip() if line else acc
            if _brackets_balanced(acc):
                _assign(root, key_path, _parse_value(acc))
                pending = None
            else:
                pending = (key_path, acc)
            continue

        if not line:
            continue
        if line.startswith("[["):
            raise TomlLiteError("第 %d 行: 不支持数组表 [[...]]" % lineno)
        if line.startswith("["):
            if not line.endswith("]"):
                raise TomlLiteError("第 %d 行: 表头未闭合" % lineno)
            current = _parse_key(line[1:-1].strip())
            if current not in _table_paths(root):
                _assign(root, current, {})
            continue
        if line.startswith("=") or line.startswith("."):
            raise TomlLiteError("第 %d 行: 非法行首" % lineno)

        try:
            raw_key, raw_val = _split_key_value(line)
        except TomlLiteError as exc:
            raise TomlLiteError("第 %d 行: %s" % (lineno, exc)) from None
        key_path = current + _parse_key(raw_key)
        if not _brackets_balanced(raw_val):
            pending = (key_path, raw_val)
            continue
        _assign(root, key_path, _parse_value(raw_val))

    if pending is not None:
        raise TomlLiteError("文件末尾有未闭合的值: %r" % pending[1])
    return root


def _brackets_balanced(text: str) -> bool:
    depth = 0
    quote = ""
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
    return depth <= 0 and not quote


def _table_paths(root: Dict[str, Any]) -> List[List[str]]:
    """收集已存在的表路径，供表头重复时去重使用。"""
    found: List[List[str]] = []
    stack: List[Tuple[List[str], Dict[str, Any]]] = [([], root)]
    while stack:
        prefix, node = stack.pop()
        for key, value in node.items():
            if isinstance(value, dict):
                path = prefix + [key]
                found.append(path)
                stack.append((path, value))
    return found
