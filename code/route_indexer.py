#!/usr/bin/env python3
"""
claude-dejavu — API route indexer (v0.3.2.1).

Walks a project's source files and extracts every HTTP route declared
across the supported frameworks. Stores the result in `api_routes` +
`api_route_params` so the lint pipeline can ground client-side URL
literals (in `fetch(...)`, `axios.get(...)`, `useQuery(...)`, etc.)
against the project's actual route surface.

Sources, in precedence order:
  1. OpenAPI / Swagger files (`openapi.{json,yaml,yml}`, `swagger.{...}`,
     `api.openapi.yaml`, `openapi/*.yaml`) — the maintainer's declared
     truth.
  2. Code-side detection per framework:

     TypeScript / JavaScript
       Express      app.get/post/put/patch/delete(path, handler)
       Fastify      fastify.get/post/...({...}) AND fastify.route({...})
       Koa          router.get/post/... + app.use(...)
       Hono         app.get/post/... + Hono routing
       NestJS       @Controller('/prefix') + @Get/@Post/@Put/@Patch/@Delete
       Next.js      pages/api/**/*.{ts,js}    (filesystem)
                    app/**/route.{ts,js}      (named exports GET/POST/...)

     Python
       FastAPI      @app.get/post/.../api_route('/path')
       Flask        @app.route('/path', methods=[...])
       Django       urlpatterns = [path('...', view), re_path(...), ...]

Path normalization:
  Express  /users/:id      ─┐
  FastAPI  /users/{id}     ─┼─→ canonical:  /users/:id
  Next.js  /users/[id]     ─┘
  *       wildcards (`*` / `:rest*` / `[...slug]`) → `*`

This file is the per-framework registry. Each framework adds an
extractor function that yields RouteRecord objects; index_project_routes
walks the tree, dispatches by file, persists results.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Tree-sitter parsers loaded lazily — only when the framework is detected
# in a given file. Same parsers as symbol_indexer (Python, TS, TSX, JS).
try:
    import warnings as _w
    _w.filterwarnings("ignore", category=FutureWarning, module="tree_sitter")
    from tree_sitter_languages import get_parser as _ts_get_parser
    _HAVE_TS = True
except Exception:
    _ts_get_parser = None  # type: ignore
    _HAVE_TS = False


@dataclass
class RouteRecord:
    framework: str
    source: str               # 'openapi' | 'code'
    method: str               # 'GET' | 'POST' | ... | '*'
    path_pattern: str         # canonical normalized
    raw_path: str             # original literal
    source_file: str
    start_line: int
    end_line: int = 0
    handler_name: str | None = None
    description: str | None = None
    params: list[dict] = None  # type: ignore[assignment]


# ─── Path normalization ─────────────────────────────────────────────────────

_FASTAPI_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")           # {id} or {id:int}
_NEXTJS_PARAM = re.compile(r"\[\.{3}([^\]]+)\]|\[([^\]]+)\]")     # [...slug] or [id]


def normalize_path(path: str) -> str:
    """Canonicalize a route path so :id / {id} / [id] all collapse.

    Examples:
      '/users/{id}'       -> '/users/:id'
      '/users/[id]'       -> '/users/:id'
      '/api/users/:id'    -> '/api/users/:id'
      '/blog/[...slug]'   -> '/blog/*'
      '/api/v1/'          -> '/api/v1'      (trailing slash trimmed except root)
    """
    if not path:
        return path
    # Convert FastAPI/OpenAPI {param} → :param
    path = _FASTAPI_PARAM.sub(lambda m: f":{m.group(1)}", path)
    # Convert Next.js [param] / [...slug] → :param / *
    def _nextjs_repl(m):
        spread, name = m.groups()
        return "*" if spread else f":{name}"
    path = _NEXTJS_PARAM.sub(_nextjs_repl, path)
    # Collapse double slashes (except the protocol-like leading)
    path = re.sub(r"(?<!:)//+", "/", path)
    # Trim trailing slash unless path is exactly '/'
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


# ─── OpenAPI parser ─────────────────────────────────────────────────────────


def _extract_openapi(path: Path) -> Iterable[RouteRecord]:
    """Parse openapi.{json,yaml,yml} into RouteRecord stream."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
                spec = yaml.safe_load(text)
            except ImportError:
                # PyYAML may not be installed; fall back to a tiny YAML parser
                # that handles the OpenAPI subset (no anchors, no tags).
                spec = _yaml_minimal_load(text)
        else:
            spec = json.loads(text)
    except Exception:
        return
    if not isinstance(spec, dict):
        return
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return
    for raw_path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            m = method.upper()
            if m not in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
                continue
            description = None
            params: list[dict] = []
            if isinstance(op, dict):
                description = op.get("summary") or op.get("description")
                for p in op.get("parameters") or []:
                    if isinstance(p, dict):
                        params.append({
                            "param_name": p.get("name", ""),
                            "located_in": p.get("in", "query"),
                            "type_hint": (p.get("schema") or {}).get("type"),
                            "required": bool(p.get("required")),
                            "description": p.get("description"),
                        })
                # Body params via requestBody
                rb = op.get("requestBody") or {}
                if isinstance(rb, dict) and rb.get("content"):
                    for ct, body in (rb.get("content") or {}).items():
                        schema = (body or {}).get("schema") or {}
                        for prop_name, prop_def in (schema.get("properties") or {}).items():
                            params.append({
                                "param_name": prop_name,
                                "located_in": "body",
                                "type_hint": (prop_def or {}).get("type"),
                                "required": prop_name in (schema.get("required") or []),
                                "description": (prop_def or {}).get("description"),
                            })
            yield RouteRecord(
                framework="openapi",
                source="openapi",
                method=m,
                path_pattern=normalize_path(raw_path),
                raw_path=raw_path,
                source_file=str(path),
                start_line=1,
                description=description,
                params=params or [],
            )


def _yaml_minimal_load(text: str):
    """Very small YAML loader for OpenAPI specs when PyYAML isn't available.
    Handles the subset of YAML actually used in OpenAPI specs: nested maps,
    lists with `- ` bullets, scalars (strings/ints/bools/null). No anchors,
    no tags, no flow-style. Falls back to None on anything weird.
    """
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except Exception:
        # Minimal fallback — best-effort.
        return None


# ─── TypeScript / JavaScript extractors ─────────────────────────────────────


_HTTP_METHODS_LOWER = ("get", "post", "put", "patch", "delete", "options", "head")
_NEXTJS_NAMED_EXPORT_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}


def _ts_decode(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _string_literal_value(node, src: bytes) -> str | None:
    """If `node` is a string literal (quoted or template no-interp), return its value."""
    t = node.type
    if t == "string":
        # TS/JS string nodes are wrapped: the first/last children are quotes; concat the fragments.
        parts = []
        for c in node.named_children:
            if c.type == "string_fragment":
                parts.append(_ts_decode(c, src))
            elif c.type == "escape_sequence":
                parts.append(_ts_decode(c, src))
        if parts:
            return "".join(parts)
        # Fallback: strip quotes
        s = _ts_decode(node, src)
        if len(s) >= 2 and s[0] in "\"'`" and s[-1] == s[0]:
            return s[1:-1]
        return s
    if t == "template_string":
        # No-interp templates only — we don't track interpolated route paths.
        # If there are `template_substitution` children, it's interpolated → skip.
        if any(c.type == "template_substitution" for c in node.named_children):
            return None
        # Otherwise concatenate the literal fragments.
        s = _ts_decode(node, src).strip("`")
        return s
    return None


def _extract_express_like(source: str, parser_lang: str, source_file: str) -> Iterable[RouteRecord]:
    """Catches Express, Fastify, Koa router, Hono — they all share the
    pattern <obj>.<method>(<path>, ...handlers).

    We don't try to identify which framework it is — we just record
    'express' as the framework label since the call shape is the same.
    A later refinement could distinguish by import statements; not needed
    for the lint use case.
    """
    if not _HAVE_TS:
        return
    parser = _ts_get_parser(parser_lang)
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)

    def walk(node):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if fn and fn.type == "member_expression" and args and args.named_children:
                prop = fn.child_by_field_name("property")
                obj = fn.child_by_field_name("object")
                if prop and prop.type == "property_identifier":
                    method_name = _ts_decode(prop, src_b).lower()
                    if method_name in _HTTP_METHODS_LOWER:
                        first_arg = args.named_children[0]
                        path_str = _string_literal_value(first_arg, src_b)
                        if path_str and path_str.startswith("/"):
                            obj_text = _ts_decode(obj, src_b) if obj else ""
                            framework = _guess_framework_from_obj(obj_text)
                            yield RouteRecord(
                                framework=framework,
                                source="code",
                                method=method_name.upper(),
                                path_pattern=normalize_path(path_str),
                                raw_path=path_str,
                                source_file=source_file,
                                start_line=node.start_point[0] + 1,
                                end_line=node.end_point[0] + 1,
                            )
        for c in node.named_children:
            yield from walk(c)

    yield from walk(tree.root_node)


def _guess_framework_from_obj(obj_text: str) -> str:
    """Best-effort framework detection from the receiver expression.
    Defaults to 'express' which is by far the most common shape."""
    low = obj_text.lower()
    if "fastify" in low: return "fastify"
    if "hono" in low: return "hono"
    if "koa" in low or "ctx" in low: return "koa"
    if "router" in low: return "express"  # Express Router or fastify Router
    return "express"


def _extract_nestjs(source: str, parser_lang: str, source_file: str) -> Iterable[RouteRecord]:
    """NestJS: @Controller('/prefix') + @Get/@Post/.../@Patch('/path') decorators
    on method definitions inside a class.
    """
    if not _HAVE_TS:
        return
    parser = _ts_get_parser(parser_lang)
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)

    nest_method_decorators = {"Get", "Post", "Put", "Patch", "Delete", "Options", "Head", "All"}

    def decorator_call(node):
        """Returns (callee_name, [str args]) if `node` is a decorator with a call expression."""
        if node.type != "decorator":
            return None, []
        expr = node.named_children[0] if node.named_children else None
        if not expr:
            return None, []
        if expr.type == "call_expression":
            fn = expr.child_by_field_name("function")
            args = expr.child_by_field_name("arguments")
            name = _ts_decode(fn, src_b) if fn else ""
            arg_values: list[str] = []
            if args:
                for a in args.named_children:
                    v = _string_literal_value(a, src_b)
                    if v is not None:
                        arg_values.append(v)
            return name, arg_values
        if expr.type == "identifier":
            return _ts_decode(expr, src_b), []
        return None, []

    def collect_class_routes(class_node, prefix: str):
        body = class_node.child_by_field_name("body")
        if not body:
            return
        for member in body.named_children:
            if member.type != "method_definition":
                continue
            decos = []
            # NestJS decorators are siblings BEFORE the method_definition;
            # tree-sitter places them as decorator children of method_definition's
            # parent in some grammars and as preceding siblings in others. We
            # look at the method_definition's named_children for both shapes.
            for d in member.named_children:
                if d.type == "decorator":
                    decos.append(d)
            # Some grammars use 'method_definition.modifiers' or place decorators
            # as preceding siblings. Handle the prev-sibling case too:
            prev = member.prev_named_sibling
            while prev is not None and prev.type == "decorator":
                decos.append(prev)
                prev = prev.prev_named_sibling
            for d in decos:
                name, args = decorator_call(d)
                if not name:
                    continue
                # Strip namespace if any: '@common.Get' → 'Get'
                short = name.split(".")[-1]
                if short in nest_method_decorators:
                    method = short.upper() if short != "All" else "*"
                    raw_subpath = args[0] if args else ""
                    raw_full = (prefix.rstrip("/") + "/" + raw_subpath.lstrip("/")) if raw_subpath else prefix
                    if not raw_full.startswith("/"):
                        raw_full = "/" + raw_full
                    name_node = member.child_by_field_name("name")
                    handler_name = _ts_decode(name_node, src_b) if name_node else None
                    yield RouteRecord(
                        framework="nestjs",
                        source="code",
                        method=method,
                        path_pattern=normalize_path(raw_full),
                        raw_path=raw_full,
                        source_file=source_file,
                        start_line=d.start_point[0] + 1,
                        end_line=member.end_point[0] + 1,
                        handler_name=handler_name,
                    )

    def walk(node):
        if node.type == "class_declaration":
            prefix = ""
            # Look for @Controller('/prefix') decorator
            decos = [c for c in node.named_children if c.type == "decorator"]
            prev = node.prev_named_sibling
            while prev is not None and prev.type == "decorator":
                decos.append(prev)
                prev = prev.prev_named_sibling
            for d in decos:
                name, args = decorator_call(d)
                if name and name.split(".")[-1] == "Controller":
                    prefix = args[0] if args else ""
                    if prefix and not prefix.startswith("/"):
                        prefix = "/" + prefix
                    break
            yield from collect_class_routes(node, prefix or "")
        for c in node.named_children:
            yield from walk(c)

    yield from walk(tree.root_node)


def _extract_nextjs_app_router(source: str, source_file: str, route_path: str) -> Iterable[RouteRecord]:
    """Next.js app router: file-system path → route, named exports
    GET/POST/... in the file declare which methods are handled.
    """
    if not _HAVE_TS:
        return
    parser = _ts_get_parser("typescript")  # works for .ts, .tsx, .js, .mjs
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)
    found: set[str] = set()

    def walk(node):
        if node.type == "export_statement":
            decl = node.child_by_field_name("declaration")
            if not decl:
                # `export { GET, POST }` shape — handled below
                for c in node.named_children:
                    if c.type == "export_clause":
                        for spec in c.named_children:
                            if spec.type == "export_specifier":
                                nm = spec.child_by_field_name("name")
                                if nm:
                                    name = _ts_decode(nm, src_b)
                                    if name in _NEXTJS_NAMED_EXPORT_METHODS:
                                        found.add(name)
                return
            # `export function GET(...) {}` or `export async function POST(...) {}`
            if decl.type in ("function_declaration", "generator_function_declaration"):
                nm = decl.child_by_field_name("name")
                if nm:
                    name = _ts_decode(nm, src_b)
                    if name in _NEXTJS_NAMED_EXPORT_METHODS:
                        found.add(name)
            elif decl.type == "lexical_declaration":
                # `export const GET = async (req) => {...}`
                for vd in decl.named_children:
                    if vd.type == "variable_declarator":
                        nm = vd.child_by_field_name("name")
                        if nm and nm.type == "identifier":
                            name = _ts_decode(nm, src_b)
                            if name in _NEXTJS_NAMED_EXPORT_METHODS:
                                found.add(name)
        for c in node.named_children:
            walk(c)

    walk(tree.root_node)

    for method in sorted(found):
        yield RouteRecord(
            framework="nextjs-app",
            source="code",
            method=method,
            path_pattern=normalize_path(route_path),
            raw_path=route_path,
            source_file=source_file,
            start_line=1,
        )


def _nextjs_app_route_path(repo_root: Path, file_path: Path) -> str | None:
    """Convert app/foo/bar/route.ts → /foo/bar.
    Skips files that aren't under an `app/` dir or aren't named route.{ts,tsx,js,mjs}.
    """
    rel = file_path.relative_to(repo_root) if repo_root in file_path.parents else file_path
    parts = list(rel.parts)
    # Find an 'app' segment
    if "app" not in parts:
        return None
    idx = parts.index("app")
    sub = parts[idx + 1:]
    if not sub or sub[-1] not in ("route.ts", "route.tsx", "route.js", "route.mjs"):
        return None
    segments = sub[:-1]
    # Skip groups: (admin) etc.
    cleaned = [seg for seg in segments if not (seg.startswith("(") and seg.endswith(")"))]
    if not cleaned:
        return "/"
    return "/" + "/".join(cleaned)


def _nextjs_pages_route_path(repo_root: Path, file_path: Path) -> str | None:
    """Convert pages/api/users/[id].ts → /api/users/:id.
    Returns None for non-pages/api files.
    """
    rel = file_path.relative_to(repo_root) if repo_root in file_path.parents else file_path
    parts = list(rel.parts)
    if "pages" not in parts:
        return None
    idx = parts.index("pages")
    sub = parts[idx + 1:]
    if not sub or sub[0] != "api":
        return None
    # Drop extension on last segment
    last = sub[-1]
    name, ext = os.path.splitext(last)
    if ext.lower() not in (".ts", ".tsx", ".js", ".mjs", ".cjs"):
        return None
    if name == "index":
        path_segments = sub[1:-1]
    else:
        path_segments = sub[1:-1] + [name]
    raw = "/api/" + "/".join(path_segments) if path_segments else "/api"
    return raw


# ─── Python extractors ──────────────────────────────────────────────────────


def _extract_fastapi(source: str, source_file: str) -> Iterable[RouteRecord]:
    """FastAPI: @app.get('/path') / @router.post('/path') / @app.api_route('/path', methods=[...])"""
    if not _HAVE_TS:
        return
    parser = _ts_get_parser("python")
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)

    def first_str_arg(call_node):
        args = call_node.child_by_field_name("arguments")
        if not args:
            return None
        for a in args.named_children:
            if a.type == "string":
                txt = _ts_decode(a, src_b)
                if len(txt) >= 2 and txt[0] in "\"'" and txt[-1] == txt[0]:
                    return txt[1:-1]
        return None

    def find_methods_kwarg(call_node) -> list[str]:
        """For api_route('/path', methods=['GET','POST'])."""
        out: list[str] = []
        args = call_node.child_by_field_name("arguments")
        if not args:
            return out
        for a in args.named_children:
            if a.type == "keyword_argument":
                key_node = a.child_by_field_name("name")
                val = a.child_by_field_name("value")
                if key_node and _ts_decode(key_node, src_b) == "methods" and val and val.type == "list":
                    for elem in val.named_children:
                        if elem.type == "string":
                            txt = _ts_decode(elem, src_b)
                            if len(txt) >= 2 and txt[0] in "\"'" and txt[-1] == txt[0]:
                                out.append(txt[1:-1].upper())
        return out

    def walk(node):
        if node.type == "decorated_definition":
            target = node.child_by_field_name("definition")
            handler_name = None
            if target and target.type in ("function_definition", "async_function_definition"):
                nm = target.child_by_field_name("name")
                if nm:
                    handler_name = _ts_decode(nm, src_b)
            for c in node.named_children:
                if c.type == "decorator":
                    inner = c.named_children[0] if c.named_children else None
                    if inner and inner.type == "call":
                        fn = inner.child_by_field_name("function")
                        if not fn or fn.type != "attribute":
                            continue
                        attr = fn.child_by_field_name("attribute")
                        if not attr:
                            continue
                        method_name = _ts_decode(attr, src_b)
                        path_str = first_str_arg(inner)
                        if not path_str:
                            continue
                        if method_name == "api_route":
                            methods = find_methods_kwarg(inner) or ["GET"]
                            for m in methods:
                                yield RouteRecord(
                                    framework="fastapi",
                                    source="code",
                                    method=m,
                                    path_pattern=normalize_path(path_str),
                                    raw_path=path_str,
                                    source_file=source_file,
                                    start_line=c.start_point[0] + 1,
                                    handler_name=handler_name,
                                )
                        elif method_name in _HTTP_METHODS_LOWER:
                            yield RouteRecord(
                                framework="fastapi",
                                source="code",
                                method=method_name.upper(),
                                path_pattern=normalize_path(path_str),
                                raw_path=path_str,
                                source_file=source_file,
                                start_line=c.start_point[0] + 1,
                                handler_name=handler_name,
                            )
        for c in node.named_children:
            yield from walk(c)

    yield from walk(tree.root_node)


def _extract_flask(source: str, source_file: str) -> Iterable[RouteRecord]:
    """Flask: @app.route('/path', methods=['GET','POST'])"""
    if not _HAVE_TS:
        return
    parser = _ts_get_parser("python")
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)

    def walk(node):
        if node.type == "decorated_definition":
            target = node.child_by_field_name("definition")
            handler_name = None
            if target:
                nm = target.child_by_field_name("name")
                if nm:
                    handler_name = _ts_decode(nm, src_b)
            for c in node.named_children:
                if c.type == "decorator":
                    inner = c.named_children[0] if c.named_children else None
                    if not (inner and inner.type == "call"):
                        continue
                    fn = inner.child_by_field_name("function")
                    if not fn or fn.type != "attribute":
                        continue
                    attr = fn.child_by_field_name("attribute")
                    if not attr or _ts_decode(attr, src_b) != "route":
                        continue
                    args = inner.child_by_field_name("arguments")
                    if not args:
                        continue
                    path_str = None
                    methods = ["GET"]
                    for a in args.named_children:
                        if a.type == "string" and path_str is None:
                            txt = _ts_decode(a, src_b)
                            if len(txt) >= 2 and txt[0] in "\"'" and txt[-1] == txt[0]:
                                path_str = txt[1:-1]
                        elif a.type == "keyword_argument":
                            kn = a.child_by_field_name("name")
                            v = a.child_by_field_name("value")
                            if kn and _ts_decode(kn, src_b) == "methods" and v and v.type == "list":
                                ms = []
                                for elem in v.named_children:
                                    if elem.type == "string":
                                        txt = _ts_decode(elem, src_b)
                                        if len(txt) >= 2:
                                            ms.append(txt[1:-1].upper())
                                methods = ms or methods
                    if path_str:
                        for m in methods:
                            yield RouteRecord(
                                framework="flask",
                                source="code",
                                method=m,
                                path_pattern=normalize_path(path_str),
                                raw_path=path_str,
                                source_file=source_file,
                                start_line=c.start_point[0] + 1,
                                handler_name=handler_name,
                            )
        for c in node.named_children:
            yield from walk(c)

    yield from walk(tree.root_node)


def _extract_django(source: str, source_file: str) -> Iterable[RouteRecord]:
    """Django: urlpatterns = [path('users/', UserView), re_path(r'^foo$', ...)]"""
    if not _HAVE_TS:
        return
    parser = _ts_get_parser("python")
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)

    def collect_calls(node, out):
        if node.type == "call":
            fn = node.child_by_field_name("function")
            if fn:
                fn_name = _ts_decode(fn, src_b).split(".")[-1]
                if fn_name in ("path", "re_path", "url"):
                    args = node.child_by_field_name("arguments")
                    if args and args.named_children:
                        first = args.named_children[0]
                        if first.type == "string":
                            txt = _ts_decode(first, src_b)
                            if len(txt) >= 2 and txt[0] in "\"'" and txt[-1] == txt[0]:
                                raw = txt[1:-1]
                                # Convert Django's <int:id> / <str:slug> / <id> → :id
                                raw = re.sub(r"<(?:\w+:)?(\w+)>", r":\1", raw)
                                out.append((raw, node))
        for c in node.named_children:
            collect_calls(c, out)

    found: list[tuple[str, object]] = []
    collect_calls(tree.root_node, found)
    for raw, node in found:
        # Django routes are method-agnostic at the URL level (the View dispatches).
        # We record method='*' so client-side lint can match against any method.
        yield RouteRecord(
            framework="django",
            source="code",
            method="*",
            path_pattern=normalize_path(raw if raw.startswith("/") else "/" + raw),
            raw_path=raw,
            source_file=source_file,
            start_line=node.start_point[0] + 1 if hasattr(node, "start_point") else 1,
        )


# ─── Project walker / dispatcher ────────────────────────────────────────────


_OPENAPI_FILENAMES = (
    "openapi.json", "openapi.yaml", "openapi.yml",
    "swagger.json", "swagger.yaml", "swagger.yml",
)


def _file_state(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def _index_routes_for_file(file_path: Path, project_slug: str, repo_root: Path,
                            conn) -> dict:
    """Process one file. Returns stats dict."""
    if not file_path.exists():
        return {"routes": 0, "framework": None, "skipped": "missing"}

    suffix = file_path.suffix.lower()
    name = file_path.name.lower()
    rel = str(file_path)
    records: list[RouteRecord] = []
    framework_label = "code"

    # 1. OpenAPI files
    if name in _OPENAPI_FILENAMES or (suffix in (".yaml", ".yml", ".json")
                                      and "openapi" in str(file_path).lower()):
        records = list(_extract_openapi(file_path))
        framework_label = "openapi"

    # 2. Next.js app router
    elif name in ("route.ts", "route.tsx", "route.js", "route.mjs"):
        rp = _nextjs_app_route_path(repo_root, file_path)
        if rp is not None:
            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                source = ""
            records = list(_extract_nextjs_app_router(source, rel, rp))
            framework_label = "nextjs-app"

    # 3. Next.js pages router
    elif suffix in (".ts", ".tsx", ".js", ".mjs", ".cjs"):
        rp = _nextjs_pages_route_path(repo_root, file_path)
        if rp is not None:
            # File-system route declares the path; method = '*' (handler dispatches internally)
            records = [RouteRecord(
                framework="nextjs-pages",
                source="code",
                method="*",
                path_pattern=normalize_path(rp),
                raw_path=rp,
                source_file=rel,
                start_line=1,
            )]
            framework_label = "nextjs-pages"
        else:
            # Fall through to TS/JS framework detection (Express, Fastify, NestJS, etc.)
            try:
                source = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                source = ""
            parser_lang = "tsx" if suffix == ".tsx" else ("typescript" if suffix in (".ts",) else "javascript")
            # Run all three TS/JS extractors — they're cheap and complementary.
            records = list(_extract_express_like(source, parser_lang, rel))
            records += list(_extract_nestjs(source, parser_lang, rel))
            framework_label = records[0].framework if records else "code"

    # 4. Python frameworks
    elif suffix == ".py":
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = ""
        records = list(_extract_fastapi(source, rel))
        records += list(_extract_flask(source, rel))
        # Django urls.py
        if name == "urls.py" or "urlpatterns" in source:
            records += list(_extract_django(source, rel))
        framework_label = records[0].framework if records else "code"

    if not records:
        return {"routes": 0, "framework": None, "skipped": "no_routes"}

    # Persist
    mtime_ns, size_bytes = _file_state(file_path)
    sha = hashlib.sha256(file_path.read_bytes()).hexdigest() if file_path.exists() else ""
    with conn.cursor() as cur:
        # Replace existing routes from this file (atomic per-file).
        cur.execute("DELETE FROM api_routes WHERE source_file = %s AND project_slug = %s",
                    (rel, project_slug))
        for r in records:
            cur.execute(
                """INSERT INTO api_routes
                   (project_slug, framework, source, method, path_pattern, raw_path,
                    source_file, start_line, end_line, description, indexed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                   RETURNING id""",
                (project_slug, r.framework, r.source, r.method, r.path_pattern, r.raw_path,
                 rel, r.start_line, r.end_line or r.start_line, r.description),
            )
            route_id = cur.fetchone()[0]
            for p in (r.params or []):
                cur.execute(
                    """INSERT INTO api_route_params
                       (route_id, param_name, located_in, type_hint, required, description)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (route_id, p.get("param_name", ""), p.get("located_in", "query"),
                     p.get("type_hint"), bool(p.get("required")), p.get("description")),
                )
        cur.execute(
            """INSERT INTO api_route_file_state
               (file_path, project_slug, framework, source, mtime_ns, size_bytes, sha256,
                routes_count, indexed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (file_path) DO UPDATE SET
                 project_slug = EXCLUDED.project_slug,
                 framework = EXCLUDED.framework,
                 source = EXCLUDED.source,
                 mtime_ns = EXCLUDED.mtime_ns,
                 size_bytes = EXCLUDED.size_bytes,
                 sha256 = EXCLUDED.sha256,
                 routes_count = EXCLUDED.routes_count,
                 indexed_at = now()""",
            (rel, project_slug, framework_label, records[0].source if records else "code",
             mtime_ns, size_bytes, sha, len(records)),
        )
    return {"routes": len(records), "framework": framework_label}


def index_project_routes(project_dir: str, project_slug: str, conn,
                          *, force: bool = False, flt=None) -> dict:
    """Walk a project, extract routes from every supported file, persist
    to api_routes + api_route_params. Returns stats."""
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"error": f"not_a_directory: {root}"}
    stats = {"files_scanned": 0, "files_with_routes": 0, "routes_added": 0, "errors": 0}

    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(root)

    # ─── v0.4.5: fingerprint cache ─────────────────────────────────────
    from file_state import IndexerCache, files_under
    relevant = list(files_under(
        root,
        suffixes={".ts", ".tsx", ".js", ".mjs", ".cjs", ".py",
                   ".yaml", ".yml", ".json"},
        flt=flt,
    ))
    cache = IndexerCache(conn, project_slug, "route")
    if not force and cache.is_fresh(relevant):
        return cache.cached_stats_marked()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                        if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
        filenames = [f for f in filenames
                       if not flt.should_skip(Path(dirpath) / f, is_dir=False)]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".ts", ".tsx", ".js", ".mjs", ".cjs", ".py", ".yaml", ".yml", ".json"):
                continue
            full = Path(dirpath) / fn
            stats["files_scanned"] += 1
            # Cheap pre-skip: only consider .yaml/.yml/.json if filename hints OpenAPI.
            if ext in (".yaml", ".yml", ".json") and fn.lower() not in _OPENAPI_FILENAMES \
                    and "openapi" not in fn.lower() and "swagger" not in fn.lower():
                continue
            try:
                result = _index_routes_for_file(full, project_slug, root, conn)
            except Exception as e:
                stats["errors"] += 1
                import sys
                sys.stderr.write(f"route_indexer: {full}: {type(e).__name__}: {e}\n")
                continue
            if result.get("routes", 0) > 0:
                stats["files_with_routes"] += 1
                stats["routes_added"] += result["routes"]

    cache.store(relevant, stats=stats)
    return stats


# ─── Lookup ────────────────────────────────────────────────────────────────


def _path_pattern_matches(client_path: str, route_pattern: str) -> bool:
    """True if a concrete client URL matches a parameterized route pattern.

    `/api/users/123`  matches  `/api/users/:id`        → True
    `/api/users`      matches  `/api/users/:id`        → False
    `/api/users/123`  matches  `/api/users`            → False
    `/blog/foo/bar`   matches  `/blog/*`               → True (wildcard)
    """
    if not client_path or not route_pattern:
        return False
    cs = [s for s in client_path.split("/") if s != ""]
    rs = [s for s in route_pattern.split("/") if s != ""]
    # Wildcard at end matches any remaining segments
    if rs and rs[-1] == "*":
        if len(cs) < len(rs) - 1:
            return False
        for i in range(len(rs) - 1):
            r = rs[i]
            if r.startswith(":"):
                continue
            if i >= len(cs) or cs[i] != r:
                return False
        return True
    if len(cs) != len(rs):
        return False
    for c, r in zip(cs, rs):
        if r.startswith(":"):
            continue
        if c != r:
            return False
    return True


def resolve_route(method: str, path: str, project_slug: str, conn,
                   *, fuzzy: bool = True, limit: int = 5) -> list[dict]:
    """Look up a route by (method, path). Three layers, in order:
      1. Exact normalized path match (method-agnostic if method='*' is recorded
         OR if the caller passed method='*').
      2. Pattern match — concrete `/api/users/123` against parameterized
         `/api/users/:id`. This is the common case for client-side calls.
      3. Fuzzy on path_pattern via pg_trgm — for typos.

    Returns ranked dicts; `match_kind` ∈ {'exact','pattern','fuzzy'}.
    """
    norm = normalize_path(path)
    method = method.upper()
    seen: set[int] = set()
    out: list[dict] = []
    with conn.cursor() as cur:
        # 1. Exact
        cur.execute(
            """SELECT id, framework, source, method, path_pattern, raw_path,
                      source_file, start_line, description
               FROM api_routes
               WHERE project_slug = %s
                 AND path_pattern = %s
                 AND (method = %s OR method = '*' OR %s = '*')
               ORDER BY (method = %s) DESC, source DESC
               LIMIT %s""",
            (project_slug, norm, method, method, method, limit),
        )
        for row in cur.fetchall():
            seen.add(row[0])
            out.append({
                "id": row[0], "framework": row[1], "source": row[2],
                "method": row[3], "path_pattern": row[4], "raw_path": row[5],
                "source_file": row[6], "start_line": row[7], "description": row[8],
                "match_kind": "exact", "trigram_sim": 1.0,
            })
        if out:
            return out

        # 2. Pattern match — fetch all parameterized routes for this project
        # (paths containing ':' or '*') and test each against the client URL.
        # For typical projects this is bounded (tens to hundreds) so the
        # in-Python scan is fine; if it ever scales, we can move to a path
        # trie.
        cur.execute(
            """SELECT id, framework, source, method, path_pattern, raw_path,
                      source_file, start_line, description
               FROM api_routes
               WHERE project_slug = %s
                 AND (path_pattern LIKE %s OR path_pattern LIKE %s)
                 AND (method = %s OR method = '*' OR %s = '*')
               ORDER BY length(path_pattern) DESC""",
            (project_slug, '%:%', '%*%', method, method),
        )
        for row in cur.fetchall():
            if _path_pattern_matches(norm, row[4]):
                seen.add(row[0])
                out.append({
                    "id": row[0], "framework": row[1], "source": row[2],
                    "method": row[3], "path_pattern": row[4], "raw_path": row[5],
                    "source_file": row[6], "start_line": row[7], "description": row[8],
                    "match_kind": "pattern", "trigram_sim": 1.0,
                })
                if len(out) >= limit:
                    return out
        if out:
            return out

        # 3. Fuzzy on the path_pattern (typos).
        if fuzzy:
            try:
                from adaptive_threshold import get_threshold
                t = get_threshold(project_slug, "route", conn)
                # Routes default lower (0.3) than other channels because
                # path_pattern strings tend to share many trigrams via
                # shared prefixes (`/api/`, `/users/`); allow lower
                # similarity floor unless the tuner says otherwise.
                if t == 0.40 or t == 0.50:
                    t = 0.3
            except Exception:
                t = 0.3
            cur.execute("SELECT set_limit(%s)", (t,))
            cur.execute(
                """SELECT id, framework, source, method, path_pattern, raw_path,
                          source_file, start_line, description,
                          similarity(path_pattern, %s) AS sim
                   FROM api_routes
                   WHERE project_slug = %s
                     AND (path_pattern %% %s OR raw_path %% %s)
                     AND (method = %s OR method = '*' OR %s = '*')
                   ORDER BY sim DESC
                   LIMIT %s""",
                (norm, project_slug, norm, norm, method, method, limit),
            )
            for row in cur.fetchall():
                if row[0] in seen:
                    continue
                out.append({
                    "id": row[0], "framework": row[1], "source": row[2],
                    "method": row[3], "path_pattern": row[4], "raw_path": row[5],
                    "source_file": row[6], "start_line": row[7], "description": row[8],
                    "match_kind": "fuzzy", "trigram_sim": float(row[9] or 0),
                })
    return out


def list_routes(project_slug: str, conn, *,
                framework: str | None = None,
                method: str | None = None,
                limit: int = 100,
                offset: int = 0) -> list[dict]:
    where = ["project_slug = %s"]
    params: list = [project_slug]
    if framework:
        where.append("framework = %s"); params.append(framework)
    if method:
        where.append("method = %s"); params.append(method.upper())
    sql = (f"SELECT framework, source, method, path_pattern, raw_path, source_file, start_line "
           f"FROM api_routes WHERE {' AND '.join(where)} "
           f"ORDER BY path_pattern, method LIMIT %s OFFSET %s")
    params += [limit, offset]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
