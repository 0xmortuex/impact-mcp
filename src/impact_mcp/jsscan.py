"""A hand-written JS/TS import and export scanner.

Not a full parser. A tokenizer that understands comments, string literals, template
literals (with nested ``${}``) and regex literals feeds a pattern matcher for the module
syntax. Anything inside comments or strings is never mistaken for an import.

Known limits (reported to users in the README and tool output): JSX text containing an
apostrophe can desync the tokenizer for the rest of that line; regex-vs-division is a
heuristic; ``import`` / ``require`` calls with a non-literal argument are reported as
dynamic, never guessed.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
   |(?P<lc>//[^\n]*)
   |(?P<bc>/\*.*?(?:\*/|\Z))
   |(?P<str>'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")
   |(?P<id>[^\W\d][\w$]*|\$[\w$]*)
   |(?P<num>\d[\w.]*)
   |(?P<p>.)
    """,
    re.X | re.S,
)
_REGEX = re.compile(r"/(?:[^/\\\n\[]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/[A-Za-z]*")
_TPL_PART = re.compile(r"(?:[^`\\$]|\\.|\$(?!\{))*", re.S)
_PRE_REGEX_IDS = frozenset(
    "return typeof instanceof in of new delete void throw case do else yield await".split()
)
_DECL_WORDS = frozenset(
    "function class interface type enum namespace module const let var abstract declare async".split()
)

Token = tuple[str, str | None, int]  # (kind, value, line)


def tokenize(src: str) -> list[Token]:
    toks: list[Token] = []
    pos = 0
    n = len(src)
    line = 1
    braces: list[str] = []
    prev: Token | None = None
    match = _TOKEN.match
    while pos < n:
        m = match(src, pos)
        if m is None:  # cannot happen: (?P<p>.) with DOTALL matches anything
            break
        kind = m.lastgroup
        text = m.group()
        end = m.end()
        if kind == "ws" or kind == "lc" or kind == "bc":
            line += text.count("\n")
            pos = end
            continue
        if kind == "str":
            toks.append(("str", text[1:-1], line))
            line += text.count("\n")
        elif kind == "id":
            toks.append(("id", text, line))
        elif kind == "num":
            toks.append(("num", text, line))
        else:
            if text == "/":
                regex_ok = prev is None or (
                    (prev[0] == "p" and prev[1] not in (")", "]", "}"))
                    or (prev[0] == "id" and prev[1] in _PRE_REGEX_IDS)
                )
                rm = _REGEX.match(src, pos) if regex_ok else None
                if rm is not None:
                    toks.append(("re", None, line))
                    pos = rm.end()
                    prev = toks[-1]
                    continue
            if text == "`":
                body_m = _TPL_PART.match(src, end)
                assert body_m is not None
                body = body_m.group()
                after = body_m.end()
                if src.startswith("${", after):
                    toks.append(("tplx", None, line))
                    braces.append("tpl")
                    line += body.count("\n")
                    pos = after + 2
                else:
                    toks.append(("tpl", body, line))
                    line += body.count("\n")
                    pos = after + 1 if after < n else after
                prev = toks[-1]
                continue
            if text == "{":
                braces.append("b")
            elif text == "}":
                if braces and braces.pop() == "tpl":
                    body_m = _TPL_PART.match(src, end)
                    assert body_m is not None
                    body = body_m.group()
                    after = body_m.end()
                    line += body.count("\n")
                    if src.startswith("${", after):
                        braces.append("tpl")
                        pos = after + 2
                    else:
                        pos = after + 1 if after < n else after
                    toks.append(("p", "tpl-close", line))
                    prev = toks[-1]
                    continue
            toks.append(("p", text, line))
        prev = toks[-1]
        pos = end
    return toks


def _is(t: Token | None, kind: str, val: str | None = None) -> bool:
    return t is not None and t[0] == kind and (val is None or t[1] == val)


def _specifier_list(toks: list[Token], i: int) -> tuple[list[tuple[str, str, bool]], int]:
    """Parse ``{ a, b as c, type d }`` starting at the ``{`` token.

    Returns ([(name, alias, inline_type)], index after the closing brace)."""
    out: list[tuple[str, str, bool]] = []
    i += 1
    n = len(toks)
    while i < n and not _is(toks[i], "p", "}"):
        if _is(toks[i], "p", ","):
            i += 1
            continue
        inline_type = False
        if _is(toks[i], "id", "type") and i + 1 < n and toks[i + 1][0] in ("id", "str") and not _is(toks[i + 1], "id", "as"):
            inline_type = True
            i += 1
        if toks[i][0] in ("id", "str"):
            name = str(toks[i][1])
            alias = name
            i += 1
            if i < n and _is(toks[i], "id", "as") and i + 1 < n:
                alias = str(toks[i + 1][1])
                i += 2
            out.append((name, alias, inline_type))
        else:
            i += 1  # unexpected token: skip rather than loop forever
    return out, i + 1


def parse_js(src: str) -> dict[str, Any]:
    toks = tokenize(src)
    n = len(toks)
    refs: list[dict[str, Any]] = []
    dynamic: list[dict[str, Any]] = []
    exports: list[str] = []
    ns_aliases: list[tuple[str, dict[str, Any], int]] = []  # (alias, ref, decl token index)

    def prev_tok(i: int) -> Token | None:
        return toks[i - 1] if i > 0 else None

    def static_str(t: Token | None) -> str | None:
        if t is not None and t[0] in ("str", "tpl"):
            return t[1]
        return None

    i = 0
    while i < n:
        t = toks[i]
        if t[0] != "id":
            i += 1
            continue
        word = t[1]
        after_dot = _is(prev_tok(i), "p", ".") or (
            i >= 2 and _is(toks[i - 1], "p", ".") and _is(toks[i - 2], "p", "?")
        )
        if after_dot:
            i += 1
            continue

        if word == "import":
            nxt = toks[i + 1] if i + 1 < n else None
            if _is(nxt, "p", "("):
                lit = static_str(toks[i + 2] if i + 2 < n else None)
                close = toks[i + 3] if i + 3 < n else None
                if lit is not None and (_is(close, "p", ")") or _is(close, "p", ",")):
                    refs.append({"k": "dyn", "s": lit, "names": [], "star": False, "fl": [], "ln": t[2]})
                else:
                    dynamic.append({"ln": t[2], "what": "import(<non-literal>)"})
                i += 1
                continue
            if _is(nxt, "p", "."):  # import.meta
                i += 1
                continue
            if nxt is not None and nxt[0] == "str":
                refs.append({"k": "import", "s": nxt[1], "names": [], "star": False, "fl": ["side_effect"], "ln": t[2]})
                i += 2
                continue
            # import clause
            j = i + 1
            type_only = False
            if _is(toks[j] if j < n else None, "id", "type") and j + 1 < n:
                nt = toks[j + 1]
                if (nt[0] == "id" and nt[1] != "from") or _is(nt, "p", "{") or _is(nt, "p", "*"):
                    type_only = True
                    j += 1
            names: list[str] = []
            star = False
            aliases: list[str] = []
            ns_name: str | None = None
            ns_decl = -1
            id_idx = -1
            inline_types: list[bool] = []
            spec: str | None = None
            req_spec: str | None = None
            while j < n:
                tj = toks[j]
                if tj[0] == "id" and tj[1] == "from" and j + 1 < n and toks[j + 1][0] == "str" and (names or star or aliases):
                    spec = str(toks[j + 1][1])
                    j += 2
                    break
                if tj[0] == "id":
                    id_idx = j
                    names.append("default")
                    aliases.append(str(tj[1]))
                    inline_types.append(False)
                    j += 1
                    if _is(toks[j] if j < n else None, "p", "="):  # import x = require("y")
                        if (
                            j + 4 < n
                            and _is(toks[j + 1], "id", "require")
                            and _is(toks[j + 2], "p", "(")
                            and toks[j + 3][0] == "str"
                        ):
                            req_spec = str(toks[j + 3][1])
                            j += 5
                        break
                    continue
                if _is(tj, "p", "*"):
                    star = True
                    if j + 2 < n and _is(toks[j + 1], "id", "as"):
                        ns_name = str(toks[j + 2][1])
                        ns_decl = j + 2
                        j += 3
                    else:
                        j += 1
                    continue
                if _is(tj, "p", "{"):
                    specs, j = _specifier_list(toks, j)
                    for nm, al, it in specs:
                        names.append(nm)
                        aliases.append(al)
                        inline_types.append(it)
                    continue
                if _is(tj, "p", ",") or _is(tj, "p", "}"):
                    j += 1
                    continue
                break
            if req_spec is not None:
                ref: dict[str, Any] = {"k": "require", "s": req_spec, "names": [], "star": False, "fl": ["type_only"] if type_only else [], "ln": t[2]}
                refs.append(ref)
                if aliases:
                    ns_aliases.append((aliases[0], ref, id_idx))
            elif spec is not None:
                only_types = type_only or (bool(inline_types) and all(inline_types) and not star)
                ref = {"k": "import", "s": spec, "names": names, "star": False, "fl": ["type_only"] if only_types else [], "ln": t[2]}
                refs.append(ref)
                if star:
                    # namespace import: attribute accesses are collected below
                    ref["ns"] = True
                    if ns_name is not None:
                        ns_aliases.append((ns_name, ref, ns_decl))
                    else:
                        ref["star"] = True
            i = max(j, i + 1)
            continue

        if word == "export":
            j = i + 1
            nxt = toks[j] if j < n else None
            type_only = False
            if _is(nxt, "id", "type") and j + 1 < n and (_is(toks[j + 1], "p", "{") or _is(toks[j + 1], "p", "*")):
                type_only = True
                j += 1
                nxt = toks[j]
            fl = ["type_only"] if type_only else []
            if _is(nxt, "p", "*"):
                k = j + 1
                alias: str | None = None
                if k + 1 < n and _is(toks[k], "id", "as"):
                    alias = str(toks[k + 1][1])
                    k += 2
                if k + 1 < n and _is(toks[k], "id", "from") and toks[k + 1][0] == "str":
                    refs.append({"k": "export", "s": toks[k + 1][1], "names": [], "star": True, "fl": fl, "ln": t[2]})
                    if alias:
                        exports.append(alias)
                    i = k + 2
                    continue
                i += 1
                continue
            if _is(nxt, "p", "{"):
                specs, k = _specifier_list(toks, j)
                if k + 1 < n and _is(toks[k] if k < n else None, "id", "from") and toks[k + 1][0] == "str":
                    only_types = type_only or (bool(specs) and all(s[2] for s in specs))
                    refs.append(
                        {
                            "k": "export", "s": toks[k + 1][1], "names": [s[0] for s in specs], "star": False,
                            "rename": {s[0]: s[1] for s in specs if s[0] != s[1]},
                            "fl": ["type_only"] if only_types else [], "ln": t[2],
                        }
                    )  # fmt: skip
                    exports.extend(s[1] for s in specs)
                    i = k + 2
                else:
                    exports.extend(s[1] for s in specs)
                    i = k
                continue
            if _is(nxt, "id", "default"):
                exports.append("default")
                i = j + 1
                continue
            # export [declare] [async|abstract|const] function|class|const|... NAME
            k = j
            while k < n and toks[k][0] == "id" and toks[k][1] in _DECL_WORDS:
                kw = toks[k][1]
                k += 1
                if kw in ("function", "class", "interface", "type", "enum", "namespace", "module", "const", "let", "var"):
                    if kw == "const" and k < n and _is(toks[k], "id", "enum"):
                        continue
                    if k < n and _is(toks[k], "p", "*"):
                        k += 1
                    if k < n and toks[k][0] == "id":
                        exports.append(str(toks[k][1]))
                    break
            i = max(k, i + 1)
            continue

        if word == "require":
            prv = prev_tok(i)
            if _is(prv, "id", "function"):
                i += 1
                continue
            if i + 1 < n and _is(toks[i + 1], "p", "("):
                lit = static_str(toks[i + 2] if i + 2 < n else None)
                if lit is not None and i + 3 < n and _is(toks[i + 3], "p", ")"):
                    ref = {"k": "require", "s": lit, "names": [], "star": False, "fl": [], "ln": t[2]}
                    refs.append(ref)
                    # binding forms: const x = require(..) / const {a, b} = require(..) / require(..).foo
                    if i + 5 < n and _is(toks[i + 4], "p", ".") and toks[i + 5][0] == "id":
                        ref["names"].append(str(toks[i + 5][1]))
                    elif _is(prv, "p", "=") and i >= 2:
                        b = toks[i - 2]
                        if b[0] == "id":
                            ns_aliases.append((str(b[1]), ref, i - 2))
                        elif _is(b, "p", "}"):
                            k = i - 3
                            depth = 1
                            while k >= 0 and depth:
                                if _is(toks[k], "p", "}"):
                                    depth += 1
                                elif _is(toks[k], "p", "{"):
                                    depth -= 1
                                k -= 1
                            seg = toks[k + 2 : i - 2]
                            expect_name = True
                            for s in seg:
                                if _is(s, "p", ","):
                                    expect_name = True
                                elif s[0] == "id" and expect_name:
                                    ref["names"].append(str(s[1]))
                                    expect_name = False
                        else:
                            ref["star"] = True
                    else:
                        ref["star"] = True  # result used in some way we do not track
                else:
                    dynamic.append({"ln": t[2], "what": "require(<non-literal>)"})
            i += 1
            continue

        if word == "exports" or word == "module":
            # CommonJS exports: exports.foo = / module.exports.foo = / module.exports = {a, b}
            k = i
            if word == "module" and k + 2 < n and _is(toks[k + 1], "p", ".") and _is(toks[k + 2], "id", "exports"):
                k += 2
            elif word == "module":
                i += 1
                continue
            if k + 2 < n and _is(toks[k + 1], "p", ".") and toks[k + 2][0] == "id" and k + 3 < n and _is(toks[k + 3], "p", "="):
                exports.append(str(toks[k + 2][1]))
            elif word == "module" and k + 2 < n and _is(toks[k + 1], "p", "=") and _is(toks[k + 2], "p", "{"):
                exports.append("module.exports")
            i += 1
            continue
        i += 1

    # namespace / require-binding attribute accesses
    for alias, ref, decl in ns_aliases:
        attrs: set[str] = set()
        opaque = False
        for idx, tk in enumerate(toks):
            if idx == decl or tk[0] != "id" or tk[1] != alias:
                continue
            if _is(prev_tok(idx), "p", "."):
                continue
            nx = toks[idx + 1] if idx + 1 < n else None
            nx2 = toks[idx + 2] if idx + 2 < n else None
            if _is(nx, "p", ".") and nx2 is not None and nx2[0] == "id":
                attrs.add(str(nx2[1]))
            elif _is(nx, "p", "?") and _is(nx2, "p", ".") and idx + 3 < n and toks[idx + 3][0] == "id":
                attrs.add(str(toks[idx + 3][1]))
            elif _is(nx, "p", ":") or _is(nx, "p", "}"):
                continue  # object key / property shorthand ambiguity: ignore
            else:
                opaque = True
        ref["names"] = sorted(set(ref["names"]) | attrs)
        if opaque:
            ref["star"] = True

    return {
        "lang": "js",
        "err": None,
        "refs": refs,
        "dynamic": dynamic,
        "exports": sorted(set(exports)),
    }
