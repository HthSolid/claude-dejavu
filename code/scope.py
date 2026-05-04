"""
scope.py — resolve user × workspace × project for a recall query.

Resolution chain (last-wins):
  1. explicit --scope=X flag                           (highest)
  2. <cwd>/.claude-dejavu.json                         (per-repo, committable)
  3. cwd belongs to a defined workspace W   → W
  4. fallback                                          → 'project' (just this repo)

User isolation:
  - Default DB is claude_dejavu_$USER (separate Postgres database per Linux user).
  - Weaviate tenant = $USER.
  - --shared install puts everyone in claude_dejavu_team / tenant=team.

Returns a `ScopeFilter` object the recall layer applies as:
  - PG:       WHERE project_slug = ANY(filter.projects)  AND os_user = ANY(filter.users)
  - Weaviate: where: { operator: And, operands: [
                 { path: ["project_slug"], operator: ContainsAny, valueText: [...] },
                 { path: ["os_user"], operator: Equal, valueText: ... }] }
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_CONFIG = ".claude-dejavu.json"


@dataclass
class ScopeFilter:
    """Resolved scope for a recall query."""
    projects: list[str] | None  # None = all projects (global)
    excludes: list[str] = field(default_factory=list)
    users:    list[str] = field(default_factory=list)  # empty = current user only
    label:    str = "project"  # human-readable for the response: "project:foo" / "ws:bar" / "global"

    def explain(self) -> str:
        bits = [self.label]
        if self.users:
            bits.append(f"users={','.join(self.users)}")
        if self.excludes:
            bits.append(f"excl={','.join(self.excludes)}")
        return " ".join(bits)


def _slug_from_cwd(cwd: str) -> str:
    return os.path.basename(os.path.realpath(cwd))


def _read_repo_config(cwd: str) -> dict:
    """Walk up from cwd looking for a .claude-dejavu.json. Returns {} if none."""
    p = Path(cwd).resolve()
    for d in [p, *p.parents]:
        candidate = d / REPO_CONFIG
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text())
            except Exception:
                return {}
        # Stop at filesystem root or home dir
        if d == d.parent or str(d) == str(Path.home()):
            break
    return {}


def _workspace_for_project(slug: str, conn) -> tuple[str, list[str]] | None:
    """Find a workspace containing this project. Returns (name, projects) or None."""
    if not conn:
        return None
    cur = conn.cursor()
    cur.execute("SELECT name, projects FROM workspaces WHERE %s = ANY(projects) LIMIT 1", (slug,))
    row = cur.fetchone()
    if row:
        return row[0], list(row[1] or [])
    return None


def parse_scope_arg(scope_arg: str | None) -> tuple[str, str | None]:
    """Parse --scope into (kind, name).
    Examples: 'global' -> ('global', None), 'workspace:foo' -> ('workspace','foo'),
              'project:bar' -> ('project','bar'), 'project' -> ('project', None) [use cwd]
    """
    if not scope_arg:
        return ("auto", None)
    s = scope_arg.strip().lower()
    if s in ("global", "all"):
        return ("global", None)
    m = re.match(r"^(project|workspace|ws)(?::(.+))?$", s)
    if m:
        kind = "workspace" if m.group(1) == "ws" else m.group(1)
        return (kind, m.group(2))
    # bare value -> project name
    return ("project", scope_arg)


def resolve_scope(
    cli_scope_arg: str | None,
    cwd: str | None = None,
    pg_conn=None,
    explicit_excludes: list[str] | None = None,
) -> ScopeFilter:
    cwd = cwd or os.getcwd()
    cwd_slug = _slug_from_cwd(cwd)
    excludes = list(explicit_excludes or [])
    repo_cfg = _read_repo_config(cwd)
    repo_excludes = repo_cfg.get("exclude_projects") or []
    excludes.extend(repo_excludes)

    kind, name = parse_scope_arg(cli_scope_arg)

    # 1. explicit CLI flag wins
    if kind == "global":
        return ScopeFilter(projects=None, excludes=excludes, label="global")
    if kind == "project" and name:
        return ScopeFilter(projects=[name], excludes=excludes, label=f"project:{name}")
    if kind == "project" and not name:
        return ScopeFilter(projects=[cwd_slug], excludes=excludes, label=f"project:{cwd_slug}")
    if kind == "workspace":
        ws_name = name or repo_cfg.get("workspace")
        if ws_name and pg_conn:
            ws = _workspace_for_project_by_name(ws_name, pg_conn)
            if ws:
                return ScopeFilter(projects=ws[1], excludes=excludes, label=f"ws:{ws_name}")
        return ScopeFilter(projects=[cwd_slug], excludes=excludes,
                           label=f"project:{cwd_slug} (workspace '{ws_name}' not found)")

    # 2. per-repo config
    if "scope" in repo_cfg:
        return resolve_scope(repo_cfg["scope"], cwd=cwd, pg_conn=pg_conn,
                             explicit_excludes=excludes)

    # 3. cwd belongs to a defined workspace?
    if pg_conn:
        ws = _workspace_for_project(cwd_slug, pg_conn)
        if ws:
            return ScopeFilter(projects=ws[1], excludes=excludes, label=f"ws:{ws[0]}")

    # 4. fallback: project
    return ScopeFilter(projects=[cwd_slug], excludes=excludes, label=f"project:{cwd_slug}")


def _workspace_for_project_by_name(name: str, conn):
    cur = conn.cursor()
    cur.execute("SELECT name, projects FROM workspaces WHERE name = %s", (name,))
    row = cur.fetchone()
    if row:
        return row[0], list(row[1] or [])
    return None


def apply_pg_filter(sql: str, params: list, scope: ScopeFilter, table_alias: str = "s") -> tuple[str, list]:
    """Append WHERE clauses to enforce the scope. Caller is responsible for placing them after WHERE/AND."""
    where = []
    if scope.projects is not None:
        where.append(f'{table_alias}.project_slug = ANY(%s)')
        params.append(scope.projects)
    if scope.excludes:
        where.append(f'{table_alias}.project_slug <> ALL(%s)')
        params.append(scope.excludes)
    if scope.users:
        where.append(f'{table_alias}.os_user = ANY(%s)')
        params.append(scope.users)
    if where:
        joiner = " AND " if " WHERE " in sql.upper() else " WHERE "
        sql = sql + joiner + " AND ".join(where)
    return sql, params


def weaviate_where(scope: ScopeFilter) -> str:
    """Return a GraphQL `where:` clause string for Weaviate. Empty string if no filter."""
    operands = []
    if scope.projects is not None:
        slugs_json = json.dumps(scope.projects)
        operands.append(f'{{ path: ["project_slug"], operator: ContainsAny, valueText: {slugs_json} }}')
    if scope.users:
        users_json = json.dumps(scope.users)
        operands.append(f'{{ path: ["os_user"], operator: ContainsAny, valueText: {users_json} }}')
    if not operands:
        return ""
    if len(operands) == 1:
        return f", where: {operands[0]}"
    return f", where: {{ operator: And, operands: [{', '.join(operands)}] }}"


if __name__ == "__main__":
    # Smoke test (no DB). Uses os.getcwd() so it works on whatever machine
    # runs this — the function only needs the path's basename to derive a slug,
    # not for the path to exist.
    cwd = os.getcwd()
    print(f"  cwd: {cwd}")
    s = resolve_scope(None, cwd=cwd)
    print(f"  default (no DB):       {s.explain()}  projects={s.projects}")
    s = resolve_scope("global")
    print(f"  --scope=global:        {s.explain()}  projects={s.projects}")
    s = resolve_scope("workspace:my-stack")
    print(f"  --scope=ws:my-stack:   {s.explain()}  projects={s.projects}")
    s = resolve_scope("project:other-repo")
    print(f"  --scope=project:other: {s.explain()}  projects={s.projects}")
