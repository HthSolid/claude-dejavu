"""
GraphQL grounding — verifies client-side `gql\`query|mutation { … }\``
operations against the project's graphql_types / graphql_fields index.

Catches:
  gql`query { user(id: 1) { emaill } }`   → typo of `email`
  gql`mutation { createUer(...) }`        → typo of `createUser`
  gql`{ totallyFakeField { id } }`        → fabricated

The strategy:
  1. Pull every `gql\`…\`` template from the proposed content.
  2. Skip ones that look like SCHEMA (type/input/enum/etc.) — those
     are server-side typedefs and aren't queries to validate.
  3. Parse the remainder as operations: find selection sets, walk
     fields, resolve each against the parent type.

Out of scope (v0.3.8):
  - Argument shape validation (we know the args list but don't yet
    enforce types / required-ness on call sites).
  - Fragment spread resolution — fragments are detected but their
    on-type is just trusted.
  - Inline fragments / __typename special handling.
"""
from __future__ import annotations

import re

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


_GQL_TAG_RE = re.compile(r"\b(?:gql|graphql)\s*`([^`]*)`", re.DOTALL)


def _extract_gql_blocks(text: str) -> list[tuple[str, int]]:
    """Pull `gql\`…\`` template body + start line."""
    out: list[tuple[str, int]] = []
    for m in _GQL_TAG_RE.finditer(text):
        body = m.group(1)
        line_no = text[:m.start()].count("\n") + 1
        out.append((body, line_no))
    return out


_OP_HEAD_RE = re.compile(
    r"\b(query|mutation|subscription)\b(?:\s+([A-Za-z_][A-Za-z0-9_]*))?",
)
_FIELD_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*(?::\s*[A-Za-z_][A-Za-z0-9_]*\s*)?(?:\{|\b)",
)


def _strip_args(text: str) -> str:
    """Replace top-level (...) arg lists with an empty space, so a
    naive field walker doesn't mistake arg names for fields."""
    out = []
    depth = 0
    for c in text:
        if c == "(":
            depth += 1
            out.append(" ")
            continue
        if c == ")":
            if depth > 0:
                depth -= 1
            out.append(" ")
            continue
        if depth == 0:
            out.append(c)
        else:
            out.append(" ")
    return "".join(out)


def _walk_selections(body: str, parent_type: str,
                      gql_block_start_line: int,
                      type_for_field: callable = None):
    """Walk a brace-balanced selection set body and yield every
    (parent_type, field_name, line_no) that should be grounded.

    `body` is the contents BETWEEN { and } of the operation root or
    nested selection. Fragment spreads (`...UserFragment`), inline
    fragments (`... on User { … }`), and aliases (`alias: field`) are
    handled.

    `type_for_field`: optional callable (parent_type, field_name) →
    return-type-name (with `!` and `[]` stripped). If provided, nested
    selections walk into the correct parent type. If None, nested
    selections inherit the same parent (degraded mode).
    """
    stripped = _strip_args(body)
    pos = 0
    n = len(stripped)
    parent_stack: list[str] = [parent_type]
    # We need to remember the field that was JUST seen so when we
    # encounter a `{` we can switch parent_type to that field's return
    # type.
    last_field_for_descent: str | None = None
    while pos < n:
        c = stripped[pos]
        if c in " \t\r\n,":
            pos += 1
            continue
        if c == "#":
            nl = stripped.find("\n", pos)
            pos = nl + 1 if nl >= 0 else n
            continue
        if c == "{":
            # Descend: the new parent is the return type of the last
            # field we walked. If the field's return type can't be
            # resolved (unknown / fabricated parent), push a sentinel
            # — the walker will skip every nested identifier so we
            # don't double-flag children of an already-flagged parent.
            new_parent = parent_stack[-1]
            if type_for_field and last_field_for_descent:
                resolved = type_for_field(parent_stack[-1],
                                            last_field_for_descent)
                if resolved:
                    new_parent = resolved
                else:
                    new_parent = "__UNRESOLVED__"
            parent_stack.append(new_parent)
            last_field_for_descent = None
            pos += 1
            continue
        if c == "}":
            if len(parent_stack) > 1:
                parent_stack.pop()
            pos += 1
            continue
        if c == "." and stripped[pos:pos + 3] == "...":
            # Fragment spread or inline fragment — skip. We don't
            # validate fragment names; they'll be ground-truthed when
            # parsed at the operation level.
            pos += 3
            # Skip identifier or 'on TypeName'
            while pos < n and stripped[pos] in " \t":
                pos += 1
            if stripped[pos:pos + 2] == "on":
                pos += 2
                while pos < n and stripped[pos] in " \t":
                    pos += 1
                m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", stripped[pos:])
                if m:
                    pos += m.end()
            else:
                m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", stripped[pos:])
                if m:
                    pos += m.end()
            continue
        # Identifier — possibly alias: field
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", stripped[pos:])
        if not m:
            pos += 1
            continue
        ident = m.group(0)
        cursor = pos + m.end()
        # If next token (after whitespace) is ':', this is an alias.
        ws = cursor
        while ws < n and stripped[ws] in " \t":
            ws += 1
        if ws < n and stripped[ws] == ":":
            # Alias — the real field is after the colon.
            cursor = ws + 1
            while cursor < n and stripped[cursor] in " \t":
                cursor += 1
            m2 = re.match(r"[A-Za-z_][A-Za-z0-9_]*", stripped[cursor:])
            if m2:
                ident = m2.group(0)
                cursor += m2.end()
        # Compute line number of this field within the original gql block.
        # Note: we walk the stripped text but offsets line up because we
        # only replaced parens with spaces (preserving newlines).
        local_line = stripped[:pos].count("\n")
        # Skip yielding when our parent is the unresolved sentinel —
        # we already flagged the ancestor that landed us here.
        if parent_stack[-1] != "__UNRESOLVED__":
            yield parent_stack[-1], ident, gql_block_start_line + local_line
        last_field_for_descent = ident
        pos = cursor


def _operation_root_type(op_kind: str) -> str:
    if op_kind == "mutation":
        return "Mutation"
    if op_kind == "subscription":
        return "Subscription"
    return "Query"


def _parse_gql_operation(block: str, block_line: int,
                          type_for_field: callable = None):
    """Yield (parent_type, field_name, line_no) for every field selection
    in the operation. Skips schema typedefs."""
    # If the block looks like SCHEMA (has 'type X {' / 'input ' / 'enum ' /
    # 'extend ' at the start of a line), it's not an operation. Skip.
    if re.search(r"(?m)^\s*(?:type|input|enum|interface|union|scalar|extend|schema)\b", block):
        return
    # Find operation head + body. If there's no explicit 'query'/'mutation'
    # keyword, it's an anonymous query — find the outermost { … }.
    op_head = _OP_HEAD_RE.search(block)
    op_kind = op_head.group(1) if op_head else "query"
    parent = _operation_root_type(op_kind)
    # Find the outermost selection set.
    brace_open = block.find("{")
    if brace_open < 0:
        return
    # Find matching close.
    depth = 0
    i = brace_open
    in_str: str | None = None
    while i < len(block):
        c = block[i]
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
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        return
    body = block[brace_open + 1:i]
    # Compute the line offset of the body inside the original block.
    body_start_line = block_line + block[:brace_open + 1].count("\n")
    yield from _walk_selections(body, parent, body_start_line,
                                  type_for_field=type_for_field)


class GraphqlGroundingStrategy:
    name = "graphql_grounding"
    languages = {"typescript", "tsx", "javascript", "python"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        from graphql_indexer import resolve_gql_field, resolve_gql_type

        # Bail early if no schema indexed.
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM graphql_types WHERE project_slug = %s",
                        (ctx.project_slug,))
            if (cur.fetchone()[0] or 0) == 0:
                return []

        blocks = _extract_gql_blocks(ctx.content)
        if not blocks:
            return []

        # Build a (type_name, field_name) -> return_type lookup so the
        # selection walker can descend into the right parent type when
        # encountering nested selection sets.
        type_for_field_cache: dict[tuple[str, str], str] = {}

        def type_for_field(parent: str, fld: str) -> str | None:
            key = (parent, fld)
            if key in type_for_field_cache:
                return type_for_field_cache[key]
            with ctx.conn.cursor() as cur:
                cur.execute(
                    """SELECT field_type FROM graphql_fields
                       WHERE project_slug = %s AND type_name = %s
                         AND field_name = %s
                       LIMIT 1""",
                    (ctx.project_slug, parent, fld),
                )
                row = cur.fetchone()
            if not row or not row[0]:
                type_for_field_cache[key] = None
                return None
            # Strip [] and ! from the type expression.
            base = (row[0] or "").strip().rstrip("!").lstrip("[").rstrip("]").rstrip("!")
            type_for_field_cache[key] = base or None
            return base or None

        issues: list[Issue] = []
        seen: set[tuple[str, str, int]] = set()

        for body, line_no in blocks:
            for parent_type, field_, line in _parse_gql_operation(
                    body, line_no, type_for_field=type_for_field):
                key = (parent_type, field_, line)
                if key in seen:
                    continue
                seen.add(key)

                # First, does the parent type exist?
                # (Skip the check for the implicit Query/Mutation roots —
                # they always "exist" by spec.)
                root_type = parent_type in ("Query", "Mutation", "Subscription")

                hits = resolve_gql_field(parent_type, field_, ctx.project_slug,
                                          ctx.conn, fuzzy=True, limit=3)
                if not hits:
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.UNKNOWN,
                        name=f"{parent_type}.{field_}",
                        candidate=None,
                        confidence=0.9,
                        metadata={"parent_type": parent_type,
                                  "field": field_, "line": line},
                        reason=(f"GraphQL field `{field_}` not declared on "
                                f"`{parent_type}` (and no other type has it)"),
                        fix=(f"verify the field name; run dejavu_gql_fields "
                             f"--type={parent_type} to browse declared fields"),
                    ))
                    continue

                top = hits[0]
                if top["match_kind"] == "exact":
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.OK,
                        name=f"{parent_type}.{field_}",
                        candidate=f"{top['type_name']}.{top['field_name']}",
                        confidence=1.0,
                        metadata={"parent_type": parent_type, "field": field_,
                                  "field_type": top.get("field_type"),
                                  "source": top["source"], "line": line},
                        reason=(f"declared in {top['source']} at "
                                f"{top['source_file']}:{top['start_line']}"),
                    ))
                elif top["match_kind"] == "cross-type":
                    # Field exists but on a different parent type.
                    # If the asked parent was a root operation type and the
                    # match landed on Query/Mutation, treat it as exact.
                    if root_type and top["type_name"] in ("Query", "Mutation",
                                                            "Subscription"):
                        issues.append(Issue(
                            strategy=self.name,
                            severity=IssueSeverity.OK,
                            name=f"{parent_type}.{field_}",
                            candidate=f"{top['type_name']}.{top['field_name']}",
                            confidence=1.0,
                            metadata={"parent_type": parent_type,
                                      "field": field_,
                                      "source": top["source"], "line": line},
                            reason=(f"declared on {top['type_name']} "
                                    f"({top['source']}) at "
                                    f"{top['source_file']}:{top['start_line']}"),
                        ))
                    else:
                        issues.append(Issue(
                            strategy=self.name,
                            severity=IssueSeverity.LOW_CONFIDENCE,
                            name=f"{parent_type}.{field_}",
                            candidate=f"{top['type_name']}.{field_}",
                            confidence=0.85,
                            metadata={"parent_type": parent_type,
                                      "field": field_,
                                      "actual_type": top["type_name"],
                                      "diagnosis": "wrong_type",
                                      "line": line},
                            reason=(f"`{field_}` exists on `{top['type_name']}`, "
                                    f"not on `{parent_type}`"),
                            fix=(f"either query `{top['type_name']}` instead, "
                                 f"or add `{field_}` to `{parent_type}`"),
                        ))
                elif top["match_kind"] in ("fuzzy", "cross-fuzzy"):
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.LOW_CONFIDENCE,
                        name=f"{parent_type}.{field_}",
                        candidate=f"{top['type_name']}.{top['field_name']}",
                        confidence=float(top.get("trigram_sim", 0)),
                        metadata={"parent_type": parent_type,
                                  "field": field_,
                                  "trigram_sim": round(top.get("trigram_sim", 0), 3),
                                  "diagnosis": "field_typo",
                                  "line": line},
                        reason=(f"close match `{top['type_name']}.{top['field_name']}` "
                                f"({top['source']}, "
                                f"sim={top.get('trigram_sim', 0):.2f})"),
                        fix=f"change `{field_}` to `{top['field_name']}`",
                    ))

        return issues


register(GraphqlGroundingStrategy())
