"""Python 版本兼容垫片。

目标是 Python 3.10+（本机 3.10.0），因此：
  * `tomllib` 在 3.11 才进标准库，3.10 用 `tomli` 或自带的极简解析器兜底；
  * 数组/联合类型注解用 `typing` 形式，避免 3.10 语法差异。
"""

from __future__ import annotations

import sys
from typing import Any, Dict

IS_WINDOWS = sys.platform == "win32"
PY310 = sys.version_info[:2] == (3, 10)

# ---------------------------------------------------------------- TOML 加载

_TOML_BACKEND = "none"


def _load_toml_text(text: str) -> Dict[str, Any]:
    """把 TOML 文本解析成 dict，按可用后端降级。"""
    global _TOML_BACKEND

    try:
        import tomllib  # type: ignore[import-not-found]  # py3.11+

        _TOML_BACKEND = "tomllib"
        return tomllib.loads(text)
    except ImportError:
        pass

    try:
        import tomli  # type: ignore[import-not-found]

        _TOML_BACKEND = "tomli"
        return tomli.loads(text)
    except ImportError:
        pass

    from .toml_lite import loads as _lite_loads

    _TOML_BACKEND = "toml_lite"
    return _lite_loads(text)


def load_toml(path: str) -> Dict[str, Any]:
    # 用 utf-8-sig 是为了兼容记事本/PowerShell 写出的带 BOM 文件 —— TOML 解析器
    # 遇到 BOM 会在第 1 行第 1 列直接报 "Invalid statement"，那个报错很难看懂。
    with open(path, "r", encoding="utf-8-sig") as fh:
        return _load_toml_text(fh.read())


def toml_backend() -> str:
    """返回实际使用的 TOML 后端名，便于诊断输出。"""
    if _TOML_BACKEND != "none":
        return _TOML_BACKEND
    try:
        _load_toml_text("")
    except Exception:  # pragma: no cover - 仅在完全不可用时
        pass
    return _TOML_BACKEND
