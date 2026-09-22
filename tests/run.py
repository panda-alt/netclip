"""极简测试运行器 —— 环境里没有 pytest 时的零依赖替代品。

用法::

    python -m tests.run            # 跑全部
    python -m tests.run layout     # 只跑名字里含 layout 的模块

支持 pytest 的三个常用特性：`assert`、`pytest.raises`、`pytest.mark.parametrize`、
`pytest.approx`、`pytest.skip`。之所以自己写而不用 pytest，是因为这个项目的
"测试环境"就是用户本人的两台机器，不该为了跑测试去装东西。
但在有 pytest 的环境里这些测试文件同样可以直接 `pytest tests/` 运行。
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import sys
import traceback
from typing import Any, Callable, List, Tuple


# --------------------------------------------------------------------- pytest 垫片


class _Approx:
    """pytest.approx 的最小实现。

    参数名刻意用 `abs`（虽然遮蔽内建名），以匹配真实 pytest 的关键字调用方式。
    """

    def __init__(self, expected: Any, rel: float = 1e-6, abs: float = 1e-12) -> None:  # noqa: A002
        self.expected = expected
        self.rel = rel
        self.abs = abs

    def __eq__(self, other: Any) -> bool:
        try:
            return abs(float(other) - float(self.expected)) <= max(
                self.abs, self.rel * max(abs(float(other)), abs(float(self.expected)))
            )
        except (TypeError, ValueError):
            return NotImplemented

    def __repr__(self) -> str:  # pragma: no cover
        return "approx(%r)" % (self.expected,)


class _Raises:
    """pytest.raises 的最小实现，支持 `match` 正则。"""

    def __init__(self, expected: Any, match: Optional[str] = None) -> None:
        self.expected = expected
        self.match = match
        self.value: Any = None

    def __enter__(self) -> "_Raises":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            raise AssertionError("期望抛出 %s，但没有异常" % getattr(self.expected, "__name__", self.expected))
        if not issubclass(exc_type, self.expected):
            return False
        self.value = exc
        if self.match is not None:
            import re

            if not re.search(self.match, str(exc)):
                raise AssertionError("异常信息 %r 不匹配正则 %r" % (str(exc), self.match))
        return True


class _Skip(Exception):
    pass


class _Mark:
    def parametrize(self, argnames: str, argvalues: List[Any]) -> Callable:
        names = [n.strip() for n in argnames.split(",")]

        def decorator(fn: Callable) -> Callable:
            setattr(fn, "_parametrize", (names, list(argvalues)))
            return fn

        return decorator

    def skipif(self, condition: Any, reason: str = "") -> Callable:
        flag = bool(condition)

        def decorator(fn: Callable) -> Callable:
            if flag:
                setattr(fn, "_skip_reason", reason or "skipif 条件成立")
            return fn

        return decorator

    def skip(self, reason: str = "") -> None:
        raise _Skip(reason)

    def __getattr__(self, name: str) -> Callable:  # pragma: no cover - 忽略未知 mark
        def noop(*args: Any, **kwargs: Any) -> Callable:
            def decorator(fn: Callable) -> Callable:
                return fn

            return decorator

        return noop


class _MonkeyPatch:
    """`monkeypatch` 的最小实现：只支持 `setattr` / `delattr` 并在结束时回滚。

    测试里用它来把真实的 Win32 对象换成假对象（例如把 `TrayWindow` 换成
    只记录调用的替身），从而在不产生真实窗口/托盘图标的前提下测到分发逻辑。
    """

    def __init__(self) -> None:
        self._undo: List[Callable[[], None]] = []

    def setattr(self, target: Any, name: str, value: Any = None, raising: bool = True) -> None:
        if isinstance(target, str):
            # pytest 的 monkeypatch.setattr("mod.attr", value) 形式
            module_path, _, attr = target.rpartition(".")
            target = importlib.import_module(module_path)
            name, value = attr, name
        had = hasattr(target, name)
        old = getattr(target, name, None)
        setattr(target, name, value)

        def undo() -> None:
            if had:
                setattr(target, name, old)
            else:
                try:
                    delattr(target, name)
                except AttributeError:  # pragma: no cover
                    pass

        self._undo.append(undo)

    def delattr(self, target: Any, name: str = None, raising: bool = True) -> None:  # type: ignore[assignment]
        if isinstance(target, str):  # pragma: no cover
            module_path, _, attr = target.rpartition(".")
            target = importlib.import_module(module_path)
            name = attr
        old = getattr(target, name)
        delattr(target, name)

        def undo() -> None:
            setattr(target, name, old)

        self._undo.append(undo)

    def setenv(self, name: str, value: str, prepend: str = None) -> None:  # type: ignore[assignment]
        old = os.environ.get(name)
        os.environ[name] = str(value)

        def undo() -> None:
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

        self._undo.append(undo)

    def undo(self) -> None:
        while self._undo:
            try:
                self._undo.pop()()
            except Exception:  # pragma: no cover
                pass


def _builtin_fixtures() -> "dict[str, Callable]":
    return {"monkeypatch": _MonkeyPatch}


class _PytestShim:
    """伪装成 pytest 模块，供测试文件 `import pytest` 使用。"""

    approx = staticmethod(_Approx)
    raises = staticmethod(_Raises)
    mark = _Mark()

    @staticmethod
    def skip(reason: str = "") -> None:
        raise _Skip(reason)

    @staticmethod
    def fail(reason: str = "") -> None:
        raise AssertionError(reason)

    @staticmethod
    def fixture(*args: Any, **kwargs: Any) -> Callable:
        """支持 `@pytest.fixture` 与 `@pytest.fixture()` 两种写法。

        被装饰的函数会打上 `_is_fixture` 标记，运行器据此把它的返回值注入到
        同名参数的用例里（见 `_iter_tests`）。
        """
        def mark(fn: Callable) -> Callable:
            setattr(fn, "_is_fixture", True)
            return fn

        if args and callable(args[0]):
            return mark(args[0])
        return mark


def install_pytest_shim() -> bool:
    """如果真实 pytest 不可用，装一个垫片。返回是否装了垫片。"""
    try:
        import pytest  # noqa: F401

        return False
    except ImportError:
        sys.modules["pytest"] = _PytestShim()  # type: ignore[assignment]
        return True


# --------------------------------------------------------------------- 运行


def _collect_fixtures(module: Any) -> "dict[str, Callable]":
    """收集模块里被 `@pytest.fixture` 标记的函数。"""
    out: dict = {}
    for name in dir(module):
        fn = getattr(module, name, None)
        if callable(fn) and getattr(fn, "_is_fixture", False):
            out[name] = fn
    return out


def _iter_tests(module: Any) -> "List[Tuple[str, Callable[[], None]]]":
    cases: List[Tuple[str, Callable[[], None]]] = []
    fixtures = _collect_fixtures(module)
    for name in sorted(vars(module)):
        if not name.startswith("test_"):
            continue
        fn = getattr(module, name)
        if not callable(fn):
            continue
        if getattr(fn, "_is_fixture", False):
            continue
        skip_reason = getattr(fn, "_skip_reason", None)
        if skip_reason:
            cases.append((name, _make_skipper(skip_reason)))
            continue
        params = getattr(fn, "_parametrize", None)
        if not params:
            cases.append((name, _inject_fixtures(fn, fixtures)))
            continue
        argnames, argvalues = params
        for values in argvalues:
            if len(argnames) == 1:
                values = (values,)
            label = "%s[%s]" % (name, ",".join(repr(v) for v in values))
            cases.append((label, _bind(fn, argnames, values)))
    return cases


def _inject_fixtures(fn: Callable, fixtures: "dict[str, Callable]") -> Callable[[], None]:
    """把用例签名里出现的 fixture 名字解析成实际参数。

    只支持"无参 fixture"（够用了）。缺 fixture 时保持原样调用，让
    TypeError 带着"缺少参数"的信息暴露出来，而不是被静默吞掉。
    """
    try:
        import inspect as _inspect

        sig = _inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover
        return fn

    needed = [p.name for p in sig.parameters.values() if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
    if not needed:
        return fn
    sources = dict(fixtures)
    sources.update(_builtin_fixtures())
    if any(name not in sources for name in needed):
        return fn

    def run() -> None:
        created: List[Any] = []
        # fixture 之间可以互相依赖（例如 `controller` 需要 `monkeypatch`），
        # 所以这里递归解析，而不是只解析一层。
        kwargs = {name: _resolve(name, sources, created, set()) for name in needed}
        try:
            fn(**kwargs)
        finally:
            for value in created:
                if isinstance(value, _MonkeyPatch):
                    value.undo()

    return run


def _resolve(name: str, sources: "dict[str, Callable]", created: List[Any], stack: "set[str]") -> Any:
    """按名字解析一个 fixture（递归解析它自己的依赖）。"""
    if name in stack:
        raise AssertionError("fixture 依赖成环: %s" % " -> ".join(list(stack) + [name]))
    factory = sources[name]
    try:
        import inspect as _inspect

        sig = _inspect.signature(factory)
    except (TypeError, ValueError):  # pragma: no cover
        sig = None

    deps: List[str] = []
    if sig is not None:
        deps = [
            p.name
            for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
    stack = stack | {name}
    kwargs = {dep: _resolve(dep, sources, created, stack) for dep in deps if dep in sources}
    value = factory(**kwargs)
    created.append(value)
    return value


def _bind(fn: Callable, argnames: List[str], values: Tuple[Any, ...]) -> Callable[[], None]:
    def run() -> None:
        fn(**dict(zip(argnames, values)))

    return run


def _make_skipper(reason: str) -> Callable[[], None]:
    def run() -> None:
        raise _Skip(reason)

    return run


def _call(fn: Callable) -> None:
    """跑一个用例。`async def` 的用例会被自动用 asyncio 跑起来。

    为什么要支持异步用例：文件传输的发送路径是协程（它要在数据块之间 await 让路），
    只测同步部分等于没测到真正会卡住的那段代码。
    """
    if inspect.iscoroutinefunction(fn):
        import asyncio

        asyncio.run(fn())
        return
    result = fn()
    if inspect.iscoroutine(result):
        import asyncio

        asyncio.run(result)


def main(argv: "List[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    keyword = argv[0] if argv else ""
    shimmed = install_pytest_shim()

    # 有些失败是发生在**后台线程**里的（比如剪贴板写线程），用例本身只能看到
    # 一个统计计数器不对，真正的异常在被吞掉的线程里。把 ERROR 级别的日志打开，
    # 异常就会跟着打出来 —— 否则这种间歇性失败基本查不出原因。
    import logging as _logging

    if not _logging.getLogger().handlers:
        _logging.basicConfig(level=_logging.ERROR, format="   [%(levelname)s %(name)s] %(message)s")

    # netclip 的剪贴板层有一个诊断开关：EmptyClipboard 失败时打印线程/错误码。
    # 这类失败只在"跑完整套件"时出现（剪贴板被前面的用例搞热了），
    # 用环境变量打开开关，方便按需复现，默认不刷日志。
    if os.environ.get("NETCLIP_DIAG_CLIPBOARD"):
        import netclip.win.clipboard as _cb

        _cb._DIAG_EMPTY = True  # noqa: SLF001

    import tests as tests_pkg

    total = failed = skipped = 0
    failures: List[str] = []

    print("=== netclip 测试 ===" + ("（使用内置 pytest 垫片）" if shimmed else "（使用真实 pytest 垫片无关的运行器）"))
    for info in pkgutil.iter_modules(tests_pkg.__path__):
        if not info.name.startswith("test_"):
            continue
        if keyword and keyword not in info.name:
            continue
        modname = "tests.%s" % info.name
        module = importlib.import_module(modname)
        cases = _iter_tests(module)
        print("\n-- %s (%d 个用例)" % (modname, len(cases)))
        for name, fn in cases:
            total += 1
            try:
                _call(fn)
            except _Skip as exc:
                skipped += 1
                print("   SKIP %s: %s" % (name, exc))
            except Exception:
                failed += 1
                print("   FAIL %s" % name)
                detail = traceback.format_exc()
                failures.append("%s::%s\n%s" % (modname, name, detail))
                for line in detail.strip().splitlines()[-4:]:
                    print("        " + line)
            else:
                print("   ok   %s" % name)

    print("\n=== 结果: %d 通过, %d 失败, %d 跳过, 共 %d ===" % (total - failed - skipped, failed, skipped, total))
    if failures:
        print("\n--- 失败详情 ---")
        for item in failures:
            print(item)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
