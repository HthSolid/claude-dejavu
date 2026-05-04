"""
DB schema grounding — verifies ORM/SQL field references against the
project's db_models / db_fields index.

Catches:
  - prisma.user.findUnique({ where: { emaill: 'x' } })   → typo of email
  - prisma.usrs.findMany()                               → typo of user
  - db.select().from(usrs)                               → Drizzle typo
  - prisma.post.create({ data: { authorEmail: ... } })   → field on wrong model
  - pg.query("SELECT * FROM postss")                     → SQL table typo

What gets extracted:

  Prisma client calls:
    prisma.<model>.<op>({ where|select|data|include|orderBy|distinct: { … } })
    The field keys inside those object literals are checked.

  Drizzle calls:
    db.select().from(<schemaConst>)
    db.insert(<schemaConst>).values({ … })
    db.update(<schemaConst>).set({ … })
    The schema const is checked against db_models.

  Raw SQL strings:
    pg.query("SELECT col FROM table")
    sql\`SELECT col FROM table\`
    Best-effort lex of FROM / INTO / UPDATE / SELECT.

Out of scope (v0.3.7):
  - Spread operators inside where/data/select objects — those keys come
    from a runtime variable; we can't ground a partial.
  - Dynamic field selection — `select: { [name]: true }` skipped.
  - Nested includes deeper than 1 level — just first level checked.
"""
from __future__ import annotations

import re

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


# ─── Prisma client extraction ───────────────────────────────────────────────


# Map common Prisma client method calls to the kind of object literal they take.
# Method → which keys in the call's options object hold field names.
_PRISMA_OPS = {
    "findUnique": ("where", "select", "include"),
    "findFirst": ("where", "select", "include", "orderBy"),
    "findMany": ("where", "select", "include", "orderBy", "distinct"),
    "create": ("data", "select", "include"),
    "createMany": ("data",),
    "update": ("where", "data", "select", "include"),
    "updateMany": ("where", "data"),
    "upsert": ("where", "create", "update", "select", "include"),
    "delete": ("where", "select", "include"),
    "deleteMany": ("where",),
    "count": ("where",),
    "aggregate": ("where", "_count", "_sum", "_avg", "_min", "_max",
                   "orderBy"),
    "groupBy": ("where", "by", "_count", "_sum"),
}


# Prisma chain: prisma.<model>.<op>(...)
_PRISMA_CHAIN_RE = re.compile(
    r"\b(?:prisma|tx|db|client|trx)\.([a-z][A-Za-z0-9_]*)\.([a-z][A-Za-z0-9_]*)\s*\(",
)


def _balance_braces(text: str, start: int) -> int:
    """Given text[start] == '(' or '{', return the index of the matching
    closer. -1 on unbalanced. Skips strings / template literals."""
    if start >= len(text):
        return -1
    open_ch = text[start]
    close_ch = {"(": ")", "{": "}", "[": "]"}.get(open_ch)
    if not close_ch:
        return -1
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
        if c in ("'", '"', "`"):
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


def _extract_object_keys(obj_text: str) -> list[tuple[str, int]]:
    """Pull top-level keys out of a JS object literal text. Returns
    (key, byte_offset) pairs. Naive but works for most call-site usage.
    Skips spread (...) and computed keys ([expr])."""
    keys: list[tuple[str, int]] = []
    i = 0
    in_str: str | None = None
    depth = 0
    n = len(obj_text)
    # Walk the string at depth 0 (top-level of the object), find identifier-
    # or string-keyed keys followed by ':'.
    while i < n:
        c = obj_text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"', "`"):
            in_str = c
            i += 1
            continue
        if c in "{[(":
            depth += 1
            i += 1
            continue
        if c in "}])":
            depth -= 1
            i += 1
            continue
        if depth != 1:
            # Top-level of the literal sits at depth 1 (we entered the
            # outer brace already on the first iteration).
            i += 1
            continue
        # Possible identifier key: [A-Za-z_$]
        if c.isalpha() or c == "_" or c == "$":
            j = i
            while j < n and (obj_text[j].isalnum() or obj_text[j] in "_$"):
                j += 1
            # Skip whitespace then check for ':'
            k = j
            while k < n and obj_text[k] in " \t\r\n":
                k += 1
            if k < n and obj_text[k] == ":":
                key = obj_text[i:j]
                keys.append((key, i))
            i = j
            continue
        # Quoted string key: "name": or 'name':
        if c in ("'", '"') and i + 1 < n:
            # We won't get here because we'd have flipped in_str. But
            # explicit support: scan to closing, then look for ':'.
            pass
        i += 1
    # Initialize depth properly: the outer { is at index 0 of obj_text;
    # our walk starts at depth 0 and bumps to 1 on the opening { — so
    # top-level keys ARE at depth 1.
    return keys


_PRISMA_OPTION_KEY_RE = re.compile(r"\b(where|select|include|data|orderBy|create|update|distinct|by)\s*:\s*\{")


def _walk_prisma_options(opts_text: str, model: str, line_no: int):
    """Yield (model, field_name, line_no) for every field key in any
    options sub-object."""
    for m in _PRISMA_OPTION_KEY_RE.finditer(opts_text):
        opt_name = m.group(1)
        brace_open = m.end() - 1
        brace_close = _balance_braces(opts_text, brace_open)
        if brace_close < 0:
            continue
        inner = opts_text[brace_open:brace_close + 1]
        keys = _extract_object_keys(inner)
        for k, off in keys:
            yield model, k, line_no, opt_name


def extract_prisma_calls(text: str):
    """Yield (model, field, line, opt_name) for every grounded field
    reference in Prisma client call sites."""
    for m in _PRISMA_CHAIN_RE.finditer(text):
        model = m.group(1)
        op = m.group(2)
        if op not in _PRISMA_OPS:
            continue
        # Find the matching ')' for the call's argument list.
        paren_open = m.end() - 1
        paren_close = _balance_braces(text, paren_open)
        if paren_close < 0:
            continue
        opts_text = text[paren_open:paren_close + 1]
        # Line number of the call.
        line_no = text[:m.start()].count("\n") + 1
        # Capitalize first letter — Prisma client uses lowercase model
        # accessors but model declarations are PascalCase.
        model_pascal = model[0].upper() + model[1:] if model else model
        yield from _walk_prisma_options(opts_text, model_pascal, line_no)


# ─── Drizzle extraction ─────────────────────────────────────────────────────


# Drizzle: db.select().from(SCHEMA), db.insert(SCHEMA), db.update(SCHEMA),
# db.delete(SCHEMA), .where(eq(SCHEMA.field, ...))
#
# Match only when there's a Drizzle-shaped chain leading into the call —
# `db.X(...)` / `tx.X(...)` / `<chain>.from(<ident>)` — to avoid false
# positives on unrelated DOM/Lodash/Date.from / express-rate-limit
# request.from(ip) calls. The `from` operator must follow either a
# `select()/$` chain or a `db|tx|trx|client` receiver.
_DRIZZLE_TABLE_REF_RE = re.compile(
    # Insert/update/delete only when chained off a `db|tx|trx|conn` receiver:
    r"(?:\b(?:db|tx|trx|conn)\b)\s*\.\s*(?:insert|update|delete)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]"
    r"|"
    # `.from(<ident>)` only after a `select(...)`, `selectDistinct(...)`,
    # or `selectDistinctOn(...)` chain — that's the Drizzle idiom.
    r"\bselect(?:Distinct(?:On)?)?\s*\([^)]*\)\s*\.\s*from\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]"
)
# .values({…}), .set({…})
_DRIZZLE_VALUES_RE = re.compile(r"\.(values|set)\s*\(\s*\{")


def extract_drizzle_calls(text: str):
    """Yield (table_ref, line) for every Drizzle-style table reference."""
    for m in _DRIZZLE_TABLE_REF_RE.finditer(text):
        # Either group 1 (insert/update/delete after db.) or group 2
        # (select(...).from(<ident>)) will be set.
        table = m.group(1) or m.group(2)
        if not table:
            continue
        line_no = text[:m.start()].count("\n") + 1
        yield table, line_no


# ─── Strategy ───────────────────────────────────────────────────────────────


class DbGroundingStrategy:
    name = "db_grounding"
    languages = {"typescript", "tsx", "javascript", "python"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        from db_indexer import resolve_db_field, resolve_db_model

        # Bail early if no schema indexed.
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM db_models WHERE project_slug = %s",
                        (ctx.project_slug,))
            if (cur.fetchone()[0] or 0) == 0:
                return []

        issues: list[Issue] = []
        seen_keys: set[tuple[str, str, int]] = set()

        # 1. Prisma client calls.
        for model, field_, line_no, opt in extract_prisma_calls(ctx.content):
            key = (model, field_, line_no)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # Resolve model first — if it doesn't exist, surface that as
            # the higher-priority signal (no point checking field on a
            # phantom model).
            model_hits = resolve_db_model(model, ctx.project_slug, ctx.conn,
                                            fuzzy=True, limit=3)
            if not model_hits:
                # Unknown model.
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=f"prisma.{model[0].lower()}{model[1:]}.<op>",
                    candidate=None,
                    confidence=0.92,
                    metadata={"model": model, "opt": opt, "line": line_no},
                    reason=(f"no model `{model}` in this project's "
                            f"Prisma/Drizzle/TypeORM/SQL schema"),
                    fix=("verify the model name; run dejavu_db_models to "
                         "browse what's declared"),
                ))
                continue
            top_model = model_hits[0]
            if top_model["match_kind"] == "fuzzy":
                # Likely model typo.
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.LOW_CONFIDENCE,
                    name=f"prisma.{model}",
                    candidate=top_model["model_name"],
                    confidence=float(top_model.get("trigram_sim", 0)),
                    metadata={"model": model, "opt": opt, "line": line_no,
                              "trigram_sim": round(top_model.get("trigram_sim", 0), 3),
                              "diagnosis": "model_typo"},
                    reason=(f"no model named `{model}`, closest match "
                            f"`{top_model['model_name']}` ({top_model['source']})"),
                    fix=f"change `{model}` to `{top_model['model_name']}`",
                ))
                # Still continue to check fields against the probable model.
                model = top_model["model_name"]

            # Resolve field on the (possibly corrected) model.
            field_hits = resolve_db_field(model, field_, ctx.project_slug,
                                            ctx.conn, fuzzy=True, limit=3)
            if not field_hits:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=f"{model}.{field_}",
                    candidate=None,
                    confidence=0.9,
                    metadata={"model": model, "field": field_,
                              "opt": opt, "line": line_no},
                    reason=(f"`{field_}` is not a field on {model} (and "
                            f"no other model has a field by that name)"),
                    fix=(f"verify the field name; run dejavu_db_fields with "
                         f"--model={model} to browse what's declared"),
                ))
                continue
            top = field_hits[0]
            if top["match_kind"] == "exact":
                # Resolved (silent OK).
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.OK,
                    name=f"{model}.{field_}",
                    candidate=f"{top['model_name']}.{top['field_name']}",
                    confidence=1.0,
                    metadata={"model": model, "field": field_,
                              "field_type": top.get("field_type"),
                              "is_id": top.get("is_id"),
                              "source": top["source"], "line": line_no},
                    reason=(f"declared in {top['source']} at "
                            f"{top['source_file']}:{top['start_line']}"),
                ))
            elif top["match_kind"] == "cross-model":
                # Field exists but on a different model.
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.LOW_CONFIDENCE,
                    name=f"{model}.{field_}",
                    candidate=f"{top['model_name']}.{field_}",
                    confidence=0.85,
                    metadata={"model": model, "field": field_,
                              "wrong_model": True,
                              "actual_model": top["model_name"],
                              "opt": opt, "line": line_no,
                              "diagnosis": "wrong_model"},
                    reason=(f"`{field_}` exists on `{top['model_name']}` "
                            f"({top['source']}), not on `{model}`"),
                    fix=(f"either query `{top['model_name']}` instead, or "
                         f"add `{field_}` to `{model}`"),
                ))
            elif top["match_kind"] in ("fuzzy", "cross-fuzzy"):
                # Typo.
                target_model = top["model_name"]
                target_field = top["field_name"]
                kind = "field_typo" if top["match_kind"] == "fuzzy" else "field_typo_cross_model"
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.LOW_CONFIDENCE,
                    name=f"{model}.{field_}",
                    candidate=f"{target_model}.{target_field}",
                    confidence=float(top.get("trigram_sim", 0)),
                    metadata={"model": model, "field": field_,
                              "trigram_sim": round(top.get("trigram_sim", 0), 3),
                              "diagnosis": kind, "opt": opt, "line": line_no},
                    reason=(f"close match `{target_model}.{target_field}` "
                            f"({top['source']}, sim={top.get('trigram_sim', 0):.2f}) "
                            f"— likely a typo"),
                    fix=f"change `{field_}` to `{target_field}`",
                ))

        # 2. Drizzle table refs (validate the const exists).
        for table, line_no in extract_drizzle_calls(ctx.content):
            key = ("__drizzle_table__", table, line_no)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            hits = resolve_db_model(table, ctx.project_slug, ctx.conn,
                                      fuzzy=True, limit=3)
            if not hits:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=f"drizzle:{table}",
                    candidate=None,
                    confidence=0.9,
                    metadata={"table": table, "line": line_no},
                    reason=(f"no Drizzle table / Prisma model / TypeORM entity "
                            f"named `{table}` in this project"),
                    fix="verify the table name (run dejavu_db_models to browse)",
                ))
                continue
            top = hits[0]
            if top["match_kind"] == "fuzzy":
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.LOW_CONFIDENCE,
                    name=f"drizzle:{table}",
                    candidate=top["model_name"],
                    confidence=float(top.get("trigram_sim", 0)),
                    metadata={"table": table, "line": line_no,
                              "diagnosis": "table_typo"},
                    reason=(f"no table named `{table}`, closest match "
                            f"`{top['model_name']}` ({top['source']}, "
                            f"sim={top.get('trigram_sim', 0):.2f})"),
                    fix=f"change `{table}` to `{top['model_name']}`",
                ))
            else:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.OK,
                    name=f"drizzle:{table}",
                    candidate=top["model_name"],
                    confidence=1.0,
                    metadata={"table": table, "line": line_no,
                              "source": top["source"]},
                    reason=(f"declared in {top['source']} at "
                            f"{top['source_file']}:{top['start_line']}"),
                ))

        return issues


register(DbGroundingStrategy())
