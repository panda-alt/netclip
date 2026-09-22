"""构造/属性接线的静态一致性检查。

为什么值得单独一个文件
----------------------
这套源码是从会话记录里恢复出来的（原来的文件被误删了）。恢复过程中
`netclip/debug/` 整个模块被去掉，**引用它的地方没有全删干净**，于是真实崩过一次：

    ERROR 启动失败: HookThread.__init__() got an unexpected keyword argument 'chain'

这类残留有个共同点：**只在运行时、而且往往在启动路径上才炸**，测试完全看不见 ——
因为要触发它得真的构造 `Session` / 装全局钩子。上面那次是两个半截改动凑成的：
`HookThread` 的 `chain` 形参被删了，但它内部 `if self.chain is not None` 的守卫、
以及调用点的 `chain=self.chain` 都还在。

所以这里**不执行任何代码**，只做静态检查。两条，各管一半：

  1. 一个类里 `self.x` 被读、却从来没有被赋值过 —— 会在访问时 `AttributeError`；
  2. 调用点传了目标类不接受的**关键字参数** —— 会在调用时 `TypeError`。

第 1 条本可以用 `__getattr__` 或 `setattr` 绕过，第 2 条本可以靠 `**kwargs` 绕过，
所以两个检查都对这两种情况放行（见各自的实现）。
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "netclip"


def _source_files() -> "List[Path]":
    return sorted(PACKAGE.rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _classes(tree: ast.Module) -> "Iterator[ast.ClassDef]":
    return (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))


def _self_attribute_usage(cls: ast.ClassDef) -> "Tuple[Dict[str, int], Set[str]]":
    """返回 `({读到的 self.x: 行号}, {被赋值过/定义过的 self.x})`。"""
    assigned: "Set[str]" = set()
    read: "Dict[str, int]" = {}

    for node in ast.walk(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            #: 方法名（含嵌套定义的）都算"这个属性存在"
            assigned.add(node.name)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                assigned.add(node.attr)
            else:
                read.setdefault(node.attr, node.lineno)

    #: 类体上的直接赋值 / 注解也算
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.add(target.id)

    return read, assigned


def test_no_self_attribute_is_read_but_never_assigned():
    """`self.x` 被读、本类却从未赋值 —— 访问那一刻就是 `AttributeError`。

    这正是 `HookThread.chain` 那次崩溃的另一半：形参和赋值被删了，读取留下了。
    """
    problems: "List[str]" = []
    for path in _source_files():
        for cls in _classes(_parse(path)):
            #: 允许用 `__getattr__` 动态兜底的类
            if any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == "__getattr__"
                for item in cls.body
            ):
                continue
            read, assigned = _self_attribute_usage(cls)
            for name, lineno in sorted(read.items(), key=lambda pair: pair[1]):
                if name not in assigned:
                    problems.append(
                        "%s:%d %s 读了 self.%s，但本类从未赋值"
                        % (path.relative_to(ROOT), lineno, cls.name, name)
                    )
    assert not problems, "有属性只读不写：\n  " + "\n  ".join(problems)


def _module_name(path: Path) -> str:
    rel = path.relative_to(PACKAGE).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["netclip"] + parts)


def _shadowed_names(func: ast.AST) -> "Set[str]":
    """函数内部被当成局部变量/参数用掉的名字。

    `Config(...)` 里的 `Config` 如果只是个局部变量，那就跟同名的类没关系，
    不该拿类的签名去要求它。这里把这些名字收集起来，调用点直接跳过。
    """
    names: "Set[str]" = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for group in (
                args.posonlyargs,
                args.args,
                args.kwonlyargs,
                [a for a in (args.vararg, args.kwarg) if a is not None],
            ):
                names.update(a.arg for a in group)
    return names


def test_constructor_calls_only_pass_keywords_the_class_accepts():
    """调用点不得传目标类不接受的**关键字参数**。

    恢复源码时删掉一个形参、却忘了删调用点的那个关键字，就是
    `HookThread.__init__() got an unexpected keyword argument 'chain'`。
    这条检查只看 `netclip` 自己定义的类（外部库的签名不归我们管），
    并且放过带 `**kwargs` 的类 —— 那种本来就什么都能收。
    """
    problems: "List[str]" = []
    for path in _source_files():
        tree = _parse(path)
        try:
            module = importlib.import_module(_module_name(path))
        except Exception:  # pragma: no cover - 导入不了的模块跳过，别让测试本身脆
            continue

        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            shadowed = _shadowed_names(func)
            #: 参数名本身在 walk 里会被算成 Store，但那是"用这个名字"，
            #: 所以只在**不是**被当局部变量赋值的情况下才认为它指向那个类
            for call in (n for n in ast.walk(func) if isinstance(n, ast.Call)):
                if not isinstance(call.func, ast.Name):
                    continue
                name = call.func.id
                if name in shadowed:
                    continue
                target = getattr(module, name, None)
                if not inspect.isclass(target):
                    continue
                if not str(getattr(target, "__module__", "")).startswith("netclip"):
                    continue
                try:
                    params = inspect.signature(target.__init__).parameters
                except (TypeError, ValueError):  # pragma: no cover
                    continue
                if any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
                ):
                    continue
                for keyword in call.keywords:
                    if keyword.arg is None:  # **kwargs 展开，静态看不出来
                        continue
                    if keyword.arg not in params:
                        problems.append(
                            "%s:%d %s(...) 不接受关键字 %r"
                            % (path.relative_to(ROOT), call.lineno, name, keyword.arg)
                        )
    assert not problems, "构造调用传了多余的参数：\n  " + "\n  ".join(problems)


def test_the_checks_actually_look_at_something():
    """两条检查都靠 AST 遍历，遍历写错了会"零问题"地通过。

    这里钉一下扫描范围，避免哪天 `PACKAGE` 指错地方、测试变成永远绿灯。
    """
    files = _source_files()
    assert len(files) > 20, "只扫到 %d 个文件，路径多半不对" % len(files)
    classes = [cls for path in files for cls in _classes(_parse(path))]
    assert len(classes) > 25, "只扫到 %d 个类" % len(classes)


# ------------------------------------------------------- 重名定义 / 未定义的名字
#
# 下面两条针对的是另一类恢复损伤，和上面两条一样是"读起来没问题、跑起来才炸"：
#
#   * **同一个模块里把同名函数定义两遍** —— 后面那个会**静默覆盖**前面那个。
#     真踩过：`clipboard.py` 里 `build_file_clipboard_items` 定义了两次，被覆盖的
#     恰恰是正确的那份（它带着"不要自己造 Shell IDList Array，真机上把资源管理器
#     搞崩过"的结论），生效的是早期那版，于是文件一到就崩。
#   * **引用一个不存在的模块级名字** —— 上一轮那个 NameError
#     （`SHELL_IDLIST_FORMAT`）就是这么来的，而且要等文件真的传过来才炸。
#
# 两条都用**名字**而不是行号来定位，所以它们不关心代码怎么排版。


def _top_level_bindings(tree: ast.Module) -> "Dict[str, List[int]]":
    """模块顶层绑定了哪些名字（函数、类、赋值、注解赋值）。"""
    bound: "Dict[str, List[int]]" = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.setdefault(node.name, []).append(node.lineno)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.setdefault(target.id, []).append(node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.setdefault(node.target.id, []).append(node.lineno)
    return bound


def test_no_module_binds_the_same_top_level_name_twice():
    """模块顶层不许重名 —— 后面的定义会**静默覆盖**前面的，而且没有任何提示。

    这类残留恢复源码时很容易留下：旧版本的一段代码和新版本的一段同时存在，
    谁生效只取决于**谁在后面**，读代码时根本看不出来。
    """
    problems: "List[str]" = []
    for path in _source_files():
        for name, lines in sorted(_top_level_bindings(_parse(path)).items()):
            if len(lines) > 1:
                problems.append(
                    "%s: %s 被定义了 %d 次（行 %s），只有最后一个生效"
                    % (path.relative_to(ROOT), name, len(lines), lines)
                )
    assert not problems, "有重名的顶层定义：\n  " + "\n  ".join(problems)


#: Python 给人准备的隐式全局名，不需要谁去定义。
_IMPLICIT_GLOBALS = frozenset(
    {
        "__file__",
        "__name__",
        "__doc__",
        "__package__",
        "__spec__",
        "__builtins__",
        "__loader__",
        "__cached__",
    }
)


def test_no_module_references_an_undefined_global():
    """函数里引用的模块级名字必须真的存在。

    这条是上一轮那个 `NameError: name 'SHELL_IDLIST_FORMAT' is not defined` 的
    直接守卫 —— 它藏在 `build_file_clipboard_items` 里，平时不跑，等用户真的
    复制一个文件过来才炸。

    用标准库的 `symtable` 做真正的作用域分析（`ast` 单看不行：分不清"局部变量"
    和"全局引用"），所以局部变量、参数、推导式变量都不会误报。实测整套代码
    零误报。
    """
    import builtins
    import symtable

    problems: "List[str]" = []
    for path in _source_files():
        table = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
        defined = {
            symbol.get_name()
            for symbol in table.get_symbols()
            if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
        }

        def walk(scope, where: str) -> None:
            for symbol in scope.get_symbols():
                name = symbol.get_name()
                if not symbol.is_global() or symbol.is_assigned():
                    continue
                if name in defined or name in _IMPLICIT_GLOBALS or hasattr(builtins, name):
                    continue
                problems.append("%s: %s 里引用了未定义的名字 %r" % (path.relative_to(ROOT), where, name))
            for child in scope.get_children():
                walk(child, "%s.%s" % (where, child.get_name()))

        walk(table, path.stem)

    assert not problems, "引用了不存在的模块级名字：\n  " + "\n  ".join(problems)
