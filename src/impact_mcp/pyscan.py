"""Extract imports, definitions and attribute chains from one Python file."""

from __future__ import annotations

import ast
from typing import Any

_DYN_FUNCS = frozenset({"import_module", "__import__"})


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.refs: list[dict[str, Any]] = []
        self.dynamic: list[dict[str, Any]] = []
        self.chains: set[str] = set()
        self.ctx: list[str] = []

    def _flags(self) -> list[str]:
        return sorted(set(self.ctx))

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.refs.append(
                {
                    "k": "import", "m": a.name, "lv": 0, "names": [], "as": a.asname,
                    "ln": node.lineno, "fl": self._flags(),
                }
            )  # fmt: skip

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.refs.append(
            {
                "k": "from", "m": node.module or "", "lv": node.level,
                "names": [[a.name, a.asname] for a in node.names],
                "ln": node.lineno, "fl": self._flags(),
            }
        )  # fmt: skip

    def _with(self, tag: str, nodes: list[ast.stmt]) -> None:
        self.ctx.append(tag)
        for n in nodes:
            self.visit(n)
        self.ctx.pop()

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        if _is_type_checking(node.test):
            self._with("type_only", node.body)
        else:
            self._with("conditional", node.body)
        self._with("conditional", node.orelse)

    def visit_Try(self, node: ast.Try) -> None:
        self._with("conditional", node.body)
        for h in node.handlers:
            self._with("conditional", h.body)
        self._with("conditional", node.orelse)
        self._with("conditional", node.finalbody)

    def visit_TryStar(self, node: Any) -> None:
        self.visit_Try(node)

    def _func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for d in node.decorator_list:
            self.visit(d)
        self._with("lazy", node.body)

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            self.chains.add(".".join([cur.id, *reversed(parts)]))
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
        if name in _DYN_FUNCS:
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and not arg.value.startswith("."):
                self.refs.append(
                    {
                        "k": "import", "m": arg.value, "lv": 0, "names": [], "as": None,
                        "ln": node.lineno, "fl": [*self._flags(), "dynamic"],
                    }
                )  # fmt: skip
            else:
                self.dynamic.append({"ln": node.lineno, "what": f"{name}(<non-literal or relative>)"})
        self.generic_visit(node)


def _str_items(node: ast.expr | None) -> tuple[list[str], bool]:
    """Strings of a list/tuple expression; second item True if something was not a literal."""
    if node is None:
        return [], True
    if isinstance(node, (ast.List, ast.Tuple)):
        out: list[str] = []
        partial = False
        for el in node.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                out.append(el.value)
            else:
                partial = True
        return out, partial
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, pa = _str_items(node.left)
        b, pb = _str_items(node.right)
        return a + b, pa or pb
    return [], True


def _add_all(state: dict[str, Any], node: ast.expr | None) -> None:
    items, partial = _str_items(node)
    state["all"] = (state["all"] or []) + items
    state["partial"] = state["partial"] or partial


def _defs(body: list[ast.stmt], defs: set[str], state: dict[str, Any]) -> None:
    for st in body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.add(st.name)
        elif isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    defs.add(t.id)
                    if t.id == "__all__":
                        _add_all(state, st.value)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    defs.update(e.id for e in t.elts if isinstance(e, ast.Name))
        elif isinstance(st, ast.Import):
            for a in st.names:
                defs.add(a.asname or a.name.split(".")[0])
        elif isinstance(st, ast.ImportFrom):
            for a in st.names:
                if a.name != "*":
                    defs.add(a.asname or a.name)
        elif isinstance(st, ast.If):
            _defs(st.body, defs, state)
            _defs(st.orelse, defs, state)
        elif isinstance(st, ast.Try):
            _defs(st.body, defs, state)
            for h in st.handlers:
                _defs(h.body, defs, state)
            _defs(st.orelse, defs, state)
        elif (
            isinstance(st, ast.Expr)
            and isinstance(st.value, ast.Call)
            and isinstance(st.value.func, ast.Attribute)
            and isinstance(st.value.func.value, ast.Name)
            and st.value.func.value.id == "__all__"
            and st.value.args
        ):  # __all__.extend([...]) / __all__.append("x")
            arg = st.value.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                state["all"] = (state["all"] or []) + [arg.value]
            else:
                _add_all(state, arg)


def _has_main_guard(tree: ast.Module) -> bool:
    for st in tree.body:
        if isinstance(st, ast.If) and isinstance(st.test, ast.Compare):
            left = st.test.left
            if isinstance(left, ast.Name) and left.id == "__name__":
                return True
    return False


def parse_py(data: bytes) -> dict[str, Any]:
    """Parse source bytes. Returns a JSON-able dict; ``err`` is set on failure."""
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
        line = getattr(e, "lineno", None)
        return {"lang": "py", "err": f"{type(e).__name__}: {e}", "err_line": line}
    v = _Visitor()
    v.visit(tree)
    defs: set[str] = set()
    state: dict[str, Any] = {"all": None, "partial": False}
    _defs(tree.body, defs, state)
    for ref in v.refs:
        attrs: dict[str, list[str]] = {}
        if ref["k"] == "import":
            prefix = ref["as"] or ref["m"]
            found = {c[len(prefix) + 1 :].split(".")[0] for c in v.chains if c.startswith(prefix + ".")}
            if found:
                attrs[""] = sorted(found)
        else:
            for name, asname in ref["names"]:
                prefix = asname or name
                found = {c[len(prefix) + 1 :].split(".")[0] for c in v.chains if c.startswith(prefix + ".")}
                if found:
                    attrs[name] = sorted(found)
        ref["attrs"] = attrs
    return {
        "lang": "py",
        "err": None,
        "refs": v.refs,
        "dynamic": v.dynamic,
        "defs": sorted(defs),
        "all": state["all"],
        "all_partial": state["partial"],
        "main": _has_main_guard(tree),
    }
