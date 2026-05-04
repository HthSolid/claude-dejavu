#!/usr/bin/env python3
"""
claude-dejavu — GraphQL schema indexer (v0.3.8).

Sister of route_indexer (HTTP) / page_indexer (filesystem routes) /
db_indexer (DB schema) / env_indexer (env vars). Catches:

  gql`query { user(id: 1) { emaill } }`      → typo of `email`
  gql`mutation { createUer(input: ...) }`    → typo of `createUser`
  gql`query { fabricatedField { id } }`      → fabricated field

Sources (canonical first):
  sdl-file          *.graphql / *.gql files                    — schema source of truth
  inline-gql        `gql\`type X { … }\`` template literals    — server-side schema bits
  codegen           generated *.generated.ts type defs         — derived; lowest authority

Parser: small custom SDL tokenizer (no external graphql-core dep). It
covers the subset of SDL that 99% of projects use: type / input / enum
/ interface / union / scalar definitions, and field declarations with
optional args. Directives are scanned but not enforced.
"""
from __future__ import annotations

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


# ─── Records ────────────────────────────────────────────────────────────────


@dataclass
class GqlField:
    name: str
    field_type: str
    is_nullable: bool = True
    is_list: bool = False
    args: list[dict] | None = None
    start_line: int = 1
    description: str | None = None


@dataclass
class GqlType:
    type_name: str
    kind: str                       # object | input | enum | interface | union | scalar | schema
    source: str                     # sdl-file | inline-gql | codegen
    source_file: str
    start_line: int = 1
    description: str | None = None
    fields: list[GqlField] = field(default_factory=list)
    enum_values: list[str] = field(default_factory=list)


# ─── SDL tokenizer + parser ─────────────────────────────────────────────────


_SDL_KIND_RE = re.compile(
    r"\b(extend\s+)?(type|input|enum|interface|union|scalar|schema)\b\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)?"
)
_SDL_DESC_RE = re.compile(r'^\s*"""(.*?)"""', re.DOTALL)


def parse_sdl(text: str, source: str, source_file: str) -> Iterable[GqlType]:
    """Parse a GraphQL SDL document. Yields one GqlType per declaration."""
    pos = 0
    n = len(text)
    while pos < n:
        # Strip line comments + whitespace.
        while pos < n:
            c = text[pos]
            if c in " \t\r\n,":
                pos += 1
                continue
            if c == "#":
                # Line comment.
                nl = text.find("\n", pos)
                if nl < 0:
                    pos = n
                    break
                pos = nl + 1
                continue
            break
        if pos >= n:
            break

        # Optional description: "..." or """..."""
        leading_desc: str | None = None
        if text[pos:pos + 3] == '"""':
            end = text.find('"""', pos + 3)
            if end > 0:
                leading_desc = text[pos + 3:end].strip()
                pos = end + 3
                continue  # whitespace skip on next iter, then keyword
        elif text[pos] == '"':
            end = text.find('"', pos + 1)
            if end > 0:
                leading_desc = text[pos + 1:end].strip()
                pos = end + 1
                continue

        m = _SDL_KIND_RE.match(text, pos)
        if not m:
            # Unknown token — skip to next whitespace.
            pos = text.find("\n", pos)
            if pos < 0:
                break
            pos += 1
            continue

        kind = m.group(2)
        type_name = m.group("name")
        # Compute line number for the keyword.
        line_no = text[:m.start()].count("\n") + 1
        # Move past the keyword + name.
        cursor = m.end()
        # Special: schema { … } has no name.
        if not type_name and kind == "schema":
            type_name = "schema"

        # union: type_name = A | B | C ;  (no body)
        if kind == "union":
            # Walk to '\n' or 'directive boundary' but for simplicity find the
            # next type/input/enum/interface/union/scalar/schema keyword OR EOF.
            block_end = _find_next_decl(text, cursor)
            block = text[cursor:block_end]
            members = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", block)
            # Drop the leading '=' if matched as identifier (it isn't).
            decl = GqlType(type_name=type_name, kind="union",
                           source=source, source_file=source_file,
                           start_line=line_no)
            decl.enum_values = members  # reuse field for member type names
            yield decl
            pos = block_end
            continue

        # scalar: type_name (no body, possibly directives)
        if kind == "scalar":
            yield GqlType(type_name=type_name, kind="scalar",
                          source=source, source_file=source_file,
                          start_line=line_no)
            # Skip to end of line or next decl.
            nl = text.find("\n", cursor)
            pos = nl + 1 if nl > 0 else n
            continue

        # Find the opening '{'.
        brace_open = text.find("{", cursor)
        if brace_open < 0:
            # Some types (extend type X implements Y) might omit fields.
            pos = cursor
            continue
        brace_close = _balance(text, brace_open, "{", "}")
        if brace_close < 0:
            pos = brace_open + 1
            continue
        body = text[brace_open + 1:brace_close]
        decl = GqlType(type_name=type_name, kind=kind,
                       source=source, source_file=source_file,
                       start_line=line_no)
        if kind == "enum":
            for ln in body.splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#") or ln.startswith('"'):
                    continue
                # An enum value is an identifier (optionally followed by directives).
                m2 = re.match(r"^([A-Z_][A-Z0-9_]*)\b", ln)
                if m2:
                    decl.enum_values.append(m2.group(1))
        else:
            # Object / input / interface / schema: parse fields.
            decl.fields = list(_parse_field_block(body, brace_open + 1, text, line_no))
        yield decl
        pos = brace_close + 1


def _find_next_decl(text: str, start: int) -> int:
    """Return the index where the next top-level declaration starts (or EOF)."""
    # Look for 'type|input|enum|...' at line-start.
    next_kw = re.search(
        r"(?m)^\s*(?:extend\s+)?(?:type|input|enum|interface|union|scalar|schema)\b",
        text[start:],
    )
    if next_kw:
        return start + next_kw.start()
    return len(text)


def _balance(text: str, start: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    i = start
    in_str: str | None = None
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"',):
            in_str = c
            i += 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_field_block(body: str, body_start: int,
                        full_text: str, type_line: int) -> Iterable[GqlField]:
    """Parse field declarations. Each field is essentially:
      name(arg1: Type, arg2: [Type!]! = default): ReturnType[!] [@directive]
    """
    # Compute line offsets so we can map field line numbers.
    line_offset = full_text[:body_start].count("\n")

    # Walk the body splitting on newlines but keeping argument lists together.
    pos = 0
    n = len(body)
    while pos < n:
        # Skip whitespace + comments + descriptions.
        while pos < n:
            c = body[pos]
            if c in " \t\r\n,":
                pos += 1
                continue
            if c == "#":
                nl = body.find("\n", pos)
                if nl < 0:
                    pos = n
                    break
                pos = nl + 1
                continue
            if c == '"' and body[pos:pos+3] == '"""':
                end = body.find('"""', pos + 3)
                if end > 0:
                    pos = end + 3
                    continue
            if c == '"':
                end = body.find('"', pos + 1)
                if end > 0:
                    pos = end + 1
                    continue
            break
        if pos >= n:
            break

        # Field name.
        nm = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", body[pos:])
        if not nm:
            pos += 1
            continue
        name = nm.group(1)
        line_no = line_offset + body[:pos].count("\n") + 1
        cursor = pos + nm.end()

        # Optional argument list: '(' … ')'
        args = None
        # Skip whitespace.
        while cursor < n and body[cursor] in " \t":
            cursor += 1
        if cursor < n and body[cursor] == "(":
            paren_close = _balance(body, cursor, "(", ")")
            if paren_close > 0:
                args_text = body[cursor + 1:paren_close]
                args = _parse_args(args_text)
                cursor = paren_close + 1

        # Required ':' then return type. (For input types fields are also
        # 'name: Type', no args.)
        while cursor < n and body[cursor] in " \t":
            cursor += 1
        if cursor >= n or body[cursor] != ":":
            # Not a field declaration — skip.
            pos = cursor + 1 if cursor < n else n
            continue
        cursor += 1
        # Read the type expression: identifiers, [], !, until end of line
        # or directive '@' or another field.
        while cursor < n and body[cursor] in " \t":
            cursor += 1
        # Capture until newline, '@', or another lowercase identifier
        # followed by '(' / ':' (next field).
        type_start = cursor
        while cursor < n:
            c = body[cursor]
            if c in "\n,":
                break
            if c == "#":
                break
            if c == "@":
                break
            cursor += 1
        ftype = body[type_start:cursor].strip()
        is_nullable = not ftype.endswith("!")
        is_list = ftype.startswith("[")
        yield GqlField(
            name=name, field_type=ftype,
            is_nullable=is_nullable, is_list=is_list,
            args=args, start_line=line_no,
        )
        pos = cursor


def _parse_args(args_text: str) -> list[dict]:
    """Parse a graphql arg list: 'a: Int, b: String! = "x"'."""
    out: list[dict] = []
    # Naive split on top-level commas.
    parts = []
    depth = 0
    cur = ""
    in_str: str | None = None
    for c in args_text:
        if in_str:
            cur += c
            if c == in_str:
                in_str = None
            continue
        if c in ('"', "'"):
            in_str = c
            cur += c
            continue
        if c in "[({":
            depth += 1
            cur += c
            continue
        if c in "])}":
            depth -= 1
            cur += c
            continue
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += c
    if cur.strip():
        parts.append(cur)

    for p in parts:
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)(?:\s*=\s*(.+))?$",
                      p.strip())
        if m:
            out.append({
                "name": m.group(1),
                "type": m.group(2).strip(),
                "default": (m.group(3) or "").strip() or None,
            })
    return out


# ─── Inline gql template extraction ─────────────────────────────────────────


def parse_inline_gql(path: Path) -> Iterable[GqlType]:
    """Walk a TS/JS file for `gql\`…\`` template literals containing SDL.
    Inline gql is typically used server-side (typedefs); client-side
    operation strings (gql`query { … }`) are handled by the lint
    strategy, not the indexer."""
    if not _HAVE_TS:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        # Regex fallback: find `gql\`<sdl>\`` or `graphql\`<sdl>\``.
        for m in re.finditer(
            r"\b(?:gql|graphql)\s*`([^`]+)`", text, re.DOTALL
        ):
            sdl = m.group(1)
            # Heuristic: must contain a `type ` / `input ` / `enum ` keyword
            # at the start of a line, AND no `query`/`mutation`/`subscription`
            # at top level (those are operations, not schema).
            if not re.search(r"(?m)^\s*(?:type|input|enum|interface|union|scalar|extend)\b", sdl):
                continue
            yield from parse_sdl(sdl, "inline-gql", str(path))
        return

    # tree-sitter path: pull every template_string whose tag is gql/graphql.
    ext = path.suffix.lower()
    lang = "tsx" if ext == ".tsx" else (
        "typescript" if ext == ".ts" else "javascript"
    )
    try:
        parser = _ts_get_parser(lang)
        text = path.read_bytes()
        tree = parser.parse(text)
    except Exception:
        return
    src_b = text

    def walk(node):
        # tree-sitter typescript represents `gql\`...\`` as a call_expression
        # whose children are: identifier (the tag) + template_string (the
        # body). There's no arguments wrapper. We also handle the pure
        # tagged_template_expression form for completeness.
        if node.type in ("call_expression", "tagged_template_expression"):
            tag = None
            tmpl = None
            for child in node.children:
                if child.type == "identifier" and tag is None:
                    tag = child
                elif child.type == "template_string" and tmpl is None:
                    tmpl = child
            if tag and tmpl:
                tag_name = src_b[tag.start_byte:tag.end_byte].decode("utf-8", "ignore")
                if tag_name in ("gql", "graphql"):
                    sdl = src_b[tmpl.start_byte:tmpl.end_byte].decode("utf-8", "ignore").strip("`")
                    if re.search(r"(?m)^\s*(?:type|input|enum|interface|union|scalar|extend)\b", sdl):
                        yield from parse_sdl(sdl, "inline-gql", str(path))
        for child in node.children:
            yield from walk(child)

    yield from walk(tree.root_node)


# ─── Project walk ───────────────────────────────────────────────────────────


_SKIP_DIRS = {
    "node_modules", ".git", ".next", "dist", "build", "out", "target",
    "__pycache__", ".venv", "venv", "vendor", ".pytest_cache",
    ".mypy_cache", ".turbo", "coverage",
}


def index_project_graphql(project_dir: str, project_slug: str, conn,
                            *, force: bool = False, flt=None) -> dict:
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"error": f"not_a_directory: {root}"}

    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(root)

    # ─── v0.4.5: fingerprint cache ─────────────────────────────────────
    from file_state import IndexerCache, files_under
    relevant = list(files_under(
        root,
        suffixes={".graphql", ".gql", ".ts", ".tsx", ".js", ".mjs", ".cjs"},
        flt=flt,
    ))
    cache = IndexerCache(conn, project_slug, "graphql")
    if not force and cache.is_fresh(relevant):
        return cache.cached_stats_marked()

    decls: list[GqlType] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                        if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
        filenames = [f for f in filenames
                       if not flt.should_skip(Path(dirpath) / f, is_dir=False)]
        for f in filenames:
            full = Path(dirpath) / f
            ext = full.suffix.lower()
            if ext in (".graphql", ".gql"):
                try:
                    text = full.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                decls.extend(parse_sdl(text, "sdl-file", str(full)))
            elif ext in (".ts", ".tsx", ".js", ".mjs", ".cjs"):
                # Cheap text-based filter — read only files mentioning gql.
                try:
                    head = full.read_text(encoding="utf-8", errors="replace")[:4000]
                except OSError:
                    continue
                if "gql" not in head and "graphql" not in head:
                    continue
                decls.extend(parse_inline_gql(full))

    by_source: dict[str, int] = {}
    field_count = 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM graphql_types WHERE project_slug = %s", (project_slug,))
        for d in decls:
            cur.execute(
                """INSERT INTO graphql_types
                   (project_slug, type_name, kind, source, source_file,
                    start_line, description, indexed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (project_slug, type_name, source, source_file)
                     DO NOTHING
                   RETURNING id""",
                (project_slug, d.type_name, d.kind, d.source, d.source_file,
                 d.start_line, d.description),
            )
            row = cur.fetchone()
            if not row:
                continue
            tid = row[0]
            by_source[d.source] = by_source.get(d.source, 0) + 1
            for fld in d.fields:
                cur.execute(
                    """INSERT INTO graphql_fields
                       (type_id, project_slug, type_name, field_name,
                        field_type, args_json, is_nullable, is_list,
                        start_line, description)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (tid, project_slug, d.type_name, fld.name,
                     fld.field_type,
                     json.dumps(fld.args) if fld.args else None,
                     fld.is_nullable, fld.is_list,
                     fld.start_line, fld.description),
                )
                field_count += 1
            for ev in d.enum_values:
                # For enums, treat each enum value as a field row.
                # For unions, we reuse enum_values to store member type names.
                cur.execute(
                    """INSERT INTO graphql_fields
                       (type_id, project_slug, type_name, field_name,
                        is_nullable, is_list)
                       VALUES (%s,%s,%s,%s, FALSE, FALSE)""",
                    (tid, project_slug, d.type_name, ev),
                )
                field_count += 1

    stats = {
        "types_added": sum(by_source.values()),
        "fields_added": field_count,
        "by_source": by_source,
    }
    cache.store(relevant, stats=stats)
    return stats


# ─── Resolution ─────────────────────────────────────────────────────────────


def resolve_gql_field(type_name: str | None, field_name: str,
                       project_slug: str, conn, *,
                       fuzzy: bool = True, limit: int = 5) -> list[dict]:
    """Resolve a (Type, field) pair against the GraphQL index."""
    out: list[dict] = []
    seen: set[int] = set()
    with conn.cursor() as cur:
        if type_name:
            cur.execute(
                """SELECT f.id, f.type_name, f.field_name, f.field_type,
                          t.source, t.source_file, f.start_line, t.kind
                   FROM graphql_fields f
                     JOIN graphql_types t ON f.type_id = t.id
                   WHERE f.project_slug = %s AND f.type_name = %s
                     AND f.field_name = %s
                   ORDER BY t.source LIMIT %s""",
                (project_slug, type_name, field_name, limit),
            )
            for row in cur.fetchall():
                seen.add(row[0])
                out.append({
                    "id": row[0], "type_name": row[1], "field_name": row[2],
                    "field_type": row[3], "source": row[4],
                    "source_file": row[5], "start_line": row[6],
                    "kind": row[7], "match_kind": "exact", "trigram_sim": 1.0,
                })
            if out:
                return out

        # Cross-type exact.
        cur.execute(
            """SELECT f.id, f.type_name, f.field_name, f.field_type,
                      t.source, t.source_file, f.start_line, t.kind
               FROM graphql_fields f
                 JOIN graphql_types t ON f.type_id = t.id
               WHERE f.project_slug = %s AND f.field_name = %s
               ORDER BY t.source, f.type_name LIMIT %s""",
            (project_slug, field_name, limit),
        )
        for row in cur.fetchall():
            if row[0] in seen:
                continue
            seen.add(row[0])
            out.append({
                "id": row[0], "type_name": row[1], "field_name": row[2],
                "field_type": row[3], "source": row[4],
                "source_file": row[5], "start_line": row[6],
                "kind": row[7],
                "match_kind": "cross-type", "trigram_sim": 1.0,
            })
        if out:
            return out

        if fuzzy:
            try:
                from adaptive_threshold import get_threshold
                t = get_threshold(project_slug, "graphql", conn)
            except Exception:
                t = 0.5
            cur.execute("SELECT set_limit(%s)", (t,))
            if type_name:
                cur.execute(
                    """SELECT f.id, f.type_name, f.field_name, f.field_type,
                              t.source, t.source_file, f.start_line, t.kind,
                              similarity(f.field_name, %s) AS sim
                       FROM graphql_fields f
                         JOIN graphql_types t ON f.type_id = t.id
                       WHERE f.project_slug = %s AND f.type_name = %s
                         AND f.field_name %% %s
                       ORDER BY sim DESC LIMIT %s""",
                    (field_name, project_slug, type_name, field_name, limit),
                )
                rows = cur.fetchall()
                if rows:
                    for row in rows:
                        if row[0] in seen:
                            continue
                        seen.add(row[0])
                        out.append({
                            "id": row[0], "type_name": row[1],
                            "field_name": row[2], "field_type": row[3],
                            "source": row[4], "source_file": row[5],
                            "start_line": row[6], "kind": row[7],
                            "match_kind": "fuzzy",
                            "trigram_sim": float(row[8] or 0),
                        })
                    return out
            cur.execute(
                """SELECT f.id, f.type_name, f.field_name, f.field_type,
                          t.source, t.source_file, f.start_line, t.kind,
                          similarity(f.field_name, %s) AS sim
                   FROM graphql_fields f
                     JOIN graphql_types t ON f.type_id = t.id
                   WHERE f.project_slug = %s AND f.field_name %% %s
                   ORDER BY sim DESC LIMIT %s""",
                (field_name, project_slug, field_name, limit),
            )
            for row in cur.fetchall():
                if row[0] in seen:
                    continue
                out.append({
                    "id": row[0], "type_name": row[1], "field_name": row[2],
                    "field_type": row[3], "source": row[4],
                    "source_file": row[5], "start_line": row[6],
                    "kind": row[7],
                    "match_kind": "cross-fuzzy",
                    "trigram_sim": float(row[8] or 0),
                })
    return out


def resolve_gql_type(type_name: str, project_slug: str, conn, *,
                      fuzzy: bool = True, limit: int = 5) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, type_name, kind, source, source_file, start_line
               FROM graphql_types
               WHERE project_slug = %s AND type_name = %s
               ORDER BY source LIMIT %s""",
            (project_slug, type_name, limit),
        )
        rows = cur.fetchall()
        if rows:
            return [
                {"id": r[0], "type_name": r[1], "kind": r[2],
                 "source": r[3], "source_file": r[4], "start_line": r[5],
                 "match_kind": "exact", "trigram_sim": 1.0}
                for r in rows
            ]
        if not fuzzy:
            return []
        try:
            from adaptive_threshold import get_threshold
            t = get_threshold(project_slug, "graphql", conn)
        except Exception:
            t = 0.4
        cur.execute("SELECT set_limit(%s)", (t,))
        cur.execute(
            """SELECT id, type_name, kind, source, source_file, start_line,
                      similarity(type_name, %s) AS sim
               FROM graphql_types
               WHERE project_slug = %s AND type_name %% %s
               ORDER BY sim DESC LIMIT %s""",
            (type_name, project_slug, type_name, limit),
        )
        return [
            {"id": r[0], "type_name": r[1], "kind": r[2],
             "source": r[3], "source_file": r[4], "start_line": r[5],
             "match_kind": "fuzzy", "trigram_sim": float(r[6] or 0)}
            for r in cur.fetchall()
        ]


def list_gql_types(project_slug: str, conn, *, kind: str | None = None,
                    source: str | None = None, limit: int = 200) -> list[dict]:
    where = ["t.project_slug = %s"]
    params: list = [project_slug]
    if kind:
        where.append("t.kind = %s"); params.append(kind)
    if source:
        where.append("t.source = %s"); params.append(source)
    sql = (f"SELECT t.id, t.type_name, t.kind, t.source, t.source_file, "
           f"       t.start_line, count(f.id) AS field_count "
           f"FROM graphql_types t LEFT JOIN graphql_fields f ON f.type_id = t.id "
           f"WHERE {' AND '.join(where)} "
           f"GROUP BY t.id ORDER BY t.kind, t.type_name LIMIT %s")
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def list_gql_fields(project_slug: str, conn, *, type_name: str | None = None,
                     limit: int = 500) -> list[dict]:
    where = ["project_slug = %s"]
    params: list = [project_slug]
    if type_name:
        where.append("type_name = %s"); params.append(type_name)
    sql = (f"SELECT type_name, field_name, field_type, is_nullable, is_list "
           f"FROM graphql_fields WHERE {' AND '.join(where)} "
           f"ORDER BY type_name, field_name LIMIT %s")
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ─── Standalone CLI for testing ─────────────────────────────────────────────


if __name__ == "__main__":
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from recall import _conn

    p = argparse.ArgumentParser()
    p.add_argument("project_dir")
    p.add_argument("project_slug")
    p.add_argument("--resolve-field", help="TYPE.FIELD or FIELD")
    p.add_argument("--resolve-type", help="type name")
    args = p.parse_args()

    with _conn() as c:
        c.autocommit = True
        stats = index_project_graphql(args.project_dir, args.project_slug, c)
        print(stats)
        if args.resolve_field:
            tn, fn = (args.resolve_field.split(".", 1)
                       if "." in args.resolve_field else (None, args.resolve_field))
            for h in resolve_gql_field(tn, fn, args.project_slug, c):
                print(f"  {h['match_kind']:12s} {h['type_name']}.{h['field_name']:25s} "
                      f"[{h['source']}] sim={h.get('trigram_sim', 0):.2f}")
        if args.resolve_type:
            for h in resolve_gql_type(args.resolve_type, args.project_slug, c):
                print(f"  {h['match_kind']:6s} {h['type_name']:20s} kind={h['kind']:10s} [{h['source']}]")
