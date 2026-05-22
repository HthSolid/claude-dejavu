#!/usr/bin/env python3
"""
claude-dejavu — code symbol indexer (v0.3.0.1).

Walks a project directory, parses source files, persists every top-level
symbol (function, class, method, type, interface, enum, const) into the
`symbols` table. Tracks per-file mtime+size in `symbol_file_state` so
re-indexing is incremental.

Parser ladder (per language):
  1. tree-sitter (preferred — accurate AST, catches arrow functions,
     default exports, type predicates, generators, etc.)
  2. stdlib ast / regex fallback (so the plugin still works if
     tree-sitter-languages can't be installed; e.g., on platforms
     without pre-built wheels for the bundled grammars)

Tree-sitter coverage (v0.3.0.1):
  - python, typescript, tsx, javascript, go, rust, java, ruby, bash

Pure-Python fallback coverage:
  - python (stdlib ast — full)
  - typescript, javascript (regex — top-level only)

Non-supported extensions are skipped with a recorded parser_error so the
caller can see what's missing. Adding a language = adding one entry to
EXT_TO_LANG, optionally a tree-sitter visitor, optionally a fallback.

Usage:
    from symbol_indexer import index_project, resolve_symbol

    stats = index_project("/path/to/repo", project_slug="my-project", conn=pg_conn)
    # {'files_scanned': 312, 'files_indexed': 8, 'files_skipped': 304,
    #  'symbols_added': 47, 'symbols_removed': 12, 'errors': 0}

    candidates = resolve_symbol("parseUserInput", project_slug="my-project",
                                conn=pg_conn, fuzzy=True, limit=10)
    # [{'name': 'parse_user_input', 'kind': 'function', 'file_path': '...',
    #   'edit_distance': 3, 'trigram_sim': 0.71, ...}, ...]
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# ─── Symbol representation ───────────────────────────────────────────────────


@dataclass
class Symbol:
    name: str
    kind: str                       # function|class|method|variable|type|interface|enum|const
    start_line: int
    end_line: int
    qualified_name: str | None = None
    signature: str | None = None
    parent_name: str | None = None  # for methods, the enclosing class name
    doc: str | None = None


# ─── Python parser (stdlib ast) ──────────────────────────────────────────────


def parse_python(source: str) -> list[Symbol]:
    """Extract module-level + class-nested symbols from Python source."""
    out: list[Symbol] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_py_function(node, parent=None))
        elif isinstance(node, ast.ClassDef):
            cls = Symbol(
                name=node.name,
                kind="class",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                qualified_name=node.name,
                signature=f"class {node.name}",
                doc=ast.get_docstring(node),
            )
            out.append(cls)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(_py_function(child, parent=node.name, kind="method"))
        elif isinstance(node, ast.Assign):
            # Module-level assignments: NAME = ...
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.append(Symbol(
                        name=target.id,
                        kind="variable",
                        start_line=node.lineno,
                        end_line=getattr(node, "end_lineno", node.lineno),
                        qualified_name=target.id,
                    ))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.append(Symbol(
                name=node.target.id,
                kind="variable",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                qualified_name=node.target.id,
            ))
    return out


def _py_function(node, parent: str | None, kind: str = "function") -> Symbol:
    args = [a.arg for a in node.args.args]
    sig = f"{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}({', '.join(args)})"
    qn = f"{parent}.{node.name}" if parent else node.name
    return Symbol(
        name=node.name,
        kind=kind,
        start_line=node.lineno,
        end_line=getattr(node, "end_lineno", node.lineno),
        qualified_name=qn,
        signature=sig,
        parent_name=parent,
        doc=ast.get_docstring(node),
    )


# ─── TypeScript / JavaScript parser (regex) ──────────────────────────────────
# Captures top-level declarations only. Misses arrow-function consts that
# happen to also be top-level — a known limitation, recovered in v0.3.0.1
# via tree-sitter.

# ^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?
_EXPORT = r"(?:export[ \t]+(?:default[ \t]+)?)?"

_TS_PATTERNS = [
    # function declarations: `function foo(args): RetType {`
    (re.compile(rf"^[ \t]*{_EXPORT}(?:async[ \t]+)?function[ \t]+(?P<name>[A-Za-z_$][\w$]*)\s*(?P<sig>\([^)]*\)(?:\s*:\s*[^{{;]+)?)", re.MULTILINE), "function"),
    # class declarations: `class Foo extends Bar {`
    (re.compile(rf"^[ \t]*{_EXPORT}(?:abstract[ \t]+)?class[ \t]+(?P<name>[A-Za-z_$][\w$]*)", re.MULTILINE), "class"),
    # interface declarations: `interface Foo {`
    (re.compile(rf"^[ \t]*{_EXPORT}interface[ \t]+(?P<name>[A-Za-z_$][\w$]*)", re.MULTILINE), "interface"),
    # type aliases: `type Foo = ...`
    (re.compile(rf"^[ \t]*{_EXPORT}type[ \t]+(?P<name>[A-Za-z_$][\w$]*)\s*=", re.MULTILINE), "type"),
    # enums: `enum Foo {`
    (re.compile(rf"^[ \t]*{_EXPORT}(?:const[ \t]+)?enum[ \t]+(?P<name>[A-Za-z_$][\w$]*)", re.MULTILINE), "enum"),
    # const/let/var at top level: `const foo = ...` or `const foo: Type = ...`
    (re.compile(rf"^[ \t]*{_EXPORT}(?P<keyword>const|let|var)[ \t]+(?P<name>[A-Za-z_$][\w$]*)(?:\s*:\s*(?P<typ>[^=;]+))?\s*=", re.MULTILINE), "const"),
]


def parse_typescript(source: str) -> list[Symbol]:
    out: list[Symbol] = []
    seen: set[tuple[str, int]] = set()  # dedupe by (name, line)
    for pat, kind in _TS_PATTERNS:
        for m in pat.finditer(source):
            name = m.group("name")
            start_line = source[: m.start()].count("\n") + 1
            key = (name, start_line)
            if key in seen:
                continue
            seen.add(key)
            sig = None
            if kind == "function":
                sig = f"function {name}{m.group('sig')}".strip()
            elif kind == "class":
                sig = f"class {name}"
            elif kind == "interface":
                sig = f"interface {name}"
            elif kind == "type":
                sig = f"type {name}"
            elif kind == "enum":
                sig = f"enum {name}"
            elif kind == "const":
                kw = m.group("keyword")
                sig = f"{kw} {name}"
            out.append(Symbol(
                name=name,
                kind=kind,
                start_line=start_line,
                end_line=start_line,  # regex doesn't give us end; refined by tree-sitter later
                qualified_name=name,
                signature=sig,
            ))
    return out


# ─── Tree-sitter (preferred parsers when available) ─────────────────────────

try:  # optional dep — fall back to ast/regex if missing
    import warnings as _w
    _w.filterwarnings("ignore", category=FutureWarning, module="tree_sitter")
    from tree_sitter_languages import get_parser as _ts_get_parser
    _TS_PARSERS: dict[str, object] = {}
    for _lang in ("python", "typescript", "tsx", "javascript", "go", "rust",
                   "java", "ruby", "bash", "php", "c_sharp", "kotlin"):
        try:
            _TS_PARSERS[_lang] = _ts_get_parser(_lang)
        except Exception:
            pass
    _HAVE_TS = bool(_TS_PARSERS)
except Exception:
    _TS_PARSERS = {}
    _HAVE_TS = False


def _ts_decode(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _ts_first_named(node, *types: str):
    for c in node.named_children:
        if c.type in types:
            return c
    return None


def _ts_walk(node, callback):
    """Walk every named node in DFS order, calling callback(node, parent_chain)."""
    stack: list[tuple[object, list[object]]] = [(node, [])]
    while stack:
        n, chain = stack.pop()
        callback(n, chain)
        new_chain = chain + [n]
        for c in reversed(n.named_children):
            stack.append((c, new_chain))


def parse_python_ts(source: str) -> list[Symbol]:
    """Tree-sitter Python parser. Equivalent coverage to ast-based parser
    but catches more edge cases and gives better end_line for nested defs."""
    parser = _TS_PARSERS.get("python")
    if not parser:
        return parse_python(source)
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    out: list[Symbol] = []

    def emit(node, chain):
        # Skip nested non-class containers — we want top-level + class methods only.
        # `chain` contains the path from root (module) to this node's parent.
        in_class = any(p.type == "class_definition" for p in chain)
        # Anything nested inside a function definition is local — skip.
        if any(p.type in ("function_definition", "async_function_definition") for p in chain):
            return

        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if not name_node:
                return
            name = _ts_decode(name_node, src_b)
            doc = _extract_python_docstring(node, src_b)
            out.append(Symbol(
                name=name, kind="class",
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                qualified_name=name,
                signature=f"class {name}",
                doc=doc,
            ))
        elif node.type in ("function_definition", "async_function_definition"):
            name_node = node.child_by_field_name("name")
            if not name_node:
                return
            name = _ts_decode(name_node, src_b)
            params_node = node.child_by_field_name("parameters")
            params_text = _ts_decode(params_node, src_b) if params_node else "()"
            is_async = node.type == "async_function_definition"
            kind = "method" if in_class else "function"
            parent_name = None
            if in_class:
                # nearest enclosing class
                for p in reversed(chain):
                    if p.type == "class_definition":
                        nn = p.child_by_field_name("name")
                        if nn:
                            parent_name = _ts_decode(nn, src_b)
                        break
            qn = f"{parent_name}.{name}" if parent_name else name
            sig = f"{'async ' if is_async else ''}def {name}{params_text}"
            out.append(Symbol(
                name=name, kind=kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                qualified_name=qn,
                signature=sig,
                parent_name=parent_name,
                doc=_extract_python_docstring(node, src_b),
            ))
        elif node.type == "assignment" and not chain:
            # Module-level assignment — capture LHS names.
            return  # handled at top-level walk below
    # Walk only the module's direct children for assignments + decorated defs;
    # but use _ts_walk for nested class/def discovery.
    for child in tree.root_node.named_children:
        if child.type == "decorated_definition":
            target = child.child_by_field_name("definition")
            if target is not None:
                _ts_walk(target, emit)
        elif child.type in ("class_definition", "function_definition", "async_function_definition"):
            _ts_walk(child, emit)
        elif child.type == "expression_statement":
            # Module-level docstring or assignment-as-expression
            inner = child.named_children[0] if child.named_children else None
            if inner and inner.type == "assignment":
                _capture_python_assignment(inner, src_b, out)
        elif child.type == "assignment":
            _capture_python_assignment(child, src_b, out)
    return out


def _extract_python_docstring(node, src_b: bytes) -> str | None:
    body = node.child_by_field_name("body")
    if not body or not body.named_children:
        return None
    first = body.named_children[0]
    if first.type == "expression_statement" and first.named_children:
        s = first.named_children[0]
        if s.type == "string":
            txt = _ts_decode(s, src_b).strip()
            for q in ('"""', "'''", '"', "'"):
                if txt.startswith(q) and txt.endswith(q):
                    return txt[len(q):-len(q)].strip() or None
    return None


def _capture_python_assignment(node, src_b: bytes, out: list[Symbol]):
    left = node.child_by_field_name("left")
    if not left:
        return
    if left.type == "identifier":
        name = _ts_decode(left, src_b)
        out.append(Symbol(
            name=name, kind="variable",
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            qualified_name=name,
        ))


def _ts_extract_typescript(source: str, lang: str) -> list[Symbol]:
    """Tree-sitter TS/TSX/JS extractor.

    Captures top-level + exported declarations:
      function_declaration       -> function
      generator_function_decl..  -> function
      class_declaration          -> class (with methods captured as 'method')
      interface_declaration      -> interface (TS only)
      type_alias_declaration     -> type      (TS only)
      enum_declaration           -> enum      (TS only)
      lexical_declaration        -> const | function (if value is arrow/function)
      variable_declaration
      method_definition          -> method (inside class)
    """
    parser_key = lang if lang in ("typescript", "tsx", "javascript") else "typescript"
    parser = _TS_PARSERS.get(parser_key)
    if not parser:
        return parse_typescript(source)  # regex fallback
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    out: list[Symbol] = []

    def push(name: str, kind: str, node, *, parent: str | None = None,
             signature: str | None = None):
        out.append(Symbol(
            name=name,
            kind=kind,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            qualified_name=f"{parent}.{name}" if parent else name,
            signature=signature or f"{kind} {name}",
            parent_name=parent,
        ))

    def handle_decl(node, parent: str | None = None):
        t = node.type
        if t == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                params = node.child_by_field_name("parameters")
                ret = node.child_by_field_name("return_type")
                ptxt = _ts_decode(params, src_b) if params else "()"
                rtxt = f": {_ts_decode(ret, src_b).lstrip(': ')}" if ret else ""
                name = _ts_decode(name_node, src_b)
                push(name, "function", node, parent=parent,
                     signature=f"function {name}{ptxt}{rtxt}")
        elif t == "generator_function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                push(name, "function", node, parent=parent, signature=f"function* {name}")
        elif t == "class_declaration":
            name_node = node.child_by_field_name("name")
            if not name_node:
                return
            cname = _ts_decode(name_node, src_b)
            push(cname, "class", node, parent=parent, signature=f"class {cname}")
            body = node.child_by_field_name("body")
            if body:
                for m in body.named_children:
                    if m.type == "method_definition":
                        mname_node = m.child_by_field_name("name")
                        if mname_node:
                            mname = _ts_decode(mname_node, src_b)
                            params = m.child_by_field_name("parameters")
                            ptxt = _ts_decode(params, src_b) if params else "()"
                            push(mname, "method", m, parent=cname,
                                 signature=f"{cname}.{mname}{ptxt}")
        elif t == "interface_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                push(name, "interface", node, parent=parent, signature=f"interface {name}")
        elif t == "type_alias_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                push(name, "type", node, parent=parent, signature=f"type {name}")
        elif t == "enum_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                push(name, "enum", node, parent=parent, signature=f"enum {name}")
        elif t == "lexical_declaration" or t == "variable_declaration":
            # Possibly: const foo = (x) => ..., const Bar = class {...}, const X = 7
            for v in node.named_children:
                if v.type != "variable_declarator":
                    continue
                name_node = v.child_by_field_name("name")
                if not name_node:
                    continue
                name = _ts_decode(name_node, src_b)
                value = v.child_by_field_name("value")
                if value and value.type in ("arrow_function", "function", "function_expression",
                                            "generator_function"):
                    params = value.child_by_field_name("parameters")
                    ret = value.child_by_field_name("return_type")
                    ptxt = _ts_decode(params, src_b) if params else "()"
                    rtxt = f": {_ts_decode(ret, src_b).lstrip(': ')}" if ret else ""
                    push(name, "function", v, parent=parent, signature=f"{name} = {ptxt}{rtxt} => …")
                elif value and value.type in ("class", "class_expression"):
                    push(name, "class", v, parent=parent, signature=f"{name} = class …")
                else:
                    push(name, "const", v, parent=parent, signature=f"const {name}")

    for child in tree.root_node.named_children:
        if child.type == "export_statement":
            decl = child.child_by_field_name("declaration")
            if decl is not None:
                handle_decl(decl)
        else:
            handle_decl(child)
    return out


def _ts_extract_go(source: str) -> list[Symbol]:
    parser = _TS_PARSERS.get("go")
    if not parser:
        return []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    out: list[Symbol] = []
    for n in tree.root_node.named_children:
        t = n.type
        if t == "function_declaration":
            name_node = n.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                params = n.child_by_field_name("parameters")
                ptxt = _ts_decode(params, src_b) if params else "()"
                out.append(Symbol(name=name, kind="function",
                                  start_line=n.start_point[0]+1, end_line=n.end_point[0]+1,
                                  qualified_name=name, signature=f"func {name}{ptxt}"))
        elif t == "method_declaration":
            name_node = n.child_by_field_name("name")
            recv = n.child_by_field_name("receiver")
            if name_node:
                name = _ts_decode(name_node, src_b)
                rtxt = _ts_decode(recv, src_b) if recv else ""
                out.append(Symbol(name=name, kind="method",
                                  start_line=n.start_point[0]+1, end_line=n.end_point[0]+1,
                                  qualified_name=f"{rtxt.strip('()')}.{name}" if rtxt else name,
                                  signature=f"func {rtxt} {name}".strip()))
        elif t == "type_declaration":
            for spec in n.named_children:
                if spec.type == "type_spec":
                    name_node = spec.child_by_field_name("name")
                    typ_node = spec.child_by_field_name("type")
                    if name_node:
                        name = _ts_decode(name_node, src_b)
                        kind = "interface" if (typ_node and typ_node.type == "interface_type") else "type"
                        if typ_node and typ_node.type == "struct_type":
                            kind = "class"
                        out.append(Symbol(name=name, kind=kind,
                                          start_line=spec.start_point[0]+1, end_line=spec.end_point[0]+1,
                                          qualified_name=name, signature=f"type {name}"))
        elif t in ("var_declaration", "const_declaration"):
            for spec in n.named_children:
                if spec.type in ("var_spec", "const_spec"):
                    for c in spec.named_children:
                        if c.type == "identifier":
                            name = _ts_decode(c, src_b)
                            out.append(Symbol(name=name,
                                              kind="const" if t == "const_declaration" else "variable",
                                              start_line=spec.start_point[0]+1,
                                              end_line=spec.end_point[0]+1,
                                              qualified_name=name, signature=f"{t.split('_')[0]} {name}"))
    return out


def _ts_extract_rust(source: str) -> list[Symbol]:
    parser = _TS_PARSERS.get("rust")
    if not parser:
        return []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    out: list[Symbol] = []

    def emit_item(n, parent: str | None = None):
        t = n.type
        if t == "function_item":
            name_node = n.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                params = n.child_by_field_name("parameters")
                ptxt = _ts_decode(params, src_b) if params else "()"
                kind = "method" if parent else "function"
                qn = f"{parent}::{name}" if parent else name
                out.append(Symbol(name=name, kind=kind,
                                  start_line=n.start_point[0]+1, end_line=n.end_point[0]+1,
                                  qualified_name=qn, parent_name=parent,
                                  signature=f"fn {name}{ptxt}"))
        elif t == "struct_item":
            name_node = n.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                out.append(Symbol(name=name, kind="class",
                                  start_line=n.start_point[0]+1, end_line=n.end_point[0]+1,
                                  qualified_name=name, signature=f"struct {name}"))
        elif t == "enum_item":
            name_node = n.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                out.append(Symbol(name=name, kind="enum",
                                  start_line=n.start_point[0]+1, end_line=n.end_point[0]+1,
                                  qualified_name=name, signature=f"enum {name}"))
        elif t == "trait_item":
            name_node = n.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                out.append(Symbol(name=name, kind="interface",
                                  start_line=n.start_point[0]+1, end_line=n.end_point[0]+1,
                                  qualified_name=name, signature=f"trait {name}"))
        elif t == "type_item":
            name_node = n.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                out.append(Symbol(name=name, kind="type",
                                  start_line=n.start_point[0]+1, end_line=n.end_point[0]+1,
                                  qualified_name=name, signature=f"type {name}"))
        elif t in ("const_item", "static_item"):
            name_node = n.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                out.append(Symbol(name=name, kind="const",
                                  start_line=n.start_point[0]+1, end_line=n.end_point[0]+1,
                                  qualified_name=name,
                                  signature=f"{'const' if t == 'const_item' else 'static'} {name}"))
        elif t == "impl_item":
            type_node = n.child_by_field_name("type")
            type_name = _ts_decode(type_node, src_b) if type_node else None
            body = n.child_by_field_name("body")
            if body:
                for child in body.named_children:
                    if child.type == "function_item":
                        emit_item(child, parent=type_name)

    for child in tree.root_node.named_children:
        emit_item(child)
    return out


def _ts_extract_ruby(source: str) -> list[Symbol]:
    parser = _TS_PARSERS.get("ruby")
    if not parser:
        return []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    out: list[Symbol] = []

    def emit(node, parent: str | None = None):
        for c in node.named_children:
            t = c.type
            if t == "class":
                name_node = c.child_by_field_name("name")
                if name_node:
                    cname = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=cname, kind="class",
                                      start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                      qualified_name=cname, signature=f"class {cname}"))
                    body = c.child_by_field_name("body")
                    if body:
                        emit(body, parent=cname)
            elif t == "module":
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind="class",  # treat ruby module as class-like
                                      start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                      qualified_name=name, signature=f"module {name}"))
                    body = c.child_by_field_name("body")
                    if body:
                        emit(body, parent=name)
            elif t == "method":
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    params = c.child_by_field_name("parameters")
                    ptxt = _ts_decode(params, src_b) if params else "()"
                    kind = "method" if parent else "function"
                    qn = f"{parent}#{name}" if parent else name
                    out.append(Symbol(name=name, kind=kind,
                                      start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                      qualified_name=qn, parent_name=parent,
                                      signature=f"def {name}{ptxt}"))
            elif t == "singleton_method":
                # def self.foo or def Class.foo  — class method
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind="method",
                                      start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                      qualified_name=f"{parent}.{name}" if parent else name,
                                      parent_name=parent,
                                      signature=f"def self.{name}"))
            elif t == "assignment" and parent is None:
                # Top-level constant or variable: `MAX = 10`, `FOO = bar`
                left = c.child_by_field_name("left")
                if left and left.type in ("identifier", "constant"):
                    name = _ts_decode(left, src_b)
                    kind = "const" if name and name[0].isupper() else "variable"
                    out.append(Symbol(name=name, kind=kind,
                                      start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                      qualified_name=name, signature=f"{name} = …"))

    emit(tree.root_node)
    return out


def _ts_extract_bash(source: str) -> list[Symbol]:
    parser = _TS_PARSERS.get("bash")
    if not parser:
        return []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    out: list[Symbol] = []

    for c in tree.root_node.named_children:
        t = c.type
        if t == "function_definition":
            # bash supports both `name() { ... }` and `function name { ... }` —
            # tree-sitter exposes the name as a child node named "name" or as
            # the first child word.
            name_node = c.child_by_field_name("name")
            if not name_node:
                # Walk children for the first word/concatenation node
                for cc in c.named_children:
                    if cc.type in ("word", "concatenation"):
                        name_node = cc
                        break
            if name_node:
                name = _ts_decode(name_node, src_b)
                out.append(Symbol(name=name, kind="function",
                                  start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                  qualified_name=name, signature=f"{name}()"))
        elif t == "variable_assignment":
            # Top-level: `FOO=bar`, `MY_VAR="..."`
            name_node = c.child_by_field_name("name")
            if name_node:
                name = _ts_decode(name_node, src_b)
                # ALL_CAPS -> const, lowercase -> variable (bash convention)
                kind = "const" if name and name.isupper() else "variable"
                out.append(Symbol(name=name, kind=kind,
                                  start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                  qualified_name=name, signature=f"{name}=…"))
    return out


def _ts_extract_java(source: str) -> list[Symbol]:
    parser = _TS_PARSERS.get("java")
    if not parser:
        return []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    out: list[Symbol] = []

    def visit(node, parent: str | None = None):
        for c in node.named_children:
            t = c.type
            if t == "class_declaration":
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind="class",
                                      start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                      qualified_name=name, signature=f"class {name}"))
                    body = c.child_by_field_name("body")
                    if body:
                        visit(body, parent=name)
            elif t == "interface_declaration":
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind="interface",
                                      start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                      qualified_name=name, signature=f"interface {name}"))
            elif t == "enum_declaration":
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind="enum",
                                      start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                      qualified_name=name, signature=f"enum {name}"))
            elif t == "method_declaration":
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    params = c.child_by_field_name("parameters")
                    ptxt = _ts_decode(params, src_b) if params else "()"
                    out.append(Symbol(name=name, kind="method",
                                      start_line=c.start_point[0]+1, end_line=c.end_point[0]+1,
                                      qualified_name=f"{parent}.{name}" if parent else name,
                                      parent_name=parent,
                                      signature=f"{name}{ptxt}"))

    visit(tree.root_node)
    return out


# ─── v0.3.15: PHP / C# / Kotlin ──────────────────────────────────────────


def _ts_extract_php(source: str) -> list[Symbol]:
    parser = _TS_PARSERS.get("php")
    if not parser:
        return []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    out: list[Symbol] = []

    def visit(node, parent: str | None = None):
        for c in node.named_children:
            t = c.type
            if t in ("class_declaration", "interface_declaration",
                      "trait_declaration", "enum_declaration"):
                kind_map = {"class_declaration": "class",
                             "interface_declaration": "interface",
                             "trait_declaration": "type",
                             "enum_declaration": "enum"}
                kind = kind_map[t]
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind=kind,
                                      start_line=c.start_point[0]+1,
                                      end_line=c.end_point[0]+1,
                                      qualified_name=name,
                                      signature=f"{kind} {name}"))
                    body = c.child_by_field_name("body")
                    if body:
                        visit(body, parent=name)
            elif t == "method_declaration":
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind="method",
                                      start_line=c.start_point[0]+1,
                                      end_line=c.end_point[0]+1,
                                      qualified_name=(f"{parent}.{name}"
                                                       if parent else name),
                                      parent_name=parent,
                                      signature=f"function {name}(…)"))
            elif t == "function_definition":
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind="function",
                                      start_line=c.start_point[0]+1,
                                      end_line=c.end_point[0]+1,
                                      qualified_name=name,
                                      signature=f"function {name}(…)"))
            else:
                visit(c, parent=parent)

    visit(tree.root_node)
    return out


def _ts_extract_csharp(source: str) -> list[Symbol]:
    parser = _TS_PARSERS.get("c_sharp")
    if not parser:
        return []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    out: list[Symbol] = []

    def visit(node, parent: str | None = None):
        for c in node.named_children:
            t = c.type
            if t in ("class_declaration", "interface_declaration",
                      "struct_declaration", "record_declaration",
                      "enum_declaration"):
                kind_map = {"class_declaration": "class",
                             "interface_declaration": "interface",
                             "struct_declaration": "class",
                             "record_declaration": "class",
                             "enum_declaration": "enum"}
                kind = kind_map[t]
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind=kind,
                                      start_line=c.start_point[0]+1,
                                      end_line=c.end_point[0]+1,
                                      qualified_name=name,
                                      signature=f"{kind} {name}"))
                    body = c.child_by_field_name("body")
                    if body:
                        visit(body, parent=name)
            elif t == "method_declaration":
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind="method",
                                      start_line=c.start_point[0]+1,
                                      end_line=c.end_point[0]+1,
                                      qualified_name=(f"{parent}.{name}"
                                                       if parent else name),
                                      parent_name=parent,
                                      signature=f"{name}(…)"))
            elif t == "property_declaration":
                name_node = c.child_by_field_name("name")
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind="variable",
                                      start_line=c.start_point[0]+1,
                                      end_line=c.end_point[0]+1,
                                      qualified_name=(f"{parent}.{name}"
                                                       if parent else name),
                                      parent_name=parent))
            else:
                visit(c, parent=parent)

    visit(tree.root_node)
    return out


def _ts_extract_kotlin(source: str) -> list[Symbol]:
    parser = _TS_PARSERS.get("kotlin")
    if not parser:
        return []
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    out: list[Symbol] = []

    def visit(node, parent: str | None = None):
        for c in node.named_children:
            t = c.type
            if t in ("class_declaration", "object_declaration"):
                name_node = None
                for nc in c.named_children:
                    if nc.type in ("type_identifier", "simple_identifier"):
                        name_node = nc
                        break
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    out.append(Symbol(name=name, kind="class",
                                      start_line=c.start_point[0]+1,
                                      end_line=c.end_point[0]+1,
                                      qualified_name=name,
                                      signature=f"class {name}"))
                    visit(c, parent=name)
            elif t == "function_declaration":
                name_node = None
                for nc in c.named_children:
                    if nc.type == "simple_identifier":
                        name_node = nc
                        break
                if name_node:
                    name = _ts_decode(name_node, src_b)
                    kind = "method" if parent else "function"
                    out.append(Symbol(name=name, kind=kind,
                                      start_line=c.start_point[0]+1,
                                      end_line=c.end_point[0]+1,
                                      qualified_name=(f"{parent}.{name}"
                                                       if parent else name),
                                      parent_name=parent,
                                      signature=f"fun {name}(…)"))
            elif t == "property_declaration":
                for nc in c.named_children:
                    if nc.type == "variable_declaration":
                        for nnc in nc.named_children:
                            if nnc.type == "simple_identifier":
                                name = _ts_decode(nnc, src_b)
                                out.append(Symbol(name=name, kind="variable",
                                                  start_line=c.start_point[0]+1,
                                                  end_line=c.end_point[0]+1,
                                                  qualified_name=(f"{parent}.{name}"
                                                                    if parent else name),
                                                  parent_name=parent))
                                break
            else:
                visit(c, parent=parent)

    visit(tree.root_node)
    return out


# ─── Language registry ───────────────────────────────────────────────────────

EXT_TO_LANG: dict[str, str] = {
    ".py":   "python",
    ".pyi":  "python",
    ".ts":   "typescript",
    ".tsx":  "typescript",   # parsed with tsx grammar but stored as typescript
    ".js":   "javascript",
    ".jsx":  "javascript",
    ".mjs":  "javascript",
    ".cjs":  "javascript",
    ".go":   "go",
    ".rs":   "rust",
    ".java": "java",
    ".rb":   "ruby",
    ".sh":   "bash",
    ".bash": "bash",
    ".php":  "php",
    ".cs":   "c_sharp",
    ".kt":   "kotlin",
    ".kts":  "kotlin",
    # ─── v0.4.0: prose grounding (no symbol extraction) ─────────────
    ".md":     "markdown",
    ".markdown": "markdown",
    ".mdx":   "markdown",
    ".txt":   "text",
    ".rst":   "rst",
    ".org":   "org",
    ".tex":   "latex",
}

# When EXT_TO_LANG maps .tsx -> typescript, we still need to parse with the tsx
# grammar (which understands JSX). This map drives parser selection and
# overrides EXT_TO_LANG for parser routing only.
EXT_TO_PARSER_LANG: dict[str, str] = {
    ".tsx": "tsx",
    ".jsx": "javascript",
}


def _build_lang_parsers() -> dict[str, Callable[[str], list[Symbol]]]:
    """Wire each language to the best-available parser at import time.
    If tree-sitter is installed, we get the accurate AST path. Otherwise
    fall back to ast/regex. Languages with no fallback are simply absent
    from the dict — those files will be skipped with parser_error="no_parser".
    """
    parsers: dict[str, Callable[[str], list[Symbol]]] = {}
    if _HAVE_TS:
        if "python" in _TS_PARSERS:
            parsers["python"] = parse_python_ts
        if "typescript" in _TS_PARSERS:
            parsers["typescript"] = lambda s: _ts_extract_typescript(s, "typescript")
        if "tsx" in _TS_PARSERS:
            parsers["tsx"] = lambda s: _ts_extract_typescript(s, "tsx")
        elif "typescript" in _TS_PARSERS:
            parsers["tsx"] = lambda s: _ts_extract_typescript(s, "typescript")
        if "javascript" in _TS_PARSERS:
            parsers["javascript"] = lambda s: _ts_extract_typescript(s, "javascript")
        if "go" in _TS_PARSERS:
            parsers["go"] = _ts_extract_go
        if "rust" in _TS_PARSERS:
            parsers["rust"] = _ts_extract_rust
        if "java" in _TS_PARSERS:
            parsers["java"] = _ts_extract_java
        if "ruby" in _TS_PARSERS:
            parsers["ruby"] = _ts_extract_ruby
        if "bash" in _TS_PARSERS:
            parsers["bash"] = _ts_extract_bash
        if "php" in _TS_PARSERS:
            parsers["php"] = _ts_extract_php
        if "c_sharp" in _TS_PARSERS:
            parsers["c_sharp"] = _ts_extract_csharp
        if "kotlin" in _TS_PARSERS:
            parsers["kotlin"] = _ts_extract_kotlin
    # Fallback for python / ts / js if tree-sitter missing
    parsers.setdefault("python", parse_python)
    parsers.setdefault("typescript", parse_typescript)
    parsers.setdefault("javascript", parse_typescript)
    parsers.setdefault("tsx", parse_typescript)
    return parsers


LANG_PARSERS: dict[str, Callable[[str], list[Symbol]]] = _build_lang_parsers()


def parser_backends() -> dict[str, str]:
    """Diagnostic — which parser backend each language is using."""
    out: dict[str, str] = {}
    fn_to_name = {
        parse_python: "ast (stdlib)",
        parse_python_ts: "tree-sitter",
        parse_typescript: "regex",
    }
    for lang, fn in LANG_PARSERS.items():
        out[lang] = fn_to_name.get(fn, "tree-sitter")
    return out

# Directories we never descend into.
SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "dist", "build", "out", ".next", ".nuxt",
    "__pycache__", ".venv", "venv", ".env", ".mypy_cache", ".pytest_cache",
    ".tox", "target", "bin", "obj", ".gradle", ".idea", ".vscode",
}

# Skip files larger than this (tree-sitter / regex on huge minified blobs is wasted CPU).
MAX_FILE_BYTES = 1_000_000


# ─── Indexing pipeline ───────────────────────────────────────────────────────


def _iter_source_files(root: Path, flt=None) -> Iterable[Path]:
    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # in-place mutation skips recursion into pruned dirs
        dirnames[:] = [d for d in dirnames
                        if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in EXT_TO_LANG:
                continue
            full = Path(dirpath) / fn
            if flt.should_skip(full, is_dir=False):
                continue
            yield full


def _file_state(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def index_file(
    file_path: str,
    project_slug: str,
    conn,
    *,
    force: bool = False,
) -> dict:
    """Index a single file. Returns {'symbols': N, 'changed': bool, 'error': str|None}."""
    p = Path(file_path)
    if not p.exists():
        # File deleted — purge any rows for it.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM symbols WHERE file_path = %s", (file_path,))
            cur.execute("DELETE FROM symbol_file_state WHERE file_path = %s", (file_path,))
        return {"symbols": 0, "changed": True, "error": None, "deleted": True}

    ext = p.suffix.lower()
    lang = EXT_TO_LANG.get(ext)
    if not lang:
        return {"symbols": 0, "changed": False, "error": "unsupported_extension"}
    parser_lang = EXT_TO_PARSER_LANG.get(ext, lang)

    try:
        mtime_ns, size_bytes = _file_state(p)
    except OSError as e:
        return {"symbols": 0, "changed": False, "error": f"stat_error: {e}"}

    if size_bytes > MAX_FILE_BYTES:
        return {"symbols": 0, "changed": False, "error": "file_too_large"}

    # Skip if mtime+size unchanged and not forced.
    if not force:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mtime_ns, size_bytes FROM symbol_file_state WHERE file_path = %s",
                (file_path,),
            )
            row = cur.fetchone()
            if row and row[0] == mtime_ns and row[1] == size_bytes:
                return {"symbols": 0, "changed": False, "error": None}

    try:
        source = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"symbols": 0, "changed": False, "error": f"read_error: {e}"}

    sha = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()
    parser = LANG_PARSERS.get(parser_lang)
    if parser is None:
        # Language listed but no parser yet (e.g. tree-sitter not installed): record state.
        _upsert_file_state(conn, file_path, project_slug, lang, mtime_ns, size_bytes, sha, 0,
                           f"no_parser_for_{parser_lang}")
        return {"symbols": 0, "changed": True, "error": f"no_parser_for_{parser_lang}"}

    parser_error = None
    try:
        symbols = parser(source)
    except Exception as e:  # parser must never crash the whole indexer
        symbols = []
        parser_error = f"parse_error: {type(e).__name__}: {e}"

    # Replace symbol rows for this file atomically (avoid leaving stale rows).
    with conn.cursor() as cur:
        cur.execute("DELETE FROM symbols WHERE file_path = %s", (file_path,))
        # Two passes: parents first (classes), then children (methods) so we can resolve parent_symbol_id.
        parent_id_by_name: dict[str, int] = {}
        for s in symbols:
            if s.parent_name:
                continue  # second pass
            cur.execute(
                """
                INSERT INTO symbols (project_slug, file_path, language, kind, name,
                                     qualified_name, signature, start_line, end_line, doc)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (project_slug, file_path, lang, s.kind, s.name, s.qualified_name,
                 s.signature, s.start_line, s.end_line, s.doc),
            )
            new_id = cur.fetchone()[0]
            if s.kind == "class":
                parent_id_by_name[s.name] = new_id
        for s in symbols:
            if not s.parent_name:
                continue
            cur.execute(
                """
                INSERT INTO symbols (project_slug, file_path, language, kind, name,
                                     qualified_name, signature, start_line, end_line,
                                     parent_symbol_id, doc)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (project_slug, file_path, lang, s.kind, s.name, s.qualified_name,
                 s.signature, s.start_line, s.end_line,
                 parent_id_by_name.get(s.parent_name), s.doc),
            )
        _upsert_file_state(conn, file_path, project_slug, lang, mtime_ns, size_bytes, sha,
                           len(symbols), parser_error)

    return {"symbols": len(symbols), "changed": True, "error": parser_error}


def _upsert_file_state(conn, file_path, project_slug, lang, mtime_ns, size_bytes, sha, count, err):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO symbol_file_state
              (file_path, project_slug, language, mtime_ns, size_bytes, sha256,
               symbol_count, parser_error, indexed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (file_path) DO UPDATE SET
              project_slug = EXCLUDED.project_slug,
              language     = EXCLUDED.language,
              mtime_ns     = EXCLUDED.mtime_ns,
              size_bytes   = EXCLUDED.size_bytes,
              sha256       = EXCLUDED.sha256,
              symbol_count = EXCLUDED.symbol_count,
              parser_error = EXCLUDED.parser_error,
              indexed_at   = now()
            """,
            (file_path, project_slug, lang, mtime_ns, size_bytes, sha, count, err),
        )


def index_project(
    project_dir: str,
    project_slug: str,
    conn,
    *,
    force: bool = False,
    flt=None,
) -> dict:
    """Walk a project directory and index all source files. Incremental by default."""
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"error": f"not_a_directory: {root}"}

    stats = {
        "files_scanned": 0,
        "files_indexed": 0,
        "files_skipped": 0,
        "symbols_added": 0,
        "errors": 0,
    }
    seen_files: set[str] = set()

    for path in _iter_source_files(root, flt=flt):
        stats["files_scanned"] += 1
        seen_files.add(str(path))
        result = index_file(str(path), project_slug, conn, force=force)
        if result["error"] and result["error"] != "unsupported_extension":
            stats["errors"] += 1
        if result["changed"]:
            stats["files_indexed"] += 1
            stats["symbols_added"] += result["symbols"]
        else:
            stats["files_skipped"] += 1

    # Purge symbol rows for files that no longer exist (deleted between runs).
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT file_path FROM symbol_file_state WHERE project_slug = %s
            """,
            (project_slug,),
        )
        known = {r[0] for r in cur.fetchall()}
    stale = known - seen_files
    for fp in stale:
        if Path(fp).exists():
            continue  # still on disk, just outside our walk root (e.g. moved scope)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM symbols WHERE file_path = %s", (fp,))
            cur.execute("DELETE FROM symbol_file_state WHERE file_path = %s", (fp,))
        stats.setdefault("files_purged", 0)
        stats["files_purged"] += 1

    return stats


# ─── Resolution / lookup ─────────────────────────────────────────────────────


# ─── Resolver tuning constants (informed by internal dogfood, n=21385) ──────
#
# DEFAULT_TRIGRAM_THRESHOLD: pg_trgm similarity_threshold. At 0.3 (the v0.3.0
# default) 75% of obvious hallucinations matched something. At 0.4 zero of
# them did, while real-call recall stayed at 100%. Tuned upward.
#
# CANDIDATE_FETCH_LIMIT: how many candidates to pull from PG before Python
# re-ranking. 50 covers all realistic ambiguity for a 21k-symbol corpus
# while keeping the SQL fast (the pg_trgm GIN bitmap-OR scan returns 5-200
# rows for typical queries).
#
# RERANK_*: Python-side re-rank weights. The pg_trgm score alone doesn't
# punish "parseUser -> parser" (sim=0.70 because trigrams of 'parser' are
# a subset of 'parseUser') or "RuleService -> pageRulesService" (long
# substring win). The re-ranker adds substring/prefix bonuses and a length-
# ratio factor.
DEFAULT_TRIGRAM_THRESHOLD = 0.4
CANDIDATE_FETCH_LIMIT = 50
RERANK_PREFIX_BONUS = 0.15          # query is a prefix of candidate name
RERANK_SUBSTRING_BONUS = 0.08       # query appears anywhere in name (case-insens)
RERANK_EXACT_LOWER_BONUS = 0.25     # name.lower() == query.lower()
RERANK_LENGTH_EXP = 0.5             # length-ratio dampening exponent


def _rerank_score(query: str, name: str, qualified_name: str | None,
                  trigram_sim: float, edit_distance: int) -> float:
    """Combine pg_trgm similarity with length-aware + substring + edit-distance
    heuristics.

    Tuning informed by internal dogfood failures:
      * `parseUser` -> `parser` (sim=0.70 because trigrams of 'parser' are
        subset of 'parseUser').
      * `RuleService` -> `pageRulesService` (long substring win).
      * `WorkflowGrpah` -> `Workflow` instead of `WorkflowGraph`: pure trigram
        favored the shorter prefix-match. Fix: give a strong bonus when edit
        distance is very small (1 or 2), so close-typos beat partial-overlaps.

    Heuristics:
      - exact lowercase match (case-only diff): almost always the answer.
      - low edit distance (ed ≤ 2): strong bonus, scales with how small.
      - prefix match: solid bonus.
      - substring match: smaller bonus.
      - length difference dampens base score.
    """
    qlow = query.lower()
    nlow = name.lower()

    base = float(trigram_sim)

    bonus = 0.0
    if nlow == qlow:
        bonus += RERANK_EXACT_LOWER_BONUS
    if nlow.startswith(qlow) or qlow.startswith(nlow):
        bonus += RERANK_PREFIX_BONUS
    if qlow in nlow or nlow in qlow:
        bonus += RERANK_SUBSTRING_BONUS
    # Qualified-name substring (e.g. "Foo.bar" contains "bar")
    if qualified_name:
        ql = qualified_name.lower()
        if qlow in ql:
            bonus += RERANK_SUBSTRING_BONUS / 2

    # Edit-distance bonus — close-typo signal. Critical for two cases:
    #  1. `WorkflowGrpah` should match `WorkflowGraph` (ed=1) over `Workflow`
    #     (ed=5) — close-typos beat partial-overlap shorter names.
    #  2. `agentService` should match `agentService` (ed=0) over `AgentService`
    #     (ed=1, case-only diff). The previous version gave both ed=0 and
    #     ed=1 the same bonus, so the case-only match could win on raw sim.
    #     Solution: ed=0 bonus is strictly larger than ed=1.
    if edit_distance == 0:
        bonus += 0.50
    elif edit_distance == 1:
        bonus += 0.30
    elif edit_distance == 2:
        bonus += 0.15
    elif edit_distance == 3:
        bonus += 0.05

    # Length-ratio factor: 1.0 when names are same length, < 1 when one is
    # much shorter or longer than the other.
    nl, ql_len = max(len(name), 1), max(len(query), 1)
    length_factor = (min(nl, ql_len) / max(nl, ql_len)) ** RERANK_LENGTH_EXP

    return base * length_factor + bonus


def resolve_symbol(
    name: str,
    project_slug: str | None,
    conn,
    *,
    fuzzy: bool = True,
    limit: int = 10,
    languages: list[str] | None = None,
    threshold: float | None = None,
) -> list[dict]:
    """Find symbols matching `name`.

    Args:
        name: query string.
        project_slug: scope to a project (None = search all).
        conn: psycopg2 connection (autocommit recommended).
        fuzzy: if True, pg_trgm similarity-based candidate fetch + Python re-rank.
               If False, exact name/qualified_name match only.
        limit: max results returned.
        languages: optional list of languages to filter (e.g. ['python']).
        threshold: pg_trgm similarity_threshold override. Defaults to
                   DEFAULT_TRIGRAM_THRESHOLD (0.4).

    Returns dicts including 'trigram_sim', 'edit_distance', and 'rerank_score'.
    The list is sorted by rerank_score DESC (best first).
    """
    where = ["1=1"]
    params: dict = {"name": name, "limit": limit}
    if project_slug:
        where.append("project_slug = %(slug)s")
        params["slug"] = project_slug
    if languages:
        where.append("language = ANY(%(langs)s)")
        params["langs"] = languages

    # Salvage threshold for typos. If the strict threshold returns nothing,
    # we fall back to this lower one to catch high-edit-distance fuzzies like
    # `feetchProfile` -> `fetchProfile` whose trigram similarity is ~0.36.
    # Anything found via salvage gets clearly marked low-confidence.
    SALVAGE_TRIGRAM_THRESHOLD = 0.28

    if fuzzy:
        thr = threshold if threshold is not None else DEFAULT_TRIGRAM_THRESHOLD
        # pg_trgm: % operator uses the GIN index. similarity() in WHERE does NOT.
        # The set_limit is issued inside _run_query so the salvage path can
        # use a different threshold without re-issuing the query.
        # IMPORTANT: do NOT add `qualified_name = %s` to the OR — qualified_name
        # has only a GIN trigram index, not a btree, so equality comparison
        # forces a sequential scan over the entire symbols table (~170ms on a
        # 21k-row corpus, 19× slower than necessary). The `%` operator catches
        # exact matches anyway because similarity('X','X') = 1.0 always passes
        # the threshold. Verified via EXPLAIN ANALYZE during internal dogfood.
        sql = f"""
            SELECT id, project_slug, file_path, language, kind, name, qualified_name,
                   signature, start_line, end_line, parent_symbol_id, doc,
                   GREATEST(
                     similarity(name, %(name)s),
                     similarity(coalesce(qualified_name,''), %(name)s)
                   ) AS sim
            FROM symbols
            WHERE {' AND '.join(where)}
              AND (
                name = %(name)s
                OR name %% %(name)s
                OR coalesce(qualified_name,'') %% %(name)s
              )
            ORDER BY sim DESC
            LIMIT %(fetch_limit)s
        """
        params["fetch_limit"] = CANDIDATE_FETCH_LIMIT
    else:
        sql = f"""
            SELECT id, project_slug, file_path, language, kind, name, qualified_name,
                   signature, start_line, end_line, parent_symbol_id, doc,
                   1.0 AS sim
            FROM symbols
            WHERE {' AND '.join(where)}
              AND (name = %(name)s OR qualified_name = %(name)s)
            ORDER BY length(name) ASC
            LIMIT %(fetch_limit)s
        """
        params["fetch_limit"] = limit

    def _run_query(threshold_for_run: float) -> list[dict]:
        with conn.cursor() as cur:
            if fuzzy:
                cur.execute("SELECT set_limit(%s)", (threshold_for_run,))
            cur.execute(sql, params)
            cols_ = [d[0] for d in cur.description]
            return [dict(zip(cols_, r)) for r in cur.fetchall()]

    rows = _run_query(thr) if fuzzy else _run_query(0.0)
    salvage = False
    if fuzzy and not rows:
        # Salvage: typos like `feetchProfile` (sim ≈ 0.36 vs `fetchProfile`)
        # fall below the strict threshold. Re-fetch at a lower threshold and
        # filter aggressively by edit distance — only keep candidates whose
        # Levenshtein distance is small relative to query length.
        rows = _run_query(SALVAGE_TRIGRAM_THRESHOLD)
        salvage = True

    out: list[dict] = []
    for d in rows:
        d["trigram_sim"] = float(d.pop("sim"))
        d["edit_distance"] = _levenshtein(name, d["name"])
        if fuzzy:
            d["rerank_score"] = _rerank_score(
                name, d["name"], d.get("qualified_name"),
                d["trigram_sim"], d["edit_distance"],
            )
            d["salvage"] = salvage
        else:
            d["rerank_score"] = 1.0
            d["salvage"] = False
        out.append(d)

    if salvage:
        # Aggressive edit-distance gate so we only keep the typo candidates,
        # not random low-similarity matches. Threshold scales with query length.
        max_ed = max(2, len(name) // 3)
        out = [r for r in out if r["edit_distance"] <= max_ed]

    if fuzzy:
        out.sort(key=lambda r: r["rerank_score"], reverse=True)
    return out[:limit]


def list_symbols(
    project_slug: str | None,
    conn,
    *,
    file_path: str | None = None,
    kind: str | None = None,
    language: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Browse-mode listing. No fuzzy match — exact filters only."""
    where = ["1=1"]
    params: dict = {"limit": limit, "offset": offset}
    if project_slug:
        where.append("project_slug = %(slug)s")
        params["slug"] = project_slug
    if file_path:
        where.append("file_path = %(fp)s")
        params["fp"] = file_path
    if kind:
        where.append("kind = %(kind)s")
        params["kind"] = kind
    if language:
        where.append("language = %(lang)s")
        params["lang"] = language
    sql = f"""
        SELECT id, project_slug, file_path, language, kind, name, qualified_name,
               signature, start_line, end_line, parent_symbol_id, doc
        FROM symbols
        WHERE {' AND '.join(where)}
        ORDER BY file_path, start_line
        LIMIT %(limit)s OFFSET %(offset)s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ─── Levenshtein (small, fast, no deps) ──────────────────────────────────────


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            curr[j] = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            )
        prev = curr
    return prev[-1]


# ─── Outcome logging (used by lint/lookup, expanded in v0.3.1+) ──────────────


def log_outcome(
    conn,
    *,
    session_id: str | None = None,
    project_slug: str | None = None,
    language: str | None = None,
    mode: str,
    proposed_name: str,
    resolved_name: str | None = None,
    edit_distance: int | None = None,
    trigram_sim: float | None = None,
    candidate_count: int | None = None,
    pattern_tags: list[str] | None = None,
    outcome: str = "unknown",
    outcome_signal: str | None = None,
    build_after: str | None = None,
) -> int:
    """Insert one row into correction_outcomes. Returns new id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO correction_outcomes
              (session_id, project_slug, language, mode, proposed_name, resolved_name,
               edit_distance, trigram_sim, candidate_count, pattern_tags,
               outcome, outcome_signal, build_after)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (session_id, project_slug, language, mode, proposed_name, resolved_name,
             edit_distance, trigram_sim, candidate_count, pattern_tags or [],
             outcome, outcome_signal, build_after),
        )
        return cur.fetchone()[0]


def list_outcomes(
    conn,
    *,
    project_slug: str | None = None,
    language: str | None = None,
    mode: str | None = None,
    limit: int = 50,
) -> list[dict]:
    where = ["1=1"]
    params: dict = {"limit": limit}
    if project_slug:
        where.append("project_slug = %(slug)s")
        params["slug"] = project_slug
    if language:
        where.append("language = %(lang)s")
        params["lang"] = language
    if mode:
        where.append("mode = %(mode)s")
        params["mode"] = mode
    sql = f"""
        SELECT id, ts, session_id::text, project_slug, language, mode,
               proposed_name, resolved_name, edit_distance, trigram_sim,
               vector_sim, candidate_count, pattern_tags, outcome,
               outcome_signal, build_after
        FROM correction_outcomes
        WHERE {' AND '.join(where)}
        ORDER BY ts DESC
        LIMIT %(limit)s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
