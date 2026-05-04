#!/usr/bin/env python3
"""
claude-dejavu — page / file-route indexer (v0.3.5).

Sister of route_indexer.py — instead of HTTP API routes, this catches
the front-end "what URL does this file serve" mapping. Used by the
page_duplicate lint strategy to flag Claude creating a NEW page (or
layout / loader / error boundary) when one already exists for the
same goal.

Frameworks covered:

  Filesystem-based:
    Next.js pages router    pages/**/*.{tsx,jsx,ts,js}
    Next.js app router      app/**/page.{tsx,jsx,ts,js} + layout/route/error
    Remix                   app/routes/**/*.{tsx,jsx,ts,js}  (flat or nested)
    SvelteKit               src/routes/**/+page.svelte + +layout/+server
    Astro                   src/pages/**/*.{astro,mdx}

  Config-based:
    React Router            <Route path="..."> JSX OR createBrowserRouter([...])
    Vue Router              routes: [{ path: '/foo', component: ... }]

Path normalization:
  Next.js [id]      → :id
  Next.js [...rest] → *
  Remix $id         → :id
  SvelteKit [id]    → :id
  React/Vue :id     → :id   (already canonical)
  Astro [id].astro  → :id

  Group folders (Next.js (admin), Remix _index, etc.) collapse into the
  parent path. Trailing /index resolves to the parent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
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
class PageRecord:
    framework: str
    path_pattern: str
    raw_path: str
    kind: str                     # 'page' | 'layout' | 'api' | 'middleware' | 'loader' | 'error'
    source_file: str
    start_line: int = 1
    description: str | None = None


# ─── Path normalization ─────────────────────────────────────────────────────


def _normalize(path: str) -> str:
    """Canonicalize a route path so :id / {id} / [id] all collapse."""
    if not path:
        return path
    # Next.js [id] / [...slug]
    def _nextjs_repl(m):
        spread, name = m.groups()
        return "*" if spread else f":{name}"
    path = re.sub(r"\[\.{3}([^\]]+)\]|\[([^\]]+)\]", _nextjs_repl, path)
    # Remix $id  → :id
    path = re.sub(r"\$([A-Za-z_]\w*)", r":\1", path)
    # FastAPI/OpenAPI {id} → :id
    path = re.sub(r"\{([^}:]+)(?::[^}]*)?\}", r":\1", path)
    # Collapse double slashes
    path = re.sub(r"(?<!:)//+", "/", path)
    # Trim trailing slash unless root
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


# ─── Filesystem-based extractors ────────────────────────────────────────────


_NEXTJS_PAGE_EXTS = {".tsx", ".jsx", ".ts", ".js", ".mjs", ".cjs"}


def _is_nextjs_internal(name_no_ext: str) -> bool:
    """Skip Next.js internal pages: _app, _document, _error, _middleware."""
    return name_no_ext.startswith("_")


def _strip_group_segments(parts: list[str]) -> list[str]:
    """Drop Next.js / Remix route groups: (admin), (auth) — they don't
    affect the URL."""
    return [p for p in parts if not (p.startswith("(") and p.endswith(")"))]


def _scan_nextjs_pages(repo_root: Path, project_slug: str) -> Iterable[PageRecord]:
    """pages/**/*.{tsx,jsx,ts,js} → /path"""
    pages_dir = repo_root / "pages"
    if not pages_dir.is_dir():
        return
    for f in pages_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in _NEXTJS_PAGE_EXTS:
            continue
        rel = f.relative_to(pages_dir)
        parts = list(rel.parts)
        # Skip api/ — those are HTTP routes, handled by route_indexer
        if parts and parts[0] == "api":
            continue
        last = parts[-1]
        name_no_ext = os.path.splitext(last)[0]
        if _is_nextjs_internal(name_no_ext):
            continue
        if name_no_ext == "index":
            segments = parts[:-1]
        else:
            segments = parts[:-1] + [name_no_ext]
        segments = _strip_group_segments(segments)
        raw = "/" + "/".join(segments) if segments else "/"
        yield PageRecord(
            framework="nextjs-pages",
            path_pattern=_normalize(raw),
            raw_path=raw,
            kind="page",
            source_file=str(f),
        )


def _scan_nextjs_app(repo_root: Path, project_slug: str) -> Iterable[PageRecord]:
    """app/**/{page,layout,loading,error,not-found}.{tsx,jsx,ts,js}
    → /path with kind set per filename.
    """
    app_dir = repo_root / "app"
    if not app_dir.is_dir():
        return
    kind_by_name = {
        "page": "page", "layout": "layout", "loading": "loader",
        "error": "error", "not-found": "error",
        "template": "layout", "default": "page",
        "middleware": "middleware",
        # 'route' = HTTP handler, NOT a page — handled by route_indexer
    }
    for f in app_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in _NEXTJS_PAGE_EXTS:
            continue
        rel = f.relative_to(app_dir)
        parts = list(rel.parts)
        if not parts:
            continue
        last = parts[-1]
        name_no_ext = os.path.splitext(last)[0]
        if name_no_ext == "route":
            # API handler — out of scope for page_routes (covered by route_indexer)
            continue
        kind = kind_by_name.get(name_no_ext)
        if not kind:
            continue
        segments = _strip_group_segments(parts[:-1])
        raw = "/" + "/".join(segments) if segments else "/"
        yield PageRecord(
            framework="nextjs-app",
            path_pattern=_normalize(raw),
            raw_path=raw,
            kind=kind,
            source_file=str(f),
        )


def _scan_remix(repo_root: Path, project_slug: str) -> Iterable[PageRecord]:
    """app/routes/**/*.{tsx,jsx,ts,js} — flat (Remix v2) or nested.
    Examples:
      app/routes/index.tsx                  → /
      app/routes/about.tsx                  → /about
      app/routes/users.$id.tsx              → /users/:id     (flat)
      app/routes/users/$id.tsx              → /users/:id     (nested)
      app/routes/_index.tsx                 → /              (pathless)
    """
    base = repo_root / "app" / "routes"
    if not base.is_dir():
        return
    for f in base.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in _NEXTJS_PAGE_EXTS:
            continue
        rel = f.relative_to(base)
        parts = list(rel.parts)
        last = parts[-1]
        name_no_ext = os.path.splitext(last)[0]
        # Remix v2 flat routes: users.$id.tsx → users / $id
        if "." in name_no_ext and not name_no_ext.startswith("."):
            flat = name_no_ext.split(".")
            segments = parts[:-1] + flat
        else:
            segments = parts[:-1] + [name_no_ext]
        # _index → root of parent; _foo (pathless) → drop
        cleaned = []
        for seg in segments:
            if seg == "_index":
                continue  # represents the parent's root
            if seg.startswith("_"):
                continue  # pathless layout
            cleaned.append(seg)
        raw = "/" + "/".join(cleaned) if cleaned else "/"
        yield PageRecord(
            framework="remix",
            path_pattern=_normalize(raw),
            raw_path=raw,
            kind="page",
            source_file=str(f),
        )


def _scan_sveltekit(repo_root: Path, project_slug: str) -> Iterable[PageRecord]:
    """src/routes/**/+{page,layout,server,error,loading}.{svelte,ts,js}"""
    base = repo_root / "src" / "routes"
    if not base.is_dir():
        return
    kind_by_prefix = {
        "+page": "page", "+layout": "layout",
        "+server": "api", "+error": "error",
    }
    for f in base.rglob("*"):
        if not f.is_file():
            continue
        name = f.name
        # Match "+page.svelte", "+page.ts", "+layout.svelte", etc.
        if not name.startswith("+"):
            continue
        prefix = name.split(".", 1)[0]
        kind = kind_by_prefix.get(prefix)
        if not kind:
            continue
        rel = f.relative_to(base)
        segments = list(rel.parts[:-1])
        # SvelteKit groups: (admin) — same as Next.js
        segments = _strip_group_segments(segments)
        raw = "/" + "/".join(segments) if segments else "/"
        yield PageRecord(
            framework="sveltekit",
            path_pattern=_normalize(raw),
            raw_path=raw,
            kind=kind,
            source_file=str(f),
        )


def _scan_astro(repo_root: Path, project_slug: str) -> Iterable[PageRecord]:
    """src/pages/**/*.{astro,mdx,md}"""
    base = repo_root / "src" / "pages"
    if not base.is_dir():
        return
    for f in base.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in (".astro", ".mdx", ".md"):
            continue
        rel = f.relative_to(base)
        parts = list(rel.parts)
        last = parts[-1]
        name_no_ext = os.path.splitext(last)[0]
        if name_no_ext == "index":
            segments = parts[:-1]
        else:
            segments = parts[:-1] + [name_no_ext]
        raw = "/" + "/".join(segments) if segments else "/"
        yield PageRecord(
            framework="astro",
            path_pattern=_normalize(raw),
            raw_path=raw,
            kind="page",
            source_file=str(f),
        )


# ─── Config-based extractors (React Router + Vue Router) ────────────────────


def _ts_decode(node, src_b: bytes) -> str:
    return src_b[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _scan_react_router(repo_root: Path, project_slug: str,
                          flt=None) -> Iterable[PageRecord]:
    """Scan TS/TSX/JS files for <Route path="..." element={...}/> JSX
    and createBrowserRouter([{path: '...', element: ...}]) arrays.
    """
    if not _HAVE_TS:
        return
    parser_tsx = _ts_get_parser("tsx")
    parser_ts = _ts_get_parser("typescript")
    parser_js = _ts_get_parser("javascript")

    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(repo_root)
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames
                        if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
        filenames = [f for f in filenames
                       if not flt.should_skip(Path(dirpath) / f, is_dir=False)]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".tsx", ".ts", ".js", ".mjs", ".jsx"):
                continue
            full = Path(dirpath) / fn
            try:
                source = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Cheap pre-filter: must mention 'Route' or 'createBrowserRouter'
            if ("Route" not in source) and ("createBrowserRouter" not in source) \
                    and ("createMemoryRouter" not in source) and ("createHashRouter" not in source):
                continue
            parser = parser_tsx if ext == ".tsx" else (parser_ts if ext == ".ts" else parser_js)
            src_b = source.encode("utf-8", errors="replace")
            tree = parser.parse(src_b)
            for rec in _walk_react_router(tree.root_node, src_b, str(full)):
                yield rec


def _walk_react_router(node, src_b: bytes, source_file: str) -> Iterable[PageRecord]:
    """Walk a TS/TSX AST collecting React Router <Route path="..."> JSX
    elements and createBrowserRouter([...]) array literals."""
    t = node.type

    # JSX element: <Route path="..." element={...}/>
    if t in ("jsx_self_closing_element", "jsx_opening_element"):
        name_node = node.child_by_field_name("name")
        if name_node and _ts_decode(name_node, src_b) == "Route":
            path_value = None
            for attr in node.named_children:
                if attr.type == "jsx_attribute":
                    attr_name = attr.named_children[0] if attr.named_children else None
                    if attr_name and _ts_decode(attr_name, src_b) == "path":
                        for c in attr.named_children[1:]:
                            if c.type == "string":
                                txt = _ts_decode(c, src_b)
                                if len(txt) >= 2 and txt[0] in "\"'":
                                    path_value = txt[1:-1]
                                    break
                        break
            if path_value is not None:
                if not path_value.startswith("/"):
                    path_value = "/" + path_value
                yield PageRecord(
                    framework="react-router",
                    path_pattern=_normalize(path_value),
                    raw_path=path_value,
                    kind="page",
                    source_file=source_file,
                    start_line=node.start_point[0] + 1,
                )

    # createBrowserRouter([{path: '/foo', element: ...}, ...])
    if t == "call_expression":
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn and args and args.named_children:
            fn_name = _ts_decode(fn, src_b).split(".")[-1]
            if fn_name in ("createBrowserRouter", "createMemoryRouter",
                           "createHashRouter"):
                first = args.named_children[0]
                if first.type == "array":
                    yield from _walk_router_array(first, src_b, source_file, "react-router")

    for c in node.named_children:
        yield from _walk_react_router(c, src_b, source_file)


def _walk_router_array(arr_node, src_b: bytes, source_file: str,
                        framework: str) -> Iterable[PageRecord]:
    """For each object literal in the array, find a `path` field +
    optional nested children/`children: [...]`."""
    for elem in arr_node.named_children:
        if elem.type in ("object", "object_literal", "object_expression"):
            yield from _walk_router_object(elem, src_b, source_file, framework, prefix="")


def _walk_router_object(obj_node, src_b: bytes, source_file: str,
                          framework: str, prefix: str = "") -> Iterable[PageRecord]:
    path_str: str | None = None
    children_node = None
    for prop in obj_node.named_children:
        if prop.type in ("pair", "property", "object_property"):
            key = prop.child_by_field_name("key") or (prop.named_children[0] if prop.named_children else None)
            val = prop.child_by_field_name("value") or (prop.named_children[1] if len(prop.named_children) > 1 else None)
            key_text = _ts_decode(key, src_b).strip("\"'`") if key else ""
            if key_text == "path" and val is not None:
                if val.type == "string":
                    txt = _ts_decode(val, src_b)
                    if len(txt) >= 2 and txt[0] in "\"'":
                        path_str = txt[1:-1]
            elif key_text == "children" and val is not None and val.type == "array":
                children_node = val
    if path_str is not None:
        full = (prefix.rstrip("/") + "/" + path_str.lstrip("/")) if prefix else path_str
        if not full.startswith("/"):
            full = "/" + full
        yield PageRecord(
            framework=framework,
            path_pattern=_normalize(full),
            raw_path=full,
            kind="page",
            source_file=source_file,
            start_line=obj_node.start_point[0] + 1,
        )
        if children_node is not None:
            yield from _walk_router_array_with_prefix(children_node, src_b, source_file, framework, full)


def _walk_router_array_with_prefix(arr_node, src_b: bytes, source_file: str,
                                     framework: str, prefix: str) -> Iterable[PageRecord]:
    for elem in arr_node.named_children:
        if elem.type in ("object", "object_literal", "object_expression"):
            yield from _walk_router_object(elem, src_b, source_file, framework, prefix=prefix)


def _scan_vue_router(repo_root: Path, project_slug: str) -> Iterable[PageRecord]:
    """Vue Router config files. Look for routes: [{ path: '/foo', ... }]
    in router.{ts,js} or src/router/**/*.{ts,js}.
    """
    if not _HAVE_TS:
        return
    parser_ts = _ts_get_parser("typescript")
    parser_js = _ts_get_parser("javascript")

    candidates: list[Path] = []
    for fname in ("router.ts", "router.js", "router/index.ts", "router/index.js"):
        for d in (repo_root, repo_root / "src"):
            p = d / fname
            if p.is_file():
                candidates.append(p)
    src_router = repo_root / "src" / "router"
    if src_router.is_dir():
        for f in src_router.rglob("*.ts"):
            candidates.append(f)
        for f in src_router.rglob("*.js"):
            candidates.append(f)

    for full in candidates:
        try:
            source = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "routes" not in source:
            continue
        ext = full.suffix.lower()
        parser = parser_ts if ext == ".ts" else parser_js
        src_b = source.encode("utf-8", errors="replace")
        tree = parser.parse(src_b)
        for rec in _walk_vue_router(tree.root_node, src_b, str(full)):
            yield rec


def _walk_vue_router(node, src_b: bytes, source_file: str) -> Iterable[PageRecord]:
    """Find `routes: [...]` property anywhere in the AST."""
    if node.type in ("pair", "property", "object_property"):
        key = node.child_by_field_name("key") or (node.named_children[0] if node.named_children else None)
        val = node.child_by_field_name("value") or (node.named_children[1] if len(node.named_children) > 1 else None)
        key_text = _ts_decode(key, src_b).strip("\"'`") if key else ""
        if key_text == "routes" and val and val.type == "array":
            yield from _walk_router_array(val, src_b, source_file, "vue-router")
    for c in node.named_children:
        yield from _walk_vue_router(c, src_b, source_file)


# ─── Project walker / persistence ───────────────────────────────────────────


def index_project_pages(project_dir: str, project_slug: str, conn,
                          *, force: bool = False, flt=None) -> dict:
    """Walk a project, collect all PageRecords across the supported
    frameworks, persist to page_routes."""
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"error": f"not_a_directory: {root}"}

    # ─── v0.4.5: fingerprint cache ─────────────────────────────────────
    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(root)
    from file_state import IndexerCache, files_under
    relevant = list(files_under(
        root,
        suffixes={".tsx", ".jsx", ".ts", ".js", ".mjs", ".astro",
                   ".svelte", ".vue"},
        flt=flt,
    ))
    cache = IndexerCache(conn, project_slug, "page")
    if not force and cache.is_fresh(relevant):
        return cache.cached_stats_marked()

    records: list[PageRecord] = []
    records += list(_scan_nextjs_pages(root, project_slug))
    records += list(_scan_nextjs_app(root, project_slug))
    records += list(_scan_remix(root, project_slug))
    records += list(_scan_sveltekit(root, project_slug))
    records += list(_scan_astro(root, project_slug))
    records += list(_scan_react_router(root, project_slug, flt=flt))
    records += list(_scan_vue_router(root, project_slug))

    # Persist — full replace per project (simpler than per-file delta for
    # filesystem-routed pages where deletion = file-no-longer-on-disk).
    with conn.cursor() as cur:
        cur.execute("DELETE FROM page_routes WHERE project_slug = %s", (project_slug,))
        framework_counts: dict[str, int] = {}
        for r in records:
            cur.execute(
                """INSERT INTO page_routes
                   (project_slug, framework, path_pattern, raw_path, kind,
                    source_file, start_line, description, indexed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())""",
                (project_slug, r.framework, r.path_pattern, r.raw_path, r.kind,
                 r.source_file, r.start_line, r.description),
            )
            framework_counts[r.framework] = framework_counts.get(r.framework, 0) + 1

    stats = {
        "pages_added": len(records),
        "by_framework": framework_counts,
    }
    cache.store(relevant, stats=stats)
    return stats


# ─── Resolution ─────────────────────────────────────────────────────────────


def _path_pattern_matches(client_path: str, route_pattern: str) -> bool:
    """Same logic as route_indexer._path_pattern_matches — re-used here
    for page resolution."""
    if not client_path or not route_pattern:
        return False
    cs = [s for s in client_path.split("/") if s != ""]
    rs = [s for s in route_pattern.split("/") if s != ""]
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


def check_new_page(proposed_path: str, project_slug: str, conn,
                    *, fuzzy: bool = True, limit: int = 5) -> list[dict]:
    """Returns existing page_routes that would conflict with creating a
    page at `proposed_path`. Three layers of overlap detection:
      1. Exact normalized path match → high-confidence duplicate
      2. Pattern match (concrete proposed against parameterized existing)
         → high-confidence
      3. Fuzzy on path_pattern via pg_trgm → low-confidence near-name

    Returns ranked dicts with framework / path_pattern / source_file /
    match_kind / similarity.
    """
    norm = _normalize(proposed_path)
    seen: set[int] = set()
    out: list[dict] = []
    with conn.cursor() as cur:
        # 1. Exact
        cur.execute(
            """SELECT id, framework, path_pattern, raw_path, kind,
                      source_file, start_line, description
               FROM page_routes
               WHERE project_slug = %s AND path_pattern = %s
               ORDER BY (kind = 'page') DESC, framework
               LIMIT %s""",
            (project_slug, norm, limit),
        )
        for row in cur.fetchall():
            seen.add(row[0])
            out.append({
                "id": row[0], "framework": row[1], "path_pattern": row[2],
                "raw_path": row[3], "kind": row[4], "source_file": row[5],
                "start_line": row[6], "description": row[7],
                "match_kind": "exact", "trigram_sim": 1.0,
            })
        if out:
            return out

        # 2. Pattern match
        cur.execute(
            """SELECT id, framework, path_pattern, raw_path, kind,
                      source_file, start_line, description
               FROM page_routes
               WHERE project_slug = %s
                 AND (path_pattern LIKE %s OR path_pattern LIKE %s)
               ORDER BY length(path_pattern) DESC""",
            (project_slug, '%:%', '%*%'),
        )
        for row in cur.fetchall():
            if _path_pattern_matches(norm, row[2]):
                seen.add(row[0])
                out.append({
                    "id": row[0], "framework": row[1], "path_pattern": row[2],
                    "raw_path": row[3], "kind": row[4], "source_file": row[5],
                    "start_line": row[6], "description": row[7],
                    "match_kind": "pattern", "trigram_sim": 1.0,
                })
                if len(out) >= limit:
                    return out
        if out:
            return out

        # 3. Fuzzy
        if fuzzy:
            try:
                from adaptive_threshold import get_threshold
                t = get_threshold(project_slug, "page", conn)
            except Exception:
                t = 0.4
            cur.execute("SELECT set_limit(%s)", (t,))
            cur.execute(
                """SELECT id, framework, path_pattern, raw_path, kind,
                          source_file, start_line, description,
                          similarity(path_pattern, %s) AS sim
                   FROM page_routes
                   WHERE project_slug = %s
                     AND path_pattern %% %s
                   ORDER BY sim DESC
                   LIMIT %s""",
                (norm, project_slug, norm, limit),
            )
            for row in cur.fetchall():
                if row[0] in seen:
                    continue
                out.append({
                    "id": row[0], "framework": row[1], "path_pattern": row[2],
                    "raw_path": row[3], "kind": row[4], "source_file": row[5],
                    "start_line": row[6], "description": row[7],
                    "match_kind": "fuzzy", "trigram_sim": float(row[8] or 0),
                })
    return out


def list_pages(project_slug: str, conn, *,
                framework: str | None = None,
                kind: str | None = None,
                limit: int = 200,
                offset: int = 0) -> list[dict]:
    where = ["project_slug = %s"]
    params: list = [project_slug]
    if framework:
        where.append("framework = %s"); params.append(framework)
    if kind:
        where.append("kind = %s"); params.append(kind)
    sql = (f"SELECT framework, path_pattern, raw_path, kind, source_file, start_line "
           f"FROM page_routes WHERE {' AND '.join(where)} "
           f"ORDER BY path_pattern, framework LIMIT %s OFFSET %s")
    params += [limit, offset]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ─── Helper: derive a route from a proposed file path ──────────────────────


def file_path_to_route(file_path: str, project_root: str) -> tuple[str, str] | None:
    """If `file_path` lives under a routing-aware directory, derive the
    route the new file would serve. Returns (framework, route) or None.

    Used by the page_duplicate lint strategy: when Claude does Write to
    create a new file, we map that file path to its would-be route and
    check check_new_page() against existing pages.
    """
    rp = Path(file_path)
    root = Path(project_root)
    try:
        rel_parts = list(rp.relative_to(root).parts)
    except ValueError:
        # file_path isn't under project_root — bail
        return None

    # Next.js app router
    if "app" in rel_parts:
        idx = rel_parts.index("app")
        sub = rel_parts[idx + 1:]
        if sub and os.path.splitext(sub[-1])[0] in ("page", "layout"):
            segments = _strip_group_segments(sub[:-1])
            return ("nextjs-app", _normalize("/" + "/".join(segments) if segments else "/"))

    # Next.js pages router
    if "pages" in rel_parts:
        idx = rel_parts.index("pages")
        sub = rel_parts[idx + 1:]
        if sub and sub[0] != "api" and rp.suffix.lower() in _NEXTJS_PAGE_EXTS:
            last = sub[-1]
            name = os.path.splitext(last)[0]
            if not _is_nextjs_internal(name):
                segments = sub[:-1] if name == "index" else sub[:-1] + [name]
                segments = _strip_group_segments(segments)
                return ("nextjs-pages", _normalize("/" + "/".join(segments) if segments else "/"))

    # Remix
    if "routes" in rel_parts and rel_parts[rel_parts.index("routes") - 1: rel_parts.index("routes")] == ["app"]:
        idx = rel_parts.index("routes")
        sub = rel_parts[idx + 1:]
        if sub:
            last = sub[-1]
            name = os.path.splitext(last)[0]
            if "." in name and not name.startswith("."):
                flat = name.split(".")
                segments = sub[:-1] + flat
            else:
                segments = sub[:-1] + [name]
            cleaned = [s for s in segments if s != "_index" and not s.startswith("_")]
            return ("remix", _normalize("/" + "/".join(cleaned) if cleaned else "/"))

    # SvelteKit
    if "routes" in rel_parts and "src" in rel_parts:
        idx = rel_parts.index("routes")
        # Only valid if 'src' is the immediate parent
        if idx > 0 and rel_parts[idx - 1] == "src":
            sub = rel_parts[idx + 1:]
            if sub:
                last = sub[-1]
                if last.startswith("+"):
                    segments = _strip_group_segments(list(sub[:-1]))
                    return ("sveltekit", _normalize("/" + "/".join(segments) if segments else "/"))

    # Astro
    if "pages" in rel_parts and "src" in rel_parts:
        idx = rel_parts.index("pages")
        if idx > 0 and rel_parts[idx - 1] == "src":
            sub = rel_parts[idx + 1:]
            if sub and rp.suffix.lower() in (".astro", ".mdx", ".md"):
                last = sub[-1]
                name = os.path.splitext(last)[0]
                segments = sub[:-1] if name == "index" else sub[:-1] + [name]
                return ("astro", _normalize("/" + "/".join(segments) if segments else "/"))

    return None
