#!/usr/bin/env python3
"""
URL-literal walker for proposed file content.

Extracts (HTTP method, URL string) pairs from client-side call sites in
TypeScript / JavaScript / Python source. Used by the route_grounding
lint strategy to ground each call against the project's `api_routes`
table.

Detection patterns:

  TypeScript / JavaScript
    fetch(URL, [opts])                    method = opts.method || 'GET'
    fetch(URL)                            method = 'GET'
    axios.get/post/put/patch/delete(URL)  method = the called method
    axios(URL, [opts])                    method = opts.method || 'GET'
    httpClient.get/post/...(URL)          method = the called method
    apiClient.get/post/...(URL)
    useQuery({queryKey: [URL], ...})      method = 'GET'
    useSWR(URL, ...)                      method = 'GET'
    useMutation({...mutationFn: () => fetch(URL, {method:'POST'})})
                                          method extracted from inner fetch

  Python
    requests.get/post/...(URL)
    httpx.get/post/...(URL)
    httpx.AsyncClient().{get,post,...}(URL)
    urllib.request.urlopen(URL)           method = 'GET'
    aiohttp.ClientSession().{get,post,...}(URL)

URL strings must be literals — interpolated template-strings/f-strings
are skipped (we have no way to verify a partially-known URL). When a
URL is interpolated we record it as a 'skipped' for outcome accounting
but don't lint it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

try:
    import warnings as _w
    _w.filterwarnings("ignore", category=FutureWarning, module="tree_sitter")
    from tree_sitter_languages import get_parser as _ts_get_parser
    _HAVE_TS = True
except Exception:
    _ts_get_parser = None  # type: ignore
    _HAVE_TS = False


@dataclass
class CallSite:
    method: str            # 'GET' | 'POST' | ... | '*' (unknown)
    url: str               # raw URL literal as written
    library: str           # 'fetch'|'axios'|'http_client'|'react_query'|'swr'|'requests'|'httpx'|'urllib'|'aiohttp'
    start_line: int
    interpolated: bool = False


_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _ts_decode(node, src_b: bytes) -> str:
    return src_b[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _ts_string_value(node, src_b: bytes) -> tuple[str | None, bool]:
    """Return (value, interpolated). value is the literal text; interpolated
    True means the string contained ${...} substitutions (so we have only
    a partial URL).
    """
    t = node.type
    if t == "string":
        parts: list[str] = []
        for c in node.named_children:
            if c.type == "string_fragment":
                parts.append(_ts_decode(c, src_b))
        if parts:
            return "".join(parts), False
        s = _ts_decode(node, src_b)
        if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
            return s[1:-1], False
        return s, False
    if t == "template_string":
        interp = any(c.type == "template_substitution" for c in node.named_children)
        # Get the literal pieces
        s = _ts_decode(node, src_b).strip("`")
        return s, interp
    return None, False


def _py_string_value(node, src_b: bytes) -> tuple[str | None, bool]:
    """Python string literal extraction. f-strings with interpolations are
    flagged as interpolated."""
    if node.type != "string":
        return None, False
    txt = _ts_decode(node, src_b)
    # Detect f-string by prefix
    is_f = txt.lstrip().startswith(("f'", 'f"', "F'", 'F"', "rf", "fr", "Rf", "fR", "RF", "FR"))
    if is_f and "{" in txt:
        return txt, True
    if len(txt) >= 2 and txt[0] in "\"'" and txt[-1] == txt[0]:
        return txt[1:-1], False
    # Concatenated string (multi-segment)
    parts: list[str] = []
    interp = False
    for c in node.named_children:
        if c.type == "string_content":
            parts.append(_ts_decode(c, src_b))
        elif c.type == "interpolation":
            interp = True
    return ("".join(parts) if parts else txt), interp


def _looks_like_http_url(s: str) -> bool:
    if not s:
        return False
    s = s.strip()
    return s.startswith(("http://", "https://", "/")) or s.startswith(("api/", "v1/", "v2/"))


def _opts_method(opts_node, src_b: bytes) -> str | None:
    """Walk an object literal for `method: 'POST'`."""
    if opts_node is None or opts_node.type not in ("object", "object_literal", "object_expression"):
        return None
    for prop in opts_node.named_children:
        if prop.type in ("pair", "property", "object_property"):
            key = prop.child_by_field_name("key") or (prop.named_children[0] if prop.named_children else None)
            val = prop.child_by_field_name("value") or (prop.named_children[1] if len(prop.named_children) > 1 else None)
            key_text = _ts_decode(key, src_b) if key else ""
            if key_text.strip("\"'`") == "method" and val is not None:
                v, _ = _ts_string_value(val, src_b)
                if v:
                    return v.upper()
    return None


def _walk_ts_calls(source: str, parser_lang: str) -> Iterable[CallSite]:
    if not _HAVE_TS:
        return
    parser = _ts_get_parser(parser_lang)
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)

    def visit(node):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if fn and args and args.named_children:
                fn_text = _ts_decode(fn, src_b)
                # 1. fetch(URL, opts)
                if fn_text == "fetch":
                    first = args.named_children[0]
                    val, interp = _ts_string_value(first, src_b)
                    if val and _looks_like_http_url(val):
                        method = "GET"
                        if len(args.named_children) > 1:
                            m = _opts_method(args.named_children[1], src_b)
                            if m:
                                method = m
                        yield CallSite(method=method, url=val, library="fetch",
                                       start_line=node.start_point[0] + 1,
                                       interpolated=interp)
                # 2. axios.get/post/... and axios(URL)
                elif fn.type == "member_expression":
                    obj = fn.child_by_field_name("object")
                    prop = fn.child_by_field_name("property")
                    obj_text = _ts_decode(obj, src_b) if obj else ""
                    prop_text = _ts_decode(prop, src_b) if prop else ""
                    method_lower = prop_text.lower()
                    if method_lower in _HTTP_VERBS and obj_text in (
                        "axios", "http", "client", "httpClient", "apiClient", "api",
                        "fetch", "$fetch", "$http"
                    ) or method_lower in _HTTP_VERBS and any(
                        kw in obj_text.lower() for kw in ("axios", "client", "http")
                    ):
                        first = args.named_children[0]
                        val, interp = _ts_string_value(first, src_b)
                        if val and _looks_like_http_url(val):
                            yield CallSite(method=method_lower.upper(), url=val,
                                           library="axios" if "axios" in obj_text.lower() else "http_client",
                                           start_line=node.start_point[0] + 1,
                                           interpolated=interp)
                # 3. axios(URL, opts) — bare call
                elif fn_text == "axios":
                    first = args.named_children[0]
                    val, interp = _ts_string_value(first, src_b)
                    if val and _looks_like_http_url(val):
                        method = "GET"
                        if len(args.named_children) > 1:
                            m = _opts_method(args.named_children[1], src_b)
                            if m:
                                method = m
                        yield CallSite(method=method, url=val, library="axios",
                                       start_line=node.start_point[0] + 1,
                                       interpolated=interp)
                # 4. useSWR(URL, ...) — first arg is the URL, GET assumed
                elif fn_text in ("useSWR", "useSWRImmutable"):
                    first = args.named_children[0]
                    val, interp = _ts_string_value(first, src_b)
                    if val and _looks_like_http_url(val):
                        yield CallSite(method="GET", url=val, library="swr",
                                       start_line=node.start_point[0] + 1,
                                       interpolated=interp)
                # 5. useQuery({queryKey: [URL], ...}) — peek the queryKey array
                elif fn_text in ("useQuery", "useInfiniteQuery", "useMutation"):
                    first = args.named_children[0]
                    if first.type in ("object", "object_literal", "object_expression"):
                        for prop in first.named_children:
                            key = prop.child_by_field_name("key") or (prop.named_children[0] if prop.named_children else None)
                            val_node = prop.child_by_field_name("value") or (prop.named_children[1] if len(prop.named_children) > 1 else None)
                            key_text = _ts_decode(key, src_b).strip("\"'`") if key else ""
                            if key_text in ("queryKey", "mutationKey") and val_node and val_node.type == "array":
                                for elem in val_node.named_children:
                                    v, interp = _ts_string_value(elem, src_b)
                                    if v and _looks_like_http_url(v):
                                        method = "POST" if fn_text == "useMutation" else "GET"
                                        yield CallSite(method=method, url=v, library="react_query",
                                                       start_line=node.start_point[0] + 1,
                                                       interpolated=interp)
                                        break
        for c in node.named_children:
            yield from visit(c)

    yield from visit(tree.root_node)


def _walk_python_calls(source: str) -> Iterable[CallSite]:
    if not _HAVE_TS:
        return
    parser = _ts_get_parser("python")
    src_b = source.encode("utf-8", errors="replace")
    tree = parser.parse(src_b)

    def visit(node):
        if node.type == "call":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if not fn or not args or not args.named_children:
                for c in node.named_children:
                    yield from visit(c)
                return
            # 1. requests.get/post/..., httpx.get/post/..., session.get/post/...
            if fn.type == "attribute":
                obj = fn.child_by_field_name("object")
                attr = fn.child_by_field_name("attribute")
                if obj and attr:
                    obj_text = _ts_decode(obj, src_b).split(".")[-1].lower()
                    attr_text = _ts_decode(attr, src_b).lower()
                    if attr_text in _HTTP_VERBS:
                        if obj_text in ("requests", "httpx", "client", "session", "aiohttp"):
                            first = args.named_children[0]
                            val, interp = _py_string_value(first, src_b)
                            if val and _looks_like_http_url(val):
                                lib = obj_text if obj_text in ("requests", "httpx", "aiohttp") else "http_client"
                                yield CallSite(method=attr_text.upper(), url=val, library=lib,
                                               start_line=node.start_point[0] + 1,
                                               interpolated=interp)
                    elif attr_text == "urlopen":
                        first = args.named_children[0]
                        val, interp = _py_string_value(first, src_b)
                        if val and _looks_like_http_url(val):
                            yield CallSite(method="GET", url=val, library="urllib",
                                           start_line=node.start_point[0] + 1,
                                           interpolated=interp)
            for c in node.named_children:
                yield from visit(c)
            return
        for c in node.named_children:
            yield from visit(c)

    yield from visit(tree.root_node)


def extract_call_sites(source: str, language: str) -> list[CallSite]:
    """Public entry. Returns CallSite list for the language."""
    if language in ("typescript", "tsx", "javascript"):
        parser_lang = "tsx" if language == "tsx" else (
            "typescript" if language == "typescript" else "javascript"
        )
        return list(_walk_ts_calls(source, parser_lang))
    if language == "python":
        return list(_walk_python_calls(source))
    return []
