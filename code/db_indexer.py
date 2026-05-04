#!/usr/bin/env python3
"""
claude-dejavu — database schema indexer (v0.3.7).

Catches the "Claude generates `prisma.user.findUnique({ where: { emaill:
... }})`" class of bugs by indexing the project's canonical database
schema and grounding ORM call sites against it.

Sources (decreasing authority — but all canonical, equally trusted):

  prisma            schema.prisma — `model X { name Type @attrs }`
  drizzle           pgTable("users", { id: serial(), … }) / sqliteTable / mysqlTable
  typeorm           @Entity() classes with @Column / @PrimaryColumn etc.
  sequelize         define('User', { id: { type: ... } })
  mongoose          new Schema({ … })  (best-effort — Mongoose is loose)
  sql-migration     CREATE TABLE / ALTER TABLE in *.sql migration files

Resolution semantics in db_grounding.py:
  exact match for (model, field) → resolved (silent)
  exact match for field on a DIFFERENT model than the call →
    cross-model warning ("`email` exists on User, not Post")
  fuzzy field match (sim ≥ 0.6) → low_confidence (typo)
  no match → unknown (block) — fabricated field
"""
from __future__ import annotations

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
class FieldDecl:
    name: str
    field_type: str | None = None
    is_required: bool = True
    is_id: bool = False
    is_unique: bool = False
    is_relation: bool = False
    related_model: str | None = None
    start_line: int = 1
    description: str | None = None


@dataclass
class ModelDecl:
    model_name: str
    source: str
    source_file: str
    start_line: int = 1
    table_name: str | None = None
    description: str | None = None
    fields: list[FieldDecl] = field(default_factory=list)


# ─── Prisma schema parser ───────────────────────────────────────────────────


_PRISMA_MODEL_RE = re.compile(
    r"^\s*model\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{",
)
_PRISMA_END_RE = re.compile(r"^\s*\}\s*$")
_PRISMA_FIELD_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)"           # field name
    r"\s+([A-Za-z_][A-Za-z0-9_]*(?:\[\])?\??)"  # type with optional [] / ?
    r"(.*)$"                                    # remainder = attributes
)
_PRISMA_MAP_RE = re.compile(r'@@map\(\s*"([^"]+)"\s*\)')


def parse_prisma_schema(path: Path) -> Iterable[ModelDecl]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _PRISMA_MODEL_RE.match(line)
        if not m:
            i += 1
            continue
        model_name = m.group(1)
        start_line = i + 1
        # Pre-comment: capture leading /// or // lines.
        desc: str | None = None
        j = i - 1
        while j >= 0 and lines[j].strip().startswith("///"):
            desc = lines[j].strip().lstrip("/").strip()
            j -= 1

        decl = ModelDecl(model_name=model_name, source="prisma",
                         source_file=str(path), start_line=start_line,
                         description=desc)
        i += 1
        while i < len(lines):
            ln = lines[i]
            if _PRISMA_END_RE.match(ln):
                break
            stripped = ln.strip()
            if not stripped or stripped.startswith("//"):
                i += 1
                continue
            # Skip block-level Prisma directives.
            if stripped.startswith("@@"):
                # Check for table mapping
                mm = _PRISMA_MAP_RE.search(stripped)
                if mm:
                    decl.table_name = mm.group(1)
                i += 1
                continue
            fm = _PRISMA_FIELD_RE.match(ln)
            if fm:
                fname = fm.group(1)
                ftype_raw = fm.group(2)
                attrs = fm.group(3)
                # Optionality: trailing ?
                is_required = not ftype_raw.endswith("?")
                ftype = ftype_raw.rstrip("?").rstrip()
                is_relation_attr = "@relation" in attrs
                # Heuristic: capitalized type = a model reference (relation)
                is_relation = (is_relation_attr
                                or (ftype and ftype[0].isupper()
                                    and ftype not in {"String", "Int",
                                                       "BigInt", "Float",
                                                       "Boolean", "DateTime",
                                                       "Decimal", "Bytes",
                                                       "Json"}))
                decl.fields.append(FieldDecl(
                    name=fname,
                    field_type=ftype_raw,
                    is_required=is_required,
                    is_id="@id" in attrs,
                    is_unique="@unique" in attrs,
                    is_relation=bool(is_relation),
                    related_model=ftype.rstrip("[]").rstrip("?") if is_relation else None,
                    start_line=i + 1,
                ))
            i += 1
        yield decl
        i += 1


# ─── Drizzle parser (TS via tree-sitter) ────────────────────────────────────


_DRIZZLE_TABLE_FNS = {"pgTable", "sqliteTable", "mysqlTable", "mssqlTable"}


def _ts_text(node, src_b: bytes) -> str:
    return src_b[node.start_byte:node.end_byte].decode("utf-8", "ignore")


def parse_drizzle_schema(path: Path) -> Iterable[ModelDecl]:
    if not _HAVE_TS:
        return
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

    # Walk: find every variable_declarator where the value is a call to
    # pgTable / sqliteTable / etc. The first arg is the SQL table name,
    # the second is the column object.
    def walk(node):
        # Direct match: pattern is `const NAME = pgTable("table", { … })`
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if name_node and value_node and value_node.type == "call_expression":
                fn = value_node.child_by_field_name("function")
                args = value_node.child_by_field_name("arguments")
                if fn and args:
                    fn_name = _ts_text(fn, src_b).split(".")[-1]
                    if fn_name in _DRIZZLE_TABLE_FNS:
                        const_name = _ts_text(name_node, src_b)
                        # First arg = table name string, second = field object
                        arg_children = [c for c in args.children
                                         if c.type not in (",", "(", ")")]
                        table_name = None
                        fields_node = None
                        if arg_children:
                            first = arg_children[0]
                            if first.type == "string":
                                table_name = _ts_text(first, src_b).strip("\"'`")
                        if len(arg_children) >= 2:
                            fields_node = arg_children[1]
                        decl = ModelDecl(
                            model_name=const_name,
                            source="drizzle",
                            source_file=str(path),
                            start_line=node.start_point[0] + 1,
                            table_name=table_name,
                        )
                        if fields_node and fields_node.type == "object":
                            for pair in fields_node.children:
                                if pair.type != "pair":
                                    continue
                                key_node = pair.child_by_field_name("key")
                                val_node = pair.child_by_field_name("value")
                                if not key_node:
                                    continue
                                fname = _ts_text(key_node, src_b).strip("\"'`")
                                ftype = None
                                is_required = True
                                is_id = False
                                is_unique = False
                                is_relation = False
                                if val_node and val_node.type == "call_expression":
                                    inner_fn = val_node.child_by_field_name("function")
                                    if inner_fn:
                                        ftype = _ts_text(inner_fn, src_b).split(".")[-1].split("(")[0]
                                        # Walk the chain for primaryKey / unique / notNull
                                        chain = _ts_text(val_node, src_b)
                                        is_id = ".primaryKey(" in chain
                                        is_unique = ".unique(" in chain
                                        is_required = ".notNull(" in chain
                                decl.fields.append(FieldDecl(
                                    name=fname,
                                    field_type=ftype,
                                    is_required=is_required,
                                    is_id=is_id,
                                    is_unique=is_unique,
                                    is_relation=is_relation,
                                    start_line=pair.start_point[0] + 1,
                                ))
                        yield decl
                        return
        for child in node.children:
            yield from walk(child)

    yield from walk(tree.root_node)


# ─── TypeORM @Entity parser ─────────────────────────────────────────────────


def parse_typeorm_entities(path: Path) -> Iterable[ModelDecl]:
    if not _HAVE_TS:
        return
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

    def walk(node, parent=None):
        # class_declaration with @Entity decorator. Decorators in TS
        # tree-sitter are SIBLINGS of class_declaration (both inside
        # export_statement), not children — so we widen the text we check
        # to include the parent's source range.
        if node.type == "class_declaration":
            class_text = _ts_text(node, src_b)
            check_text = (_ts_text(parent, src_b) if parent is not None
                           else class_text)
            if "@Entity" in check_text or "@ViewEntity" in check_text:
                name_node = node.child_by_field_name("name")
                if not name_node:
                    return
                class_name = _ts_text(name_node, src_b)
                # Try to extract @Entity("table_name")
                table_name = None
                m = re.search(r'@(?:Entity|ViewEntity)\(\s*"([^"]+)"', check_text)
                if m:
                    table_name = m.group(1)

                decl = ModelDecl(
                    model_name=class_name, source="typeorm",
                    source_file=str(path),
                    start_line=node.start_point[0] + 1,
                    table_name=table_name,
                )
                # Walk the class body for field declarations. Decorators
                # are SIBLINGS of public_field_definition inside class_body
                # — accumulate them and attach to the next field def.
                body = node.child_by_field_name("body")
                if body:
                    pending_decorators: list[str] = []
                    for child in body.children:
                        if child.type == "decorator":
                            pending_decorators.append(_ts_text(child, src_b))
                            continue
                        if child.type not in ("public_field_definition",
                                                "field_definition",
                                                "property_definition",
                                                "method_definition"):
                            continue
                        decorator_blob = "\n".join(pending_decorators)
                        pending_decorators = []
                        # Need at least one TypeORM column decorator.
                        if not re.search(
                            r"@(?:Column|PrimaryColumn|PrimaryGeneratedColumn"
                            r"|CreateDateColumn|UpdateDateColumn|DeleteDateColumn"
                            r"|VersionColumn|OneToMany|ManyToOne|ManyToMany|OneToOne|JoinColumn|JoinTable)",
                            decorator_blob,
                        ):
                            continue
                        name_n = child.child_by_field_name("name")
                        if not name_n:
                            continue
                        fname = _ts_text(name_n, src_b)
                        is_id = ("@PrimaryColumn" in decorator_blob
                                  or "@PrimaryGeneratedColumn" in decorator_blob)
                        is_unique = ("unique: true" in decorator_blob
                                      or "@Unique" in decorator_blob)
                        is_relation = bool(re.search(
                            r"@(?:OneToMany|ManyToOne|ManyToMany|OneToOne)",
                            decorator_blob,
                        ))
                        ftype = None
                        ann = child.child_by_field_name("type")
                        if ann:
                            ftype = _ts_text(ann, src_b).lstrip(":").strip()
                        decl.fields.append(FieldDecl(
                            name=fname,
                            field_type=ftype,
                            is_required="nullable: true" not in decorator_blob,
                            is_id=is_id,
                            is_unique=is_unique,
                            is_relation=is_relation,
                            start_line=child.start_point[0] + 1,
                        ))
                yield decl
        for child in node.children:
            yield from walk(child, node)

    yield from walk(tree.root_node, None)


# ─── SQL migration parser ───────────────────────────────────────────────────


_SQL_CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?(?P<schema>[A-Za-z_][\w]*)"?\.)?"?(?P<table>[A-Za-z_][\w]*)"?\s*\((?P<body>.*?)\)\s*(?:;|$)',
    re.IGNORECASE | re.DOTALL,
)
_SQL_ALTER_TABLE_ADD_RE = re.compile(
    r'ALTER\s+TABLE\s+"?(?P<table>[A-Za-z_][\w]*)"?\s+ADD\s+COLUMN\s+'
    r'"?(?P<column>[A-Za-z_][\w]*)"?\s+(?P<rest>[^;\n]+)',
    re.IGNORECASE,
)
_SQL_COL_LINE_RE = re.compile(
    r'^\s*"?(?P<name>[A-Za-z_][\w]*)"?\s+(?P<type>[A-Za-z]+(?:\([^)]*\))?(?:\s+[A-Za-z]+)?)\s*(?P<rest>.*?)$',
)


def parse_sql_migration(path: Path) -> Iterable[ModelDecl]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    # CREATE TABLE statements.
    for m in _SQL_CREATE_TABLE_RE.finditer(text):
        table = m.group("table")
        body = m.group("body")
        # Compute approximate start line.
        start_line = text[:m.start()].count("\n") + 1
        decl = ModelDecl(
            model_name=table, source="sql-migration",
            source_file=str(path), start_line=start_line,
            table_name=table,
        )
        # Walk lines inside the parenthesized body.
        for raw_ln in body.split(","):
            ln = raw_ln.strip()
            if not ln or ln.startswith("--"):
                continue
            # Skip table-level constraints.
            up = ln.upper()
            if up.startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE",
                              "CHECK", "CONSTRAINT", "INDEX")):
                continue
            cm = _SQL_COL_LINE_RE.match(ln)
            if not cm:
                continue
            cname = cm.group("name")
            ctype = cm.group("type")
            rest = cm.group("rest").upper()
            decl.fields.append(FieldDecl(
                name=cname, field_type=ctype,
                is_required="NOT NULL" in rest,
                is_id="PRIMARY KEY" in rest,
                is_unique="UNIQUE" in rest,
                is_relation="REFERENCES" in rest,
            ))
        yield decl


# ─── Project walk ───────────────────────────────────────────────────────────


_SKIP_DIRS = {
    "node_modules", ".git", ".next", "dist", "build", "out", "target",
    "__pycache__", ".venv", "venv", "vendor", ".pytest_cache",
    ".mypy_cache", ".turbo", "coverage",
}


def index_project_db(project_dir: str, project_slug: str, conn,
                      *, force: bool = False, flt=None) -> dict:
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"error": f"not_a_directory: {root}"}

    decls: list[ModelDecl] = []

    # Build the ignore filter once per project; replaces the legacy
    # hardcoded _SKIP_DIRS plus picks up `.dejavuignore` rules.
    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(root)

    # ─── v0.4.5: fingerprint cache ─────────────────────────────────────
    from file_state import IndexerCache, files_under
    relevant = list(files_under(
        root,
        suffixes={".prisma", ".sql", ".ts", ".tsx", ".js", ".mjs", ".cjs"},
        flt=flt,
    ))
    cache = IndexerCache(conn, project_slug, "db")
    if not force and cache.is_fresh(relevant):
        return cache.cached_stats_marked()

    # Walk for schema files.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                        if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
        filenames = [f for f in filenames
                       if not flt.should_skip(Path(dirpath) / f, is_dir=False)]
        for f in filenames:
            full = Path(dirpath) / f
            ext = full.suffix.lower()
            name = f.lower()

            # Prisma — *.prisma files anywhere
            if ext == ".prisma":
                decls.extend(parse_prisma_schema(full))
                continue

            # SQL migrations — files in a path containing 'migration' or
            # ending in *.sql
            if ext == ".sql":
                decls.extend(parse_sql_migration(full))
                continue

            # TS/JS — Drizzle + TypeORM live here
            if ext in (".ts", ".tsx", ".js", ".mjs", ".cjs"):
                # Cheap text-based gate so we don't tree-sitter every file.
                try:
                    head = full.read_text(encoding="utf-8", errors="replace")[:4000]
                except OSError:
                    continue
                if any(fn + "(" in head for fn in _DRIZZLE_TABLE_FNS):
                    decls.extend(parse_drizzle_schema(full))
                if "@Entity" in head or "@ViewEntity" in head:
                    decls.extend(parse_typeorm_entities(full))

    # Persist — full-replace per project for simplicity.
    by_source: dict[str, int] = {}
    field_count = 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM db_models WHERE project_slug = %s", (project_slug,))
        for d in decls:
            cur.execute(
                """INSERT INTO db_models
                   (project_slug, model_name, source, source_file, start_line,
                    table_name, description, indexed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (project_slug, model_name, source, source_file)
                     DO NOTHING
                   RETURNING id""",
                (project_slug, d.model_name, d.source, d.source_file,
                 d.start_line, d.table_name, d.description),
            )
            row = cur.fetchone()
            if not row:
                continue
            model_id = row[0]
            by_source[d.source] = by_source.get(d.source, 0) + 1
            for fld in d.fields:
                cur.execute(
                    """INSERT INTO db_fields
                       (model_id, project_slug, model_name, field_name,
                        field_type, is_required, is_id, is_unique,
                        is_relation, related_model, start_line, description)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (model_id, project_slug, d.model_name, fld.name,
                     fld.field_type, fld.is_required, fld.is_id,
                     fld.is_unique, fld.is_relation, fld.related_model,
                     fld.start_line, fld.description),
                )
                field_count += 1

    stats = {
        "models_added": sum(by_source.values()),
        "fields_added": field_count,
        "by_source": by_source,
    }
    cache.store(relevant, stats=stats)
    return stats


# ─── Resolution ─────────────────────────────────────────────────────────────


def resolve_db_field(model_name: str | None, field_name: str,
                      project_slug: str, conn, *,
                      fuzzy: bool = True, limit: int = 5) -> list[dict]:
    """Resolve a (model, field) pair against the schema index. If
    `model_name` is None, search across all models.

    Match kinds returned:
      exact        → same model, exact field name
      cross-model  → exact field name but on a different model
      fuzzy        → trigram-similar field name on the named model
      cross-fuzzy  → trigram-similar field name on a different model
    """
    out: list[dict] = []
    seen: set[int] = set()
    with conn.cursor() as cur:
        # 1. Exact field on the named model (if model given).
        if model_name:
            cur.execute(
                """SELECT f.id, m.model_name, m.source, m.source_file,
                          f.field_name, f.field_type, f.is_id, f.is_unique,
                          f.is_relation, f.related_model, f.start_line
                   FROM db_fields f JOIN db_models m ON f.model_id = m.id
                   WHERE f.project_slug = %s AND f.model_name = %s
                     AND f.field_name = %s
                   ORDER BY m.source LIMIT %s""",
                (project_slug, model_name, field_name, limit),
            )
            for row in cur.fetchall():
                seen.add(row[0])
                out.append({
                    "id": row[0], "model_name": row[1], "source": row[2],
                    "source_file": row[3], "field_name": row[4],
                    "field_type": row[5], "is_id": row[6], "is_unique": row[7],
                    "is_relation": row[8], "related_model": row[9],
                    "start_line": row[10],
                    "match_kind": "exact", "trigram_sim": 1.0,
                })
            if out:
                return out

        # 2. Exact field on ANY model (cross-model — useful when caller
        # didn't know the model or got it wrong).
        cur.execute(
            """SELECT f.id, m.model_name, m.source, m.source_file,
                      f.field_name, f.field_type, f.is_id, f.is_unique,
                      f.is_relation, f.related_model, f.start_line
               FROM db_fields f JOIN db_models m ON f.model_id = m.id
               WHERE f.project_slug = %s AND f.field_name = %s
               ORDER BY m.source, m.model_name LIMIT %s""",
            (project_slug, field_name, limit),
        )
        for row in cur.fetchall():
            if row[0] in seen:
                continue
            seen.add(row[0])
            out.append({
                "id": row[0], "model_name": row[1], "source": row[2],
                "source_file": row[3], "field_name": row[4],
                "field_type": row[5], "is_id": row[6], "is_unique": row[7],
                "is_relation": row[8], "related_model": row[9],
                "start_line": row[10],
                "match_kind": "cross-model", "trigram_sim": 1.0,
            })
        if out:
            return out

        # 3. Fuzzy on field_name, scoped to model if given.
        if fuzzy:
            try:
                from adaptive_threshold import get_threshold
                t = get_threshold(project_slug, "db", conn)
            except Exception:
                t = 0.5
            cur.execute("SELECT set_limit(%s)", (t,))
            if model_name:
                cur.execute(
                    """SELECT f.id, m.model_name, m.source, m.source_file,
                              f.field_name, f.field_type, f.is_id, f.is_unique,
                              f.is_relation, f.related_model, f.start_line,
                              similarity(f.field_name, %s) AS sim
                       FROM db_fields f JOIN db_models m ON f.model_id = m.id
                       WHERE f.project_slug = %s AND f.model_name = %s
                         AND f.field_name %% %s
                       ORDER BY sim DESC LIMIT %s""",
                    (field_name, project_slug, model_name, field_name, limit),
                )
                rows = cur.fetchall()
                if rows:
                    for row in rows:
                        if row[0] in seen:
                            continue
                        seen.add(row[0])
                        out.append({
                            "id": row[0], "model_name": row[1], "source": row[2],
                            "source_file": row[3], "field_name": row[4],
                            "field_type": row[5], "is_id": row[6],
                            "is_unique": row[7], "is_relation": row[8],
                            "related_model": row[9], "start_line": row[10],
                            "match_kind": "fuzzy",
                            "trigram_sim": float(row[11] or 0),
                        })
                    return out
            # Fall through to project-wide fuzzy.
            cur.execute(
                """SELECT f.id, m.model_name, m.source, m.source_file,
                          f.field_name, f.field_type, f.is_id, f.is_unique,
                          f.is_relation, f.related_model, f.start_line,
                          similarity(f.field_name, %s) AS sim
                   FROM db_fields f JOIN db_models m ON f.model_id = m.id
                   WHERE f.project_slug = %s AND f.field_name %% %s
                   ORDER BY sim DESC LIMIT %s""",
                (field_name, project_slug, field_name, limit),
            )
            for row in cur.fetchall():
                if row[0] in seen:
                    continue
                out.append({
                    "id": row[0], "model_name": row[1], "source": row[2],
                    "source_file": row[3], "field_name": row[4],
                    "field_type": row[5], "is_id": row[6], "is_unique": row[7],
                    "is_relation": row[8], "related_model": row[9],
                    "start_line": row[10],
                    "match_kind": "cross-fuzzy",
                    "trigram_sim": float(row[11] or 0),
                })
    return out


def resolve_db_model(model_name: str, project_slug: str, conn, *,
                      fuzzy: bool = True, limit: int = 5) -> list[dict]:
    """Resolve a model/table name."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, model_name, source, source_file, start_line,
                      table_name, description
               FROM db_models
               WHERE project_slug = %s AND model_name = %s
               ORDER BY source LIMIT %s""",
            (project_slug, model_name, limit),
        )
        rows = cur.fetchall()
        if rows:
            return [
                {"id": r[0], "model_name": r[1], "source": r[2],
                 "source_file": r[3], "start_line": r[4],
                 "table_name": r[5], "description": r[6],
                 "match_kind": "exact", "trigram_sim": 1.0}
                for r in rows
            ]
        if not fuzzy:
            return []
        try:
            from adaptive_threshold import get_threshold
            t = get_threshold(project_slug, "db", conn)
        except Exception:
            t = 0.4
        cur.execute("SELECT set_limit(%s)", (t,))
        cur.execute(
            """SELECT id, model_name, source, source_file, start_line,
                      table_name, description, similarity(model_name, %s) AS sim
               FROM db_models
               WHERE project_slug = %s AND model_name %% %s
               ORDER BY sim DESC LIMIT %s""",
            (model_name, project_slug, model_name, limit),
        )
        return [
            {"id": r[0], "model_name": r[1], "source": r[2],
             "source_file": r[3], "start_line": r[4],
             "table_name": r[5], "description": r[6],
             "match_kind": "fuzzy", "trigram_sim": float(r[7] or 0)}
            for r in cur.fetchall()
        ]


def list_db_models(project_slug: str, conn, *, source: str | None = None,
                    limit: int = 200) -> list[dict]:
    where = ["m.project_slug = %s"]
    params: list = [project_slug]
    if source:
        where.append("m.source = %s")
        params.append(source)
    sql = (f"SELECT m.id, m.model_name, m.source, m.source_file, m.start_line, "
           f"       m.table_name, count(f.id) AS field_count "
           f"FROM db_models m LEFT JOIN db_fields f ON f.model_id = m.id "
           f"WHERE {' AND '.join(where)} "
           f"GROUP BY m.id ORDER BY m.model_name LIMIT %s")
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def list_db_fields(project_slug: str, conn, *, model_name: str | None = None,
                    limit: int = 500) -> list[dict]:
    where = ["project_slug = %s"]
    params: list = [project_slug]
    if model_name:
        where.append("model_name = %s")
        params.append(model_name)
    sql = (f"SELECT model_name, field_name, field_type, is_id, is_unique, "
           f"       is_relation, related_model "
           f"FROM db_fields WHERE {' AND '.join(where)} "
           f"ORDER BY model_name, field_name LIMIT %s")
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ─── CLI for standalone testing ─────────────────────────────────────────────


if __name__ == "__main__":
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from recall import _conn

    p = argparse.ArgumentParser(description="Standalone db_indexer for testing.")
    p.add_argument("project_dir")
    p.add_argument("project_slug")
    p.add_argument("--resolve-field", help="MODEL.FIELD or just FIELD")
    p.add_argument("--resolve-model", help="model name to resolve")
    args = p.parse_args()

    with _conn() as c:
        c.autocommit = True
        stats = index_project_db(args.project_dir, args.project_slug, c)
        print(stats)
        if args.resolve_field:
            mn, fn = (args.resolve_field.split(".", 1)
                       if "." in args.resolve_field else (None, args.resolve_field))
            hits = resolve_db_field(mn, fn, args.project_slug, c)
            print(f"resolve_field({mn!r}, {fn!r}) →")
            for h in hits:
                print(f"  {h['match_kind']:12s} {h['model_name']}.{h['field_name']:25s} "
                      f"[{h['source']}]  sim={h.get('trigram_sim', 0):.2f}")
        if args.resolve_model:
            hits = resolve_db_model(args.resolve_model, args.project_slug, c)
            print(f"resolve_model({args.resolve_model!r}) →")
            for h in hits:
                print(f"  {h['match_kind']:6s} {h['model_name']:25s} [{h['source']}]")
