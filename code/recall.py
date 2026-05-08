#!/usr/bin/env python3
"""
claude-dejavu — CLI for hybrid recall across Claude Code session history.

Mirrors the MCP server tools but for terminal use, plus workspace management.
"""
import argparse
import json
import os
import sys
import textwrap
import urllib.request
from pathlib import Path

import psycopg2
import psycopg2.extras

# Local imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scope import resolve_scope, apply_pg_filter, weaviate_where  # noqa: E402

# ─── Config (cross-platform, env-overridable) ─────────────────────────────────
def _resolve_data_root() -> Path:
    """Single source of truth for the per-OS data dir. Same logic as
    hooks/stop.py and scripts/install.py — keeping them in sync matters."""
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-dejavu"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData/Local")
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "claude-dejavu" if xdg else Path.home() / ".local" / "share" / "claude-dejavu"


def _load_config():
    home_data = _resolve_data_root()
    cfg = {"DATA_ROOT": str(home_data)}  # always present, even before install.py runs
    f = home_data / "config.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
    return cfg

_CFG = _load_config()
_OS_USER = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
_PG_DB = _CFG.get("PG_DB") or f"claude_dejavu_{_OS_USER}"
PG_DSN = (f"host={_CFG.get('PG_HOST','localhost')} port={_CFG.get('PG_PORT','5450')} "
          f"user={_CFG.get('PG_USER','postgres')} password={_CFG.get('PG_PASS','postgres')} "
          f"dbname={_PG_DB}")
WV_URL = _CFG.get("WV_URL", "http://localhost:8888")
WV_CLASS = os.environ.get("CLAUDE_DEJAVU_WV_CLASS") or _CFG.get("WV_CLASS") or "ClaudeDejavuTurn"

CWD_FOR_SCOPE = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _conn():
    return psycopg2.connect(PG_DSN)


def _get_scope(args):
    try:
        with _conn() as c:
            return resolve_scope(getattr(args, "scope", None), cwd=CWD_FOR_SCOPE, pg_conn=c)
    except Exception:
        return resolve_scope(getattr(args, "scope", None), cwd=CWD_FOR_SCOPE)


def _hybrid(query, limit, scope):
    where = weaviate_where(scope)
    gql = f'''{{
      Get {{
        {WV_CLASS}(
          hybrid: {{ query: {json.dumps(query)}, alpha: 0.5 }}
          limit: {limit}
          {where}
        ) {{
          session_id turn_id project_slug role ts ai_title
          _additional {{ score }}
        }}
      }}
    }}'''
    req = urllib.request.Request(f"{WV_URL}/v1/graphql",
        data=json.dumps({"query": gql}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["data"]["Get"][WV_CLASS] or []
    except Exception as e:
        print(f"weaviate error: {e}", file=sys.stderr); return []


def w(s, n=80):
    s = s or ""; return s if len(s) <= n else s[:n - 1] + "…"


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_search(args):
    sc = _get_scope(args)
    hits = _hybrid(args.query, args.limit, sc)
    print(f"scope={sc.explain()}  hits={len(hits)}")
    if not hits:
        return
    turn_ids = [int(h.get("turn_id") or 0) for h in hits]
    gist_map = {}
    if turn_ids:
        with _conn() as c:
            cur = c.cursor()
            cur.execute("SELECT id, gist FROM turns WHERE id = ANY(%s)", (turn_ids,))
            gist_map = {r[0]: r[1] for r in cur.fetchall()}

    for i, h in enumerate(hits, 1):
        tid = int(h.get("turn_id") or 0)
        score = float(h.get("_additional", {}).get("score") or 0)
        proj = h.get("project_slug") or "?"
        sess = (h.get("session_id") or "")[:8]
        ts = (h.get("ts") or "")[:10]
        gist = gist_map.get(tid) or w(h.get("ai_title") or "(no gist)", 80)
        if args.mode == "full":
            with _conn() as c:
                cur = c.cursor()
                cur.execute("SELECT content FROM turns WHERE id = %s", (tid,))
                row = cur.fetchone()
                content = row[0] if row else ""
            print(f"\n[{i}] turn={tid} score={score:.2f} {proj}/{sess} {ts}")
            print(textwrap.indent((content or "")[:1500], "    "))
        else:
            print(f"  [{i}] turn={tid} score={score:.2f} {proj}/{sess} {ts}  {gist}")
    if args.mode != "full":
        print(f"\nUse `claude-dejavu expand <turn_id>...` for full content.")


def cmd_expand(args):
    if not args.turn_ids:
        print("Pass turn IDs as positional args. Get them from `claude-dejavu search`.")
        return
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT t.id, t.session_id, s.project_slug, s.ai_title, t.role, t.content_type, t.ts, t.content
            FROM turns t JOIN sessions s ON s.id = t.session_id
            WHERE t.id = ANY(%s) ORDER BY t.ts NULLS LAST
        """, (args.turn_ids,))
        for r in cur.fetchall():
            sess = str(r['session_id'])[:8]; ts = str(r.get('ts') or '')[:19]
            print(f"\n── turn {r['id']} {r['project_slug']}/{sess} {r['role']}/{r['content_type']} {ts}")
            if r.get('ai_title'): print(f"   session-title: {r['ai_title']}")
            print(r['content'] or "(empty)")


def cmd_sessions(args):
    sc = _get_scope(args)
    sql = "SELECT id, project_slug, ai_title, total_turns, started_at FROM sessions s"
    params = []
    sql, params = apply_pg_filter(sql, params, sc, table_alias="s")
    sql += " ORDER BY started_at DESC NULLS LAST LIMIT %s"
    params.append(args.limit)
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
    print(f"scope={sc.explain()}")
    print(f"{'session':12} {'project':22} {'turns':>5}  {'started':12}  title")
    print("-" * 110)
    for r in rows:
        sid = str(r["id"])[:10]
        title = w(r["ai_title"] or "(no title)", 50)
        print(f"{sid:12} {(r['project_slug'] or '?'):22} {(r['total_turns'] or 0):>5}  {str(r['started_at'] or '')[:10]:12}  {title}")


def cmd_resume(args):
    sc = _get_scope(args)
    hits = _hybrid(args.hint, 20, sc)
    if not hits:
        print(f"no matches in {sc.explain()}"); return
    by_session = {}
    for h in hits:
        sid = h.get("session_id"); score = float(h.get("_additional", {}).get("score") or 0)
        cur = by_session.get(sid)
        if not cur or score > cur["score"]: by_session[sid] = {"score": score, "hit": h}
    ranked = sorted(by_session.values(), key=lambda x: -x["score"])[:5]
    print(f"Top in scope={sc.explain()}:")
    home = os.path.expanduser("~")
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for i, r in enumerate(ranked, 1):
            sid = r["hit"]["session_id"]
            cur.execute("SELECT project_slug, ai_title, total_turns, started_at FROM sessions WHERE id=%s::uuid", (sid,))
            meta = cur.fetchone() or {}
            slug = meta.get("project_slug") or "?"
            cwd = next((os.path.join(b, slug) for b in (f"{home}/Documents/projects", f"{home}/projects", home) if os.path.isdir(os.path.join(b, slug))), "<project-dir>")
            title = w(meta.get("ai_title") or "(no title)", 60)
            print(f"  [{i}] {sid}  score={r['score']:.2f}  {slug}  {title}")
            print(f"      $ cd {cwd} && claude --resume {sid}")


def _log_indexer_failure(name: str, exc: Exception) -> None:
    """Print to stderr (existing UX) AND persist to the structured
    error log so users can `dejavu logs` later. Never raises — the
    underlying log_error() falls back to stderr on disk failure."""
    print(f"  ({name} indexing failed: {exc})", file=sys.stderr)
    try:
        from error_log import log_error
        log_error(f"indexer.{name}",
                    f"{name} indexing failed: {exc}",
                    exc_info=True)
    except Exception:  # noqa: BLE001
        pass


def cmd_generate_runtime_trap(args):
    """Generate a runtime env-trap module for the project."""
    import re as _re
    from pathlib import Path
    from runtime_trap import generate

    project = args.project or _project_slug_for_cwd()
    cwd = Path.cwd()

    # Pull declared envs from .env.example (or the file the user
    # named) — this is the lowest-friction source.
    env_names: set[str] = set()
    src_path = cwd / args.from_file
    if src_path.is_file():
        for ln in src_path.read_text(errors="ignore").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            m = _re.match(r"^([A-Z_][A-Z0-9_]*)\s*=", ln)
            if m:
                env_names.add(m.group(1))

    # ALSO pull from the indexed env_vars table if the project is
    # indexed — this catches names declared in workflows / compose
    # / source AST scans that .env.example missed.
    try:
        with _conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT DISTINCT name FROM env_vars WHERE project_slug=%s",
                (project,))
            env_names |= {r[0] for r in cur.fetchall()}
    except Exception:  # noqa: BLE001
        pass  # Index may not exist yet — file source is enough

    if not env_names:
        print(f"No env vars found for project `{project}` "
                f"(checked {args.from_file} and the env_vars index). "
                f"Run `claude-dejavu reindex` first, or declare some "
                f"vars in {args.from_file}.", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output) if args.output else None
    path, content = generate(sorted(env_names),
                                language=args.language,
                                output_path=output,
                                project_root=cwd)
    print(f"✓ generated {path.relative_to(cwd) if path.is_relative_to(cwd) else path}")
    print(f"  declared keys: {len(env_names)}")
    print(f"  language:      {args.language}")
    print(f"\nUsage in your code:")
    if args.language.lower() in ("typescript", "ts"):
        print(f"  import {{ env }} from \"./{path.stem}\";")
        print(f"  const url = env.DATABASE_URL;  // throws if not declared")
    else:
        print(f"  from {path.stem} import env")
        print(f"  url = env.DATABASE_URL  # raises DejavuUndeclaredEnvError if not declared")
    print(f"\nTo refresh after adding new env vars, re-run this command.")


def cmd_env_propose(args):
    """Append a new env var declaration to .env.example.

    Mirrors the dejavu_propose_env_addition MCP tool: writes the
    line + a comment naming the SDK convention, with a dry-run
    diff by default so the user can review before applying."""
    import re as _re
    from pathlib import Path
    target = Path.cwd() / args.file
    if not target.is_file():
        print(f"REFUSED: {args.file} doesn't exist at {target}. "
                "Create it first or pass --file pointing somewhere "
                "else.", file=sys.stderr)
        sys.exit(1)
    existing = target.read_text(encoding="utf-8", errors="ignore")
    if _re.search(rf"^{_re.escape(args.name)}\s*=",
                     existing, _re.MULTILINE):
        print(f"NO_CHANGE: `{args.name}` is already declared in "
                f"{args.file}.")
        return
    comment = (f"# Standard env for `{args.package}` (declared by "
                 f"`claude-dejavu env propose`)\n"
                 if args.package else
                 "# Declared by `claude-dejavu env propose`\n")
    new_line = f"{args.name}=\n"
    new_content = (existing
                     + ("\n" if not existing.endswith("\n") else "")
                     + comment + new_line)
    diff = (f"--- a/{args.file}\n+++ b/{args.file}\n"
              f"@@ end-of-file @@\n"
              f"+{comment.rstrip()}\n+{new_line.rstrip()}")
    if args.apply:
        target.write_text(new_content, encoding="utf-8")
        print(f"APPLIED: appended `{args.name}=` to {args.file}.")
        print(diff)
        print(f"\nFill in the value, then:")
        print(f"  git add {args.file} && git commit -m "
                f"'add {args.name}'")
    else:
        print(f"DRY_RUN: would append to {args.file}:")
        print(diff)
        print("\nRe-run with --apply to write the change.")


def cmd_fabrications(args):
    """List + triage per-project fabrication memory."""
    from fabrication_memory import (list_fabrications, set_status,
                                          project_summary)
    project = args.project or _project_slug_for_cwd()
    with _conn() as c:
        c.autocommit = True
        if args.mark:
            name, domain, status = args.mark
            ok_ = set_status(c, project, name, domain, status)
            if ok_:
                print(f"  ✓ {project}/{domain}: `{name}` → {status}")
            else:
                print(f"  ✗ no fabrication record for "
                        f"({project}, {name}, {domain}) — "
                        f"nothing to mark")
            return
        rows = list_fabrications(c, project,
                                      status=args.status,
                                      min_count=args.min_count,
                                      limit=args.limit)
        summary = project_summary(c, project)
    print(f"project: {project}")
    print(f"summary: open={summary['open']}  "
            f"declared={summary['declared']}  "
            f"blacklisted={summary['blacklisted']}  "
            f"total_occurrences={summary['total_occurrences']}")
    if not rows:
        print("\n(no records — either nothing has been fabricated yet "
                "or your filters excluded everything)")
        return
    print()
    print(f"  {'name':32s} {'domain':8s} {'status':12s} "
            f"{'count':>5s}  last_seen")
    print(f"  {'-'*32} {'-'*8} {'-'*12} {'-'*5}  {'-'*10}")
    for r in rows:
        print(f"  {r['name']:32s} {r['domain']:8s} "
                f"{r['status']:12s} {r['count']:5d}  "
                f"{str(r['last_seen_at'])[:19]}")
    print()
    print("  (mark with: claude-dejavu fabrications --project={} "
            "--mark NAME DOMAIN {{declared|blacklisted|open}})"
            .format(project))


def cmd_logs(args):
    """Show recent records from the dejavu plugin error log."""
    from error_log import (read_recent, format_for_human,
                              log_path_str)
    if getattr(args, "path", False):
        print(log_path_str())
        return
    records = read_recent(limit=max(1, min(args.limit, 500)),
                            component=args.component,
                            level=args.level)
    print(f"# claude-dejavu error log ({len(records)} records)")
    print(f"# location: {log_path_str()}")
    print()
    print(format_for_human(
        records, with_traceback=args.with_traceback))


def cmd_errors(args):
    sc = _get_scope(args)
    sql = ("SELECT tc.id AS tool_call_id, s.project_slug, tc.tool_name, tc.tool_input, "
           "       left(tc.tool_output, 600) AS error_preview, tc.ts "
           "FROM tool_calls tc JOIN sessions s ON s.id = tc.session_id WHERE tc.is_error")
    params = []
    if args.tool:
        sql += " AND tc.tool_name = %s"; params.append(args.tool)
    sql, params = apply_pg_filter(sql, params, sc, table_alias="s")
    sql += " ORDER BY tc.ts DESC LIMIT %s"
    params.append(args.limit)
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        rows = cur.fetchall()
    print(f"scope={sc.explain()}  errors={len(rows)}")
    for r in rows:
        print(f"\n[{str(r['ts'])[:19]}] {r['project_slug']} {r['tool_name']}")
        print(f"  input: {json.dumps(r['tool_input'] or {})[:160]}")
        print(f"  err:   {(r['error_preview'] or '')[:200]}")


def cmd_files(args):
    sc = _get_scope(args)
    sql = ("SELECT s.project_slug, s.id AS session_id, ft.file_path, ft.op, ft.ts "
           "FROM file_touches ft JOIN sessions s ON s.id = ft.session_id WHERE ft.file_path ILIKE %s")
    params = [f"%{args.path}%"]
    sql, params = apply_pg_filter(sql, params, sc, table_alias="s")
    sql += " ORDER BY ft.ts DESC LIMIT %s"
    params.append(args.limit)
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params); rows = cur.fetchall()
    print(f"scope={sc.explain()}")
    for r in rows:
        print(f"  {str(r['ts'] or '')[:19]} {r['project_slug']}/{str(r['session_id'])[:8]} {r['op']:10s} {r['file_path']}")


def cmd_restore(args):
    """Restore sessions that exist in backup but are missing from live ~/.claude/projects/.

    Usage:
        claude-dejavu restore --list                # show what's restorable
        claude-dejavu restore <session-uuid> [...]  # restore specific session(s)
        claude-dejavu restore --all                 # restore everything missing
        claude-dejavu restore --since=7d            # restore items missing within last 7 days
    """
    import shutil, hashlib, time, datetime as dt
    backup_root = Path(_CFG["DATA_ROOT"]) / "chat-logs"
    live_root = Path.home() / ".claude" / "projects"

    if not backup_root.is_dir():
        print(f"Backup root not found: {backup_root}", file=sys.stderr); return

    deleted = []  # list of dicts
    truncated = []
    for proj_dir in backup_root.iterdir():
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            uuid = jsonl.stem
            live_path = live_root / proj_dir.name / f"{uuid}.jsonl"
            backup_size = jsonl.stat().st_size
            backup_mtime = jsonl.stat().st_mtime
            if not live_path.exists():
                deleted.append({"proj": proj_dir.name, "uuid": uuid, "src": jsonl, "dst": live_path,
                                "size": backup_size, "mtime": backup_mtime, "kind": "deleted"})
            else:
                live_size = live_path.stat().st_size
                if live_size < backup_size * 0.95:  # >5% smaller = likely truncated
                    truncated.append({"proj": proj_dir.name, "uuid": uuid, "src": jsonl, "dst": live_path,
                                      "size": backup_size, "live_size": live_size, "kind": "truncated"})

    candidates = deleted + truncated
    if args.since:
        cutoff = time.time() - _parse_duration(args.since)
        candidates = [c for c in candidates if c["mtime"] >= cutoff]

    if args.list or not (args.session_uuids or args.all):
        if not candidates:
            print("Nothing to restore — all backup sessions present in live (~/.claude/projects/).")
            return
        print(f"Restorable sessions ({len(candidates)}):\n")
        print(f"{'kind':10} {'uuid':38} {'project':30} {'mtime':19} {'backup_size':>12}")
        print("-" * 120)
        for c in candidates:
            mtime_str = dt.datetime.fromtimestamp(c["mtime"]).isoformat(sep=" ", timespec="seconds")
            extra = f" (live={c.get('live_size','?')})" if c["kind"] == "truncated" else ""
            print(f"{c['kind']:10} {c['uuid']:38} {c['proj'][:30]:30} {mtime_str:19} {c['size']:>12}{extra}")
        print(f"\nRestore with: claude-dejavu restore <uuid> [...]   or   claude-dejavu restore --all")
        return

    # Filter to what was requested
    if args.all:
        targets = candidates
    else:
        wanted = set(args.session_uuids)
        targets = [c for c in candidates if c["uuid"] in wanted]
        missing_in_backup = wanted - {c["uuid"] for c in candidates}
        for u in missing_in_backup:
            print(f"  ⚠ {u}: not in backup OR live already has equal-size copy", file=sys.stderr)

    if not targets:
        print("No sessions to restore."); return

    restored = 0
    for c in targets:
        c["dst"].parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(c["src"], c["dst"])
        # sha256 verify
        h_src = hashlib.sha256(c["src"].read_bytes()).hexdigest()
        h_dst = hashlib.sha256(c["dst"].read_bytes()).hexdigest()
        if h_src != h_dst:
            print(f"  ❌ {c['uuid']}: checksum mismatch — restore aborted", file=sys.stderr)
            c["dst"].unlink(missing_ok=True)
            continue
        restored += 1
        # Find a project dir for the resume hint
        slug = c["proj"].lstrip("-").rsplit("projects-", 1)[-1] or c["proj"]
        home = str(Path.home())
        cwd = next((f"{base}/{slug}" for base in (f"{home}/Documents/projects", f"{home}/projects", home)
                    if Path(f"{base}/{slug}").is_dir()), "<project-dir>")
        size_mb = c["size"] / 1e6
        print(f"  ✅ {c['uuid']}  ({size_mb:.1f} MB)  → {c['dst']}")
        print(f"     resume: cd {cwd} && claude --resume {c['uuid']}")

    print(f"\nRestored {restored}/{len(targets)} session(s).")


def _parse_duration(s: str) -> float:
    """'7d' → seconds, '24h' → seconds, '60m' → seconds. Defaults to 1 day if unparseable."""
    import re
    m = re.match(r"^(\d+)([dhms])$", s.strip().lower())
    if not m: return 86400.0
    n = int(m.group(1))
    unit = m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def cmd_workspace(args):
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if args.action == "list":
            cur.execute("SELECT name, description, projects FROM workspaces ORDER BY name")
            for r in cur.fetchall():
                print(f"  {r['name']:20} ({len(r['projects'])} projects)  {r.get('description') or ''}")
                print(f"    {', '.join(r['projects'])}")
        elif args.action == "create":
            projects = [p.strip() for p in args.projects.split(",") if p.strip()] if args.projects else []
            cur.execute("INSERT INTO workspaces (name, description, projects) VALUES (%s, %s, %s) "
                        "ON CONFLICT (name) DO UPDATE SET projects = EXCLUDED.projects, description = COALESCE(EXCLUDED.description, workspaces.description)",
                        (args.name, args.description, projects))
            c.commit()
            print(f"workspace '{args.name}' set: {projects}")
        elif args.action == "add":
            cur.execute("UPDATE workspaces SET projects = array_append(array_remove(projects, %s), %s) WHERE name = %s",
                        (args.project, args.project, args.name))
            c.commit()
            print(f"added '{args.project}' to '{args.name}'")
        elif args.action == "remove":
            cur.execute("UPDATE workspaces SET projects = array_remove(projects, %s) WHERE name = %s",
                        (args.project, args.name))
            c.commit()
            print(f"removed '{args.project}' from '{args.name}'")
        elif args.action == "delete":
            cur.execute("DELETE FROM workspaces WHERE name = %s", (args.name,))
            c.commit()
            print(f"workspace '{args.name}' deleted")


# ─── v0.3.0: code symbol grounding ───────────────────────────────────────────

def _project_slug_for_cwd():
    return os.path.basename(os.path.realpath(CWD_FOR_SCOPE))


def cmd_reindex(args):
    """Index (or re-index) the project's source files: symbols + routes
    + (optionally) semantic embeddings.

    v0.4.2 perf: build the IgnoreFilter ONCE here and pass to every
    indexer. Each indexer used to walk the full project tree on its
    own filter construction (11 redundant traversals on a typical
    reindex). Sharing the filter eliminates that.
    """
    from symbol_indexer import index_project
    from ignore import IgnoreFilter
    project_dir = args.path or CWD_FOR_SCOPE
    project_slug = args.project or _project_slug_for_cwd()
    flt = IgnoreFilter.for_project(project_dir)
    with _conn() as c:
        c.autocommit = True
        sym_stats = index_project(project_dir, project_slug=project_slug, conn=c, force=args.force, flt=flt)
        if "error" in sym_stats:
            print(f"reindex error: {sym_stats['error']}", file=sys.stderr); sys.exit(1)
        # Routes — runs after symbols so handler_symbol_id can later be resolved.
        route_stats = {}
        if not args.skip_routes:
            try:
                from route_indexer import index_project_routes
                route_stats = index_project_routes(project_dir, project_slug=project_slug, conn=c, flt=flt, force=args.force)
            except Exception as e:
                _log_indexer_failure('route', e)
        # Pages — independent of routes; same-pass for cache-locality.
        page_stats = {}
        if not args.skip_pages:
            try:
                from page_indexer import index_project_pages
                page_stats = index_project_pages(project_dir, project_slug=project_slug, conn=c, flt=flt, force=args.force)
            except Exception as e:
                _log_indexer_failure('page', e)
        # Env vars — independent of all the above.
        env_stats = {}
        if not args.skip_env:
            try:
                from env_indexer import index_project_env_vars
                env_stats = index_project_env_vars(
                    project_dir, project_slug=project_slug, conn=c,
                    include_runtime_dotenv=args.include_runtime_dotenv,
                    flt=flt,
                    force=args.force,
                )
            except Exception as e:
                _log_indexer_failure('env', e)
        # DB schema — Prisma + Drizzle + TypeORM + SQL migrations.
        db_stats = {}
        if not args.skip_db:
            try:
                from db_indexer import index_project_db
                db_stats = index_project_db(project_dir, project_slug=project_slug, conn=c, flt=flt, force=args.force)
            except Exception as e:
                _log_indexer_failure('db', e)
        # GraphQL schema — *.graphql / *.gql + inline gql template literals.
        gql_stats = {}
        if not args.skip_graphql:
            try:
                from graphql_indexer import index_project_graphql
                gql_stats = index_project_graphql(project_dir, project_slug=project_slug, conn=c, flt=flt, force=args.force)
            except Exception as e:
                _log_indexer_failure('graphql', e)
        # Feature flags — flags.json / featureFlags.ts / LD / Unleash / GrowthBook.
        flag_stats = {}
        if not args.skip_flags:
            try:
                from flag_indexer import index_project_flags
                flag_stats = index_project_flags(project_dir, project_slug=project_slug, conn=c, flt=flt, force=args.force)
            except Exception as e:
                _log_indexer_failure('flag', e)
        # Quick-win domains: CSS classes (Tailwind + project) + i18n keys.
        quickwin_stats = {}
        if not args.skip_quickwins:
            try:
                from quickwin_indexer import index_project_quickwins
                quickwin_stats = index_project_quickwins(project_dir,
                                                            project_slug=project_slug,
                                                            conn=c, flt=flt,
                                                            force=args.force)
            except Exception as e:
                _log_indexer_failure('quickwin', e)
        # Module exports — what node_modules/<pkg> actually exports.
        module_stats = {}
        if not args.skip_modules:
            try:
                from module_indexer import index_project_modules
                module_stats = index_project_modules(project_dir,
                                                       project_slug=project_slug,
                                                       conn=c)
            except Exception as e:
                _log_indexer_failure('module', e)
        # Name registry (prose grounding for non-code projects).
        registry_stats = {}
        if not args.skip_registry:
            try:
                from registry_indexer import index_project_name_registry
                registry_stats = index_project_name_registry(
                    project_dir, project_slug=project_slug, conn=c, flt=flt)
            except Exception as e:
                _log_indexer_failure('name_registry', e)
        # Semantic — only if Weaviate is reachable AND not skipped.
        semantic_stats = {}
        if not args.skip_semantic:
            try:
                from semantic_search import is_available, reindex_project as semantic_reindex
                if is_available():
                    semantic_stats = semantic_reindex(project_slug, c)
            except Exception as e:
                _log_indexer_failure('semantic', e)

    print(f"project: {project_slug}  ({project_dir})")
    print(f"  symbols:")
    print(f"    files scanned : {sym_stats['files_scanned']}")
    print(f"    files indexed : {sym_stats['files_indexed']}")
    print(f"    files skipped : {sym_stats['files_skipped']} (unchanged)")
    print(f"    files purged  : {sym_stats.get('files_purged', 0)} (deleted)")
    print(f"    symbols added : {sym_stats['symbols_added']}")
    print(f"    parse errors  : {sym_stats['errors']}")
    def _cached_tag(s: dict) -> str:
        return "  (cached, no changes since last run)" if s.get("cached") else ""

    if route_stats:
        print(f"  routes:{_cached_tag(route_stats)}")
        if "error" in route_stats:
            print(f"    error         : {route_stats['error']}")
        else:
            print(f"    files scanned : {route_stats.get('files_scanned', 0)}")
            print(f"    files w/routes: {route_stats.get('files_with_routes', 0)}")
            print(f"    routes added  : {route_stats.get('routes_added', 0)}")
            print(f"    parse errors  : {route_stats.get('errors', 0)}")
    if page_stats:
        print(f"  pages:{_cached_tag(page_stats)}")
        if "error" in page_stats:
            print(f"    error         : {page_stats['error']}")
        else:
            print(f"    pages added   : {page_stats.get('pages_added', 0)}")
            by_fw = page_stats.get('by_framework', {})
            if by_fw:
                fw_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_fw.items()))
                print(f"    by framework  : {fw_summary}")
    if env_stats:
        print(f"  env vars:{_cached_tag(env_stats)}")
        if "error" in env_stats:
            print(f"    error         : {env_stats['error']}")
        else:
            print(f"    env vars added: {env_stats.get('env_vars_added', 0)}")
            print(f"    secrets       : {env_stats.get('secrets_flagged', 0)}")
            by_src = env_stats.get('by_source', {})
            if by_src:
                src_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_src.items()))
                print(f"    by source     : {src_summary}")
    if db_stats:
        print(f"  db schema:{_cached_tag(db_stats)}")
        if "error" in db_stats:
            print(f"    error         : {db_stats['error']}")
        else:
            print(f"    models added  : {db_stats.get('models_added', 0)}")
            print(f"    fields added  : {db_stats.get('fields_added', 0)}")
            by_src = db_stats.get('by_source', {})
            if by_src:
                src_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_src.items()))
                print(f"    by source     : {src_summary}")
    if gql_stats:
        print(f"  graphql schema:{_cached_tag(gql_stats)}")
        if "error" in gql_stats:
            print(f"    error         : {gql_stats['error']}")
        else:
            print(f"    types added   : {gql_stats.get('types_added', 0)}")
            print(f"    fields added  : {gql_stats.get('fields_added', 0)}")
            by_src = gql_stats.get('by_source', {})
            if by_src:
                src_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_src.items()))
                print(f"    by source     : {src_summary}")
    if flag_stats:
        print(f"  feature flags:{_cached_tag(flag_stats)}")
        if "error" in flag_stats:
            print(f"    error         : {flag_stats['error']}")
        else:
            print(f"    flags added   : {flag_stats.get('flags_added', 0)}")
            print(f"    kill switches : {flag_stats.get('kill_switches_flagged', 0)}")
            by_src = flag_stats.get('by_source', {})
            if by_src:
                src_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_src.items()))
                print(f"    by source     : {src_summary}")
    if quickwin_stats:
        print(f"  quick-wins:{_cached_tag(quickwin_stats)}")
        if "error" in quickwin_stats:
            print(f"    error         : {quickwin_stats['error']}")
        else:
            print(f"    css classes   : {quickwin_stats.get('css_classes_added', 0)}")
            print(f"    i18n keys     : {quickwin_stats.get('i18n_keys_added', 0)}")
    if module_stats:
        print(f"  module exports:")
        if "error" in module_stats:
            print(f"    error         : {module_stats['error']}")
        else:
            print(f"    pkgs scanned  : {module_stats.get('packages_scanned', 0)}")
            print(f"    exports added : {module_stats.get('exports_added', 0)}")
    if registry_stats:
        print(f"  name registry:")
        if "error" in registry_stats:
            print(f"    error         : {registry_stats['error']}")
        else:
            print(f"    names added   : {registry_stats.get('names_added', 0)}")
            by_kind = registry_stats.get('by_kind', {})
            if by_kind:
                kind_summary = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))
                print(f"    by kind       : {kind_summary}")
    if semantic_stats:
        print(f"  semantic (Weaviate):")
        if "error" in semantic_stats:
            print(f"    skipped       : {semantic_stats['error']}")
        else:
            print(f"    symbols       : {semantic_stats.get('symbols', 0)}")
            print(f"    routes        : {semantic_stats.get('routes', 0)}")


def cmd_symbols(args):
    """List or look up symbols. Without a query, lists everything (paginated)."""
    from symbol_indexer import resolve_symbol, list_symbols, log_outcome
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        c.autocommit = True
        if args.name:
            hits = resolve_symbol(args.name, project_slug=project_slug, conn=c,
                                  fuzzy=not args.exact, limit=args.limit,
                                  languages=[args.language] if args.language else None)
            if not hits:
                log_outcome(c, project_slug=project_slug, language=args.language,
                            mode="lookup", proposed_name=args.name, resolved_name=None,
                            candidate_count=0, outcome="unknown",
                            outcome_signal="no_candidates")
                print(f"no candidates for '{args.name}' in '{project_slug}'"); return
            top = hits[0]
            log_outcome(c, project_slug=project_slug, language=top.get("language"),
                        mode="lookup", proposed_name=args.name,
                        resolved_name=top["name"],
                        edit_distance=top.get("edit_distance"),
                        trigram_sim=top.get("trigram_sim"),
                        candidate_count=len(hits), outcome="unknown",
                        outcome_signal="logged_at_lookup")
            for h in hits:
                marker = "✓" if h["edit_distance"] == 0 else " "
                sig = h.get("signature") or h["name"]
                print(f"{marker} {h['kind']:9s} {h['name']:30s} sim={h['trigram_sim']:.2f} ed={h['edit_distance']}  {h['file_path']}:{h['start_line']}")
                if sig != h["name"]:
                    print(f"    {sig}")
        else:
            rows = list_symbols(project_slug=project_slug, conn=c,
                                file_path=args.file, kind=args.kind,
                                language=args.language, limit=args.limit, offset=args.offset)
            print(f"{len(rows)} symbol(s) in '{project_slug}'")
            for r in rows:
                print(f"  {r['kind']:9s} {r['name']:30s}  {r['file_path']}:{r['start_line']}")


def cmd_lint(args):
    """Lint a proposed file edit against the symbol index.

    Read content from --content-file, --content-stdin, or default to reading
    the existing file at --file (useful for sanity-checking already-written code).
    """
    from lint import lint_proposed_edit
    project_slug = args.project or _project_slug_for_cwd()
    file_path = args.file
    if args.content_stdin:
        content = sys.stdin.read()
    elif args.content_file:
        content = open(args.content_file, "r", encoding="utf-8").read()
    else:
        # Default: lint the existing file at file_path
        try:
            content = open(file_path, "r", encoding="utf-8").read()
        except OSError as e:
            print(f"could not read {file_path}: {e}", file=sys.stderr); sys.exit(1)

    with _conn() as c:
        c.autocommit = True
        r = lint_proposed_edit(content, file_path, project_slug=project_slug, conn=c)

    if args.json:
        print(json.dumps(r, indent=2, default=str))
        # Exit code reflects verdict — important for CI/hooks even in JSON mode.
        v = r["summary"]["verdict"]
        sys.exit({"ok": 0, "warn": 1, "block": 2}.get(v, 0))

    s = r["summary"]
    print(f"verdict: {s['verdict'].upper()}  refs={s['total_references']}  ok={s['resolved']}  warn={s['low_confidence']}  unknown={s['unknown']}")
    if s.get("note"):
        print(f"  note: {s['note']}")
    if r["unknown"]:
        print("\nunknown (likely fabricated):")
        for u in r["unknown"]:
            c_ = u.get("top_candidate")
            via = f" [receiver: {u['via_receiver_type']}]" if u.get("via_receiver_type") else ""
            note = f"  note: {u['note']}" if u.get("note") else ""
            if c_:
                print(f"  ? {u['name']:30s} closest: {c_['name']} sim={c_['trigram_sim']} ed={c_['edit_distance']}{via}{note}")
            else:
                print(f"  ? {u['name']:30s} (no candidates){via}{note}")
    if r["low_confidence"]:
        print("\nlow-confidence (likely typo):")
        for lc in r["low_confidence"]:
            c_ = lc["top_candidate"]
            print(f"  ~ {lc['name']:30s} -> {c_['name']:30s} sim={c_['trigram_sim']} ed={c_['edit_distance']}")
    if args.show_resolved and r["resolved"]:
        print(f"\nresolved ({len(r['resolved'])}):")
        for x in r["resolved"]:
            print(f"  ✓ {x['name']:30s} [{x['kind']}]")
    # ─── routes (v0.3.2.1) ─────────────────────────────────────────────
    if r.get("routes_unknown"):
        print("\nroutes — unknown (likely fabricated URLs):")
        for u in r["routes_unknown"]:
            print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
    if r.get("routes_low_confidence"):
        print("\nroutes — low-confidence (typo or wrong method):")
        for lc in r["routes_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}  {lc.get('reason', '')}")
    if args.show_resolved and r.get("routes_resolved"):
        print(f"\nroutes — resolved ({len(r['routes_resolved'])}):")
        for x in r["routes_resolved"]:
            print(f"  ✓ {x['name']:40s} -> {x.get('candidate', '')}")
    # ─── pages (v0.3.5) ────────────────────────────────────────────────
    if r.get("pages_unknown"):
        print("\npages — DUPLICATE PAGE detected (existing page covers this URL):")
        for u in r["pages_unknown"]:
            print(f"  ✗ {u['name']:40s} -> {u.get('candidate', '?')}")
            print(f"      {u.get('reason', '')}")
            if u.get('fix'):
                print(f"      → {u['fix']}")
    if r.get("pages_low_confidence"):
        print("\npages — possible overlap (parameterized or near-name match):")
        for lc in r["pages_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
            print(f"      {lc.get('reason', '')}")
    # ─── env vars (v0.3.6) ─────────────────────────────────────────────
    if r.get("env_unknown"):
        print("\nenv vars — unknown (likely fabricated):")
        for u in r["env_unknown"]:
            print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
            if u.get('fix'):
                print(f"      → {u['fix']}")
    if r.get("env_low_confidence"):
        print("\nenv vars — low-confidence (typo or undeclared but read):")
        for lc in r["env_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
            print(f"      {lc.get('reason', '')}")
            if lc.get('fix'):
                print(f"      → {lc['fix']}")
    if args.show_resolved and r.get("env_resolved"):
        print(f"\nenv vars — resolved ({len(r['env_resolved'])}):")
        for x in r["env_resolved"]:
            secret = " 🔒" if x.get("metadata", {}).get("is_secret") else ""
            print(f"  ✓ {x['name']:40s}{secret}  {x.get('reason', '')}")
    # ─── db schema (v0.3.7) ────────────────────────────────────────────
    if r.get("db_unknown"):
        print("\ndb schema — unknown (likely fabricated):")
        for u in r["db_unknown"]:
            print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
            if u.get('fix'):
                print(f"      → {u['fix']}")
    if r.get("db_low_confidence"):
        print("\ndb schema — low-confidence (typo or wrong model):")
        for lc in r["db_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
            print(f"      {lc.get('reason', '')}")
            if lc.get('fix'):
                print(f"      → {lc['fix']}")
    if args.show_resolved and r.get("db_resolved"):
        print(f"\ndb schema — resolved ({len(r['db_resolved'])}):")
        for x in r["db_resolved"]:
            print(f"  ✓ {x['name']:40s}  {x.get('reason', '')}")
    # ─── graphql (v0.3.8) ──────────────────────────────────────────────
    if r.get("gql_unknown"):
        print("\ngraphql — unknown (likely fabricated):")
        for u in r["gql_unknown"]:
            print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
            if u.get('fix'):
                print(f"      → {u['fix']}")
    if r.get("gql_low_confidence"):
        print("\ngraphql — low-confidence (typo or wrong type):")
        for lc in r["gql_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
            print(f"      {lc.get('reason', '')}")
            if lc.get('fix'):
                print(f"      → {lc['fix']}")
    if args.show_resolved and r.get("gql_resolved"):
        print(f"\ngraphql — resolved ({len(r['gql_resolved'])}):")
        for x in r["gql_resolved"]:
            print(f"  ✓ {x['name']:40s}  {x.get('reason', '')}")
    # ─── feature flags (v0.3.10) ───────────────────────────────────────
    if r.get("flag_unknown"):
        print("\nfeature flags — unknown (likely fabricated):")
        for u in r["flag_unknown"]:
            print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
            if u.get('fix'):
                print(f"      → {u['fix']}")
    if r.get("flag_low_confidence"):
        print("\nfeature flags — low-confidence (typo or undeclared but used):")
        for lc in r["flag_low_confidence"]:
            print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
            print(f"      {lc.get('reason', '')}")
            if lc.get('fix'):
                print(f"      → {lc['fix']}")
    if args.show_resolved and r.get("flag_resolved"):
        print(f"\nfeature flags — resolved ({len(r['flag_resolved'])}):")
        for x in r["flag_resolved"]:
            kill = " 🛑" if x.get("metadata", {}).get("is_kill_switch") else ""
            print(f"  ✓ {x['name']:40s}{kill}  {x.get('reason', '')}")
    # ─── quick-win domains (v0.3.14) + imports (v0.3.16) + prose (v0.4.0) ───
    for label, key in (("css", "css"), ("i18n", "i18n"),
                        ("npm-script", "npm"), ("asset-path", "asset"),
                        ("module-import", "import"), ("prose", "prose")):
        if r.get(f"{key}_unknown"):
            print(f"\n{label} — unknown (likely fabricated):")
            for u in r[f"{key}_unknown"]:
                print(f"  ? {u['name']:40s}  {u.get('reason', '')}")
                if u.get('fix'):
                    print(f"      → {u['fix']}")
        if r.get(f"{key}_low_confidence"):
            print(f"\n{label} — low-confidence (typo or near-match):")
            for lc in r[f"{key}_low_confidence"]:
                print(f"  ~ {lc['name']:40s} -> {lc.get('candidate', '?')}")
                print(f"      {lc.get('reason', '')}")
                if lc.get('fix'):
                    print(f"      → {lc['fix']}")

    # Exit code reflects verdict so CI / hooks can branch on it.
    if s["verdict"] == "block":
        sys.exit(2)
    elif s["verdict"] == "warn":
        sys.exit(1)
    else:
        sys.exit(0)


def cmd_routes(args):
    """List or look up indexed API routes."""
    from route_indexer import resolve_route, list_routes, normalize_path
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.rcmd == "list":
            rows = list_routes(project_slug, c, framework=args.framework,
                               method=args.method, limit=args.limit)
            if not rows:
                print(f"no routes indexed for '{project_slug}'"); return
            print(f"{len(rows)} route(s) in '{project_slug}'")
            for r in rows:
                print(f"  {r['method']:6s} {r['path_pattern']:35s} [{r['framework']}/{r['source']}]  {r['source_file']}:{r['start_line']}")
        else:  # get
            hits = resolve_route(args.method, args.path, project_slug, c,
                                  fuzzy=not args.exact, limit=args.limit)
            if not hits:
                print(f"no route matching `{args.method.upper()} {normalize_path(args.path)}`")
                sys.exit(1)
            for h in hits:
                marker = "✓" if h["match_kind"] == "exact" else ("≈" if h["match_kind"] == "pattern" else "~")
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                print(f"  {marker} {h['method']:6s} {h['path_pattern']:35s} [{h['framework']}/{h['source']}]  {h['source_file']}:{h['start_line']}{sim}")


def cmd_pages(args):
    """List indexed page routes or check a proposed path for duplicates."""
    from page_indexer import list_pages, check_new_page
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.pcmd == "list":
            rows = list_pages(project_slug, c, framework=args.framework,
                              kind=args.kind, limit=args.limit)
            if not rows:
                print(f"no pages indexed for '{project_slug}'"); return
            print(f"{len(rows)} page(s) in '{project_slug}'")
            for r in rows:
                print(f"  {r['kind']:10s} {r['path_pattern']:40s} [{r['framework']}]  {r['source_file']}:{r['start_line']}")
        else:  # check
            hits = check_new_page(args.path, project_slug, c, fuzzy=True, limit=args.limit)
            if not hits:
                print(f"no existing page covers `{args.path}` in '{project_slug}' — safe to create.")
                return
            print(f"{len(hits)} existing page(s) overlap with `{args.path}` in '{project_slug}':")
            for h in hits:
                marker = "✗" if h["match_kind"] == "exact" else ("≈" if h["match_kind"] == "pattern" else "~")
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                print(f"  {marker} {h['kind']:10s} {h['path_pattern']:35s} [{h['framework']}]  {h['source_file']}:{h['start_line']}{sim}")


def cmd_ignore(args):
    """Inspect / debug the .dejavuignore filter (v0.3.11)."""
    from ignore import IgnoreFilter
    project_root = Path(args.path or os.getcwd()).resolve() if args.icmd == "status" \
        else Path(os.getcwd()).resolve()
    # For both subcommands, use the project root — for `status`, take it
    # from the file's enclosing repo (walk up to a `.git`/`package.json`/
    # `pyproject.toml` marker), else cwd.
    if args.icmd == "status":
        target = Path(args.path).resolve()
        # Find project root by walking up to a marker.
        markers = (".git", "package.json", "pyproject.toml", "Cargo.toml",
                    "go.mod", ".dejavuignore")
        cur = target if target.is_dir() else target.parent
        while cur != cur.parent:
            if any((cur / m).exists() for m in markers):
                project_root = cur
                break
            cur = cur.parent
    flt = IgnoreFilter.for_project(project_root)
    if args.icmd == "list":
        rules = flt.list_rules()
        if not rules:
            print(f"no ignore rules loaded for '{project_root}'"); return
        print(f"{len(rules)} rule(s) for '{project_root}':")
        for r in rules:
            scope = "" if r["base"] == str(project_root) else f"  ({r['base']})"
            flags = []
            if r["anchored"]: flags.append("anchored")
            if r["only_dirs"]: flags.append("dirs-only")
            tag = " [" + ",".join(flags) + "]" if flags else ""
            print(f"  {r['pattern']:30s}{tag}{scope}")
        return
    # status
    info = flt.explain(args.path)
    verdict = "SKIP" if info["skipped"] else "INDEX"
    print(f"path     : {info['path']}")
    print(f"is_dir   : {info['is_dir']}")
    print(f"verdict  : {verdict}")
    if info["winning_rule"]:
        wr = info["winning_rule"]
        action = "ignored by" if not wr["negate"] else "re-included by"
        print(f"reason   : {action} `{wr['pattern']}` from {wr['base']}")
    else:
        print(f"reason   : no rule matched (default = INDEX)")
    if len(info["all_matching_rules"]) > 1:
        print(f"all matching rules ({len(info['all_matching_rules'])}, last wins):")
        for r in info["all_matching_rules"]:
            print(f"  {r['pattern']:30s}  base={r['base']}")


def cmd_thresholds(args):
    """List or recompute per-project, per-channel adaptive resolver
    thresholds (v0.3.10)."""
    from adaptive_threshold import (list_thresholds, get_threshold,
                                      compute_optimal_threshold)
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        c.autocommit = True
        if args.tcmd == "list":
            rows = list_thresholds(c, project_slug=args.project)
            if not rows:
                print(f"no thresholds tuned yet "
                      f"(project={args.project or '<all>'})")
                return
            print(f"{len(rows)} threshold row(s)")
            for r in rows:
                prec = (f"prec={r['precision_at_top']:.2f}"
                         if r["precision_at_top"] is not None else "prec=?")
                rec = (f"recall={r['recall_at_top3']:.2f}"
                        if r["recall_at_top3"] is not None else "recall=?")
                print(f"  {r['project_slug']:20s} {r['channel']:10s} "
                      f"t={r['threshold']:.2f}  obs={r['outcomes_observed']:4d}  "
                      f"{prec}  {rec}")
        elif args.tcmd == "tune":
            channels = ([args.channel] if args.channel
                         else ["symbol", "route", "page", "env",
                                "db", "graphql", "flag"])
            for ch in channels:
                res = compute_optimal_threshold(project_slug, ch, c)
                if res is None:
                    print(f"  {ch:10s}  insufficient outcomes "
                          f"(< 25) — keeping default")
                    continue
                t, obs, prec, recall = res
                print(f"  {ch:10s}  optimal={t:.2f}  "
                      f"obs={obs}  prec={prec:.2f}  recall={recall:.2f}")
                # Also write to the cache.
                get_threshold(project_slug, ch, c, recompute=True)


def cmd_flags(args):
    """Browse / resolve indexed feature flag declarations."""
    from flag_indexer import list_flags, resolve_flag
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.fcmd == "list":
            rows = list_flags(project_slug, c, source=args.source,
                              kill_switches_only=args.kill_switches_only,
                              limit=args.limit)
            if not rows:
                print(f"no feature flags indexed for '{project_slug}'"); return
            print(f"{len(rows)} flag(s) in '{project_slug}'")
            for r in rows:
                marker = "🛑" if r["is_kill_switch"] else "  "
                sources = ",".join(r["sources"])
                rel = ""
                if r.get("canonical_file"):
                    rel = f"  {r['canonical_file']}:{r['canonical_line']}"
                print(f"  {marker} {r['flag_key']:35s} [{sources}]{rel}")
        else:  # check
            hits = resolve_flag(args.flag_key, project_slug, c, fuzzy=True,
                                limit=args.limit)
            if not hits:
                print(f"no flag matching `{args.flag_key}` in '{project_slug}' — "
                      f"would be flagged as fabricated.")
                sys.exit(1)
            for h in hits:
                marker = "✓" if h["match_kind"] == "exact" else "~"
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                kill = " 🛑" if h["is_kill_switch"] else ""
                print(f"  {marker} {h['flag_key']:30s} [{h['source']}]{kill}  "
                      f"{h['source_file']}:{h['start_line']}{sim}")


def cmd_gql(args):
    """Browse / resolve indexed GraphQL types + fields."""
    from graphql_indexer import (list_gql_types, list_gql_fields,
                                   resolve_gql_field, resolve_gql_type)
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.gcmd == "types":
            rows = list_gql_types(project_slug, c, kind=args.kind,
                                    source=args.source, limit=args.limit)
            if not rows:
                print(f"no GraphQL types indexed for '{project_slug}'"); return
            print(f"{len(rows)} type(s) in '{project_slug}'")
            for r in rows:
                print(f"  {r['kind']:10s} {r['type_name']:25s} [{r['source']}]  "
                      f"{r['field_count']} fields  "
                      f"{r['source_file']}:{r['start_line']}")
        elif args.gcmd == "fields":
            rows = list_gql_fields(project_slug, c, type_name=args.type,
                                    limit=args.limit)
            if not rows:
                print(f"no GraphQL fields indexed in '{project_slug}'"); return
            print(f"{len(rows)} field(s)")
            for r in rows:
                badges = []
                if not r["is_nullable"]: badges.append("required")
                if r["is_list"]: badges.append("list")
                badge = (" [" + ",".join(badges) + "]") if badges else ""
                print(f"  {r['type_name']:20s}.{r['field_name']:25s} "
                      f"{r['field_type'] or '?':15s}{badge}")
        elif args.gcmd == "check":
            if "." in args.target:
                tn, fn = args.target.split(".", 1)
            else:
                tn, fn = None, args.target
            hits = resolve_gql_field(tn, fn, project_slug, c, fuzzy=True,
                                      limit=args.limit)
            if not hits:
                print(f"no GraphQL field matching `{args.target}` in '{project_slug}' — "
                      f"would be flagged as fabricated.")
                sys.exit(1)
            for h in hits:
                marker = ("✓" if h["match_kind"] == "exact" else
                          ("≈" if h["match_kind"] == "cross-type" else "~"))
                sim = (f" sim={h['trigram_sim']:.2f}"
                        if "fuzzy" in h["match_kind"] else "")
                print(f"  {marker} {h['type_name']:20s}.{h['field_name']:25s} "
                      f"[{h['source']}]  {h['source_file']}:{h['start_line']}{sim}")
        else:  # check-type
            hits = resolve_gql_type(args.target, project_slug, c,
                                      fuzzy=True, limit=args.limit)
            if not hits:
                print(f"no GraphQL type named `{args.target}` in '{project_slug}'")
                sys.exit(1)
            for h in hits:
                marker = "✓" if h["match_kind"] == "exact" else "~"
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                print(f"  {marker} {h['type_name']:25s} kind={h['kind']:12s} "
                      f"[{h['source']}]  {h['source_file']}:{h['start_line']}{sim}")


def cmd_db(args):
    """Browse / resolve indexed DB models + fields."""
    from db_indexer import (list_db_models, list_db_fields, resolve_db_field,
                              resolve_db_model)
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.dcmd == "models":
            rows = list_db_models(project_slug, c, source=args.source,
                                    limit=args.limit)
            if not rows:
                print(f"no DB models indexed for '{project_slug}'"); return
            print(f"{len(rows)} model(s) in '{project_slug}'")
            for r in rows:
                tn = f" → {r['table_name']}" if r.get("table_name") else ""
                print(f"  {r['model_name']:25s} [{r['source']}]  "
                      f"{r['field_count']} fields{tn}  "
                      f"{r['source_file']}:{r['start_line']}")
        elif args.dcmd == "fields":
            rows = list_db_fields(project_slug, c, model_name=args.model,
                                    limit=args.limit)
            if not rows:
                print(f"no fields indexed in '{project_slug}'"); return
            print(f"{len(rows)} field(s)")
            for r in rows:
                badges = []
                if r["is_id"]: badges.append("id")
                if r["is_unique"]: badges.append("unique")
                if r["is_relation"]: badges.append(f"→{r['related_model']}")
                badge = (" [" + ",".join(badges) + "]") if badges else ""
                print(f"  {r['model_name']:20s}.{r['field_name']:25s} "
                      f"{r['field_type'] or '?':10s}{badge}")
        elif args.dcmd == "check":
            # Either MODEL.FIELD or just FIELD (cross-model search).
            if "." in args.target:
                mn, fn = args.target.split(".", 1)
            else:
                mn, fn = None, args.target
            hits = resolve_db_field(mn, fn, project_slug, c, fuzzy=True,
                                      limit=args.limit)
            if not hits:
                print(f"no field matching `{args.target}` in '{project_slug}' — "
                      f"would be flagged as fabricated.")
                sys.exit(1)
            for h in hits:
                marker = ("✓" if h["match_kind"] == "exact" else
                          ("≈" if h["match_kind"] == "cross-model" else "~"))
                sim = (f" sim={h['trigram_sim']:.2f}"
                        if "fuzzy" in h["match_kind"] else "")
                print(f"  {marker} {h['model_name']:20s}.{h['field_name']:25s} "
                      f"[{h['source']}]  {h['source_file']}:{h['start_line']}{sim}")
        else:  # check-model
            hits = resolve_db_model(args.target, project_slug, c,
                                      fuzzy=True, limit=args.limit)
            if not hits:
                print(f"no model named `{args.target}` in '{project_slug}'")
                sys.exit(1)
            for h in hits:
                marker = "✓" if h["match_kind"] == "exact" else "~"
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                print(f"  {marker} {h['model_name']:25s} [{h['source']}]  "
                      f"{h['source_file']}:{h['start_line']}{sim}")


def cmd_env(args):
    """List indexed env vars or resolve a proposed name."""
    from env_indexer import list_env_vars, resolve_env_var
    project_slug = args.project or _project_slug_for_cwd()
    with _conn() as c:
        if args.ecmd == "list":
            rows = list_env_vars(project_slug, c,
                                  source=args.source,
                                  secrets_only=args.secrets_only,
                                  limit=args.limit)
            if not rows:
                print(f"no env vars indexed for '{project_slug}'"); return
            print(f"{len(rows)} env var(s) in '{project_slug}'")
            for r in rows:
                marker = "🔒" if r["is_secret"] else "  "
                sources = ",".join(r["sources"])
                desc = f"  # {r['description']}" if r.get("description") else ""
                file_loc = (f"  {r['canonical_file']}:{r['canonical_line']}"
                            if r.get("canonical_file") else "")
                print(f"  {marker} {r['name']:35s} [{sources}]{file_loc}{desc}")
        else:  # check
            hits = resolve_env_var(args.name, project_slug, c, fuzzy=True,
                                    limit=args.limit)
            if not hits:
                print(f"no env var matching `{args.name}` in '{project_slug}' — "
                      f"would be flagged as fabricated.")
                sys.exit(1)
            for h in hits:
                marker = "✓" if h["match_kind"] == "exact" else "~"
                sim = f" sim={h['trigram_sim']:.2f}" if h["match_kind"] == "fuzzy" else ""
                secret = " 🔒" if h["is_secret"] else ""
                print(f"  {marker} {h['name']:30s} [{h['source']}]{secret}  "
                      f"{h['source_file']}:{h['start_line']}{sim}")


def cmd_about(args):
    """Show who built claude-dejavu, what it does, optionally with
    project stats. No side effects — never captures email."""
    project_slug = args.project or _project_slug_for_cwd()
    print("claude-dejavu — grounding for LLM-generated code")
    print()
    print("Eight grounding domains catch fabricated names BEFORE they hit")
    print("disk: symbols (9 languages), HTTP routes, frontend pages,")
    print("env vars, DB schema (Prisma/Drizzle/TypeORM/SQL), GraphQL,")
    print("feature flags, plus an LSP wire-protocol passthrough.")
    print()
    print("Built by HTE Switzerland — a digital solutions partner.")
    print("  Custom software · Cloud · Web/mobile · UX/UI · Hosting")
    print("  · Outsourcing · Digital coaching")
    print()
    print("If you liked how this is built, that's the kind of work HTE")
    print("delivers.")
    print("    https://hendrikthurau.enterprises")
    print()
    print("Public hallucination-rate benchmark:")
    print("    https://hendrikthurau.enterprises/claude-dejavu/benchmark")
    print()
    print("Source / issues / contributions:")
    print("    https://github.com/HthSolid/claude-dejavu")
    print()
    print("License: FSL-1.1-ALv2 — fair-source, fully functional forever.")
    print("Free for individual + internal corporate use; commercial license")
    print("available — dejavu@hendrikthurau.enterprises. Each version converts")
    print("to Apache 2.0 two years after release.")
    if args.project or args.with_stats:
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("SELECT count(*) FROM symbols WHERE project_slug = %s",
                             (project_slug,))
                sym = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM api_routes WHERE project_slug = %s",
                             (project_slug,))
                rt = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM page_routes WHERE project_slug = %s",
                             (project_slug,))
                pg = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM env_var_decls WHERE project_slug = %s",
                             (project_slug,))
                env = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM db_models WHERE project_slug = %s",
                             (project_slug,))
                mdl = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM graphql_types WHERE project_slug = %s",
                             (project_slug,))
                gql = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM feature_flags WHERE project_slug = %s",
                             (project_slug,))
                fl = cur.fetchone()[0] or 0
            print()
            print(f"━━ what dejavu knows about '{project_slug}' ━━")
            print(f"  symbols       : {sym:>6}")
            print(f"  api routes    : {rt:>6}")
            print(f"  pages         : {pg:>6}")
            print(f"  env vars      : {env:>6}")
            print(f"  db models     : {mdl:>6}")
            print(f"  graphql types : {gql:>6}")
            print(f"  feature flags : {fl:>6}")
        except Exception as e:
            print(f"(project stats unavailable: {type(e).__name__})",
                   file=sys.stderr)


def cmd_doctor(args):
    """Run the diagnostic battery + optionally apply safe auto-fixes."""
    from doctor import run_doctor, report_to_text, report_to_dict
    rep = run_doctor()
    if args.json:
        print(json.dumps(report_to_dict(rep), indent=2, default=str))
    else:
        print(report_to_text(rep))
    if args.fix:
        # Auto-fixes that are safe to attempt non-interactively.
        # Currently: re-run scripts/install.py --auto if install_status is incomplete.
        if rep.install_status and rep.install_status.get("status") == "incomplete":
            print("\n[--fix] re-running scripts/install.py --auto to recover...")
            import subprocess
            cmd = [sys.executable, str(Path(__file__).resolve().parent.parent /
                                       "scripts" / "install.py"), "--auto"]
            subprocess.run(cmd)
    sys.exit({"ok": 0, "warn": 1, "fail": 2}.get(rep.overall, 0))


def cmd_config(args):
    """Read/write claude-dejavu settings in config.env via a typed schema."""
    from config_service import (
        get as cfg_get, set_value as cfg_set, reset as cfg_reset,
        list_settings, list_to_text, SETTINGS,
    )
    action = args.action
    if action == "list":
        rows = list_settings(include_secrets=getattr(args, "show_secrets", False))
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(list_to_text(rows))
    elif action == "get":
        try:
            print(cfg_get(args.key))
        except KeyError as e:
            print(str(e), file=sys.stderr); sys.exit(1)
    elif action == "set":
        try:
            cfg_set(args.key, args.value)
        except (KeyError, ValueError, PermissionError) as e:
            print(str(e), file=sys.stderr); sys.exit(1)
        s = SETTINGS[args.key]
        print(f"  ✓ {args.key} = {args.value}")
        if s.runtime == "next_session":
            print("  (takes effect on next Claude Code session)")
        elif s.runtime == "next_install":
            print("  (read-only via this UI — change requires re-running install.py)")
    elif action == "reset":
        try:
            cfg_reset(args.key)
        except KeyError as e:
            print(str(e), file=sys.stderr); sys.exit(1)
        print(f"  ✓ {args.key} reset to default ({SETTINGS[args.key].default})")


def cmd_outcomes(args):
    """Inspect correction_outcomes — what corrections did we propose, and what stuck?"""
    from symbol_indexer import list_outcomes
    project_slug = args.project or _project_slug_for_cwd() if not args.global_ else None
    with _conn() as c:
        rows = list_outcomes(c, project_slug=project_slug, language=args.language,
                             mode=args.mode, limit=args.limit)
    if not rows:
        print("no outcomes recorded yet"); return
    print(f"{len(rows)} outcome(s):")
    for r in rows:
        ts = r["ts"].strftime("%Y-%m-%d %H:%M") if r.get("ts") else "-"
        proposed = r.get("proposed_name") or "-"
        resolved = r.get("resolved_name") or "-"
        ed = r.get("edit_distance"); ed = f"ed={ed}" if ed is not None else ""
        sim = r.get("trigram_sim"); sim = f"sim={sim:.2f}" if sim is not None else ""
        tags = ",".join(r.get("pattern_tags") or [])
        print(f"  {ts}  {(r.get('mode') or '-'):8s}  {proposed:25s} -> {resolved:25s}  {ed} {sim}  outcome={r.get('outcome')}  [{tags}]")


# ─── Argparse ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(prog="claude-dejavu")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_scope(sp):
        sp.add_argument("--scope", help="'global' | 'project[:name]' | 'workspace[:name]' (default: auto)")

    s = sub.add_parser("search"); s.add_argument("query"); _add_scope(s)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--mode", choices=["grunt", "normal", "full"], default="grunt")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("expand"); s.add_argument("turn_ids", nargs="+", type=int)
    s.set_defaults(func=cmd_expand)

    s = sub.add_parser("sessions"); _add_scope(s); s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("resume"); s.add_argument("hint"); _add_scope(s)
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("errors"); _add_scope(s)
    s.add_argument("--tool"); s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_errors)

    s = sub.add_parser("logs", help="Show recent dejavu error-log records "
                                       "(internal plugin errors, not Bash failures)")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--component",
                     help="filter by component prefix, e.g. indexer.env")
    s.add_argument("--level", choices=("error", "warning", "info"),
                     help="filter by severity")
    s.add_argument("--with-traceback", action="store_true",
                     help="include full Python traceback for each record")
    s.add_argument("--path", action="store_true",
                     help="just print the log file path and exit")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("generate",
                          help="Generate runtime defense modules "
                                 "(env-trap, etc.)")
    gs = s.add_subparsers(dest="gcmd", required=True)
    gt = gs.add_parser("runtime-trap",
                          help="Generate a typed env accessor that "
                                 "throws on undeclared keys at runtime")
    gt.add_argument("--language", default="typescript",
                      choices=("typescript", "ts", "python", "py"))
    gt.add_argument("--output",
                      help="output file path (default depends on "
                             "language: TS → src/lib/env.ts, "
                             "Python → src/lib/dejavu_env.py)")
    gt.add_argument("--project",
                      help="project slug to read indexed env vars from "
                             "(default: cwd)")
    gt.add_argument("--from-file", default=".env.example",
                      help="ALSO include keys from this file "
                             "(default .env.example) in case the index "
                             "is stale")
    gt.set_defaults(func=cmd_generate_runtime_trap)

    s = sub.add_parser("fabrications",
                          help="List names the rewriter has flagged as "
                                 "RESIDUAL/REPLACE; mark each as declared / "
                                 "blacklisted / open after triage")
    s.add_argument("--project",
                     help="project slug; defaults to current cwd")
    s.add_argument("--status", choices=("open", "declared",
                                              "blacklisted"),
                     help="filter by triage status")
    s.add_argument("--min-count", type=int, default=1,
                     help="only show names that have been fabricated "
                            "at least N times")
    s.add_argument("--mark", nargs=3,
                     metavar=("NAME", "DOMAIN", "STATUS"),
                     help="set triage status for a (name, domain) "
                            "pair, e.g. "
                            "`--mark LD_SDK_KEY env declared`")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(func=cmd_fabrications)

    s = sub.add_parser("files"); s.add_argument("path"); _add_scope(s); s.add_argument("--limit", type=int, default=30)
    s.set_defaults(func=cmd_files)

    s = sub.add_parser("restore", help="Restore deleted sessions from off-tree backup")
    s.add_argument("session_uuids", nargs="*", help="specific UUIDs to restore (optional)")
    s.add_argument("--list", action="store_true", help="list restorable sessions, don't restore")
    s.add_argument("--all", action="store_true", help="restore all missing sessions")
    s.add_argument("--since", help="only consider sessions backed up within window (e.g. 7d, 24h)")
    s.set_defaults(func=cmd_restore)

    # v0.3.0: code symbol grounding
    s = sub.add_parser("reindex", help="(Re)index project source files: symbols + routes + semantic")
    s.add_argument("--path", help="project directory (default: cwd)")
    s.add_argument("--project", help="project slug to record under (default: cwd basename)")
    s.add_argument("--force", action="store_true", help="re-parse all files, ignore mtime cache")
    s.add_argument("--skip-routes", action="store_true", help="skip API route indexing")
    s.add_argument("--skip-pages", action="store_true", help="skip frontend page-route indexing")
    s.add_argument("--skip-env", action="store_true", help="skip env var declaration indexing")
    s.add_argument("--include-runtime-dotenv", action="store_true",
                    help="also index .env / .env.local / .env.production (off by default — dev secrets)")
    s.add_argument("--skip-db", action="store_true",
                    help="skip Prisma/Drizzle/TypeORM/SQL schema indexing")
    s.add_argument("--skip-graphql", action="store_true",
                    help="skip GraphQL SDL + inline-gql indexing")
    s.add_argument("--skip-flags", action="store_true",
                    help="skip feature-flag config indexing")
    s.add_argument("--skip-quickwins", action="store_true",
                    help="skip CSS / i18n quick-win indexing (v0.3.14)")
    s.add_argument("--skip-modules", action="store_true",
                    help="skip npm module export indexing (v0.3.16)")
    s.add_argument("--skip-registry", action="store_true",
                    help="skip name registry indexing for prose grounding (v0.4.0)")
    s.add_argument("--skip-semantic", action="store_true", help="skip Weaviate semantic embedding")
    s.set_defaults(func=cmd_reindex)

    s = sub.add_parser("routes", help="List, search, or look up indexed API routes")
    rs = s.add_subparsers(dest="rcmd", required=True)
    rl = rs.add_parser("list", help="list every indexed route")
    rl.add_argument("--project", help="project slug (default: cwd)")
    rl.add_argument("--framework", help="filter by framework (express|fastapi|nestjs|...)")
    rl.add_argument("--method", help="filter by HTTP method")
    rl.add_argument("--limit", type=int, default=200)
    rg = rs.add_parser("get", help="resolve a specific (method, path)")
    rg.add_argument("method")
    rg.add_argument("path")
    rg.add_argument("--project", help="project slug (default: cwd)")
    rg.add_argument("--exact", action="store_true", help="exact match only (no fuzzy)")
    rg.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_routes)

    s = sub.add_parser("pages", help="List or check frontend page routes (Next.js / React Router / Vue Router / Remix / SvelteKit / Astro)")
    ps = s.add_subparsers(dest="pcmd", required=True)
    pl = ps.add_parser("list", help="list every indexed page route")
    pl.add_argument("--project", help="project slug (default: cwd)")
    pl.add_argument("--framework", help="filter by framework")
    pl.add_argument("--kind", help="page|layout|api|loader|error|middleware")
    pl.add_argument("--limit", type=int, default=300)
    pc = ps.add_parser("check", help="check a proposed path for existing duplicates")
    pc.add_argument("path", help="proposed route path, e.g. /dashboard/profile")
    pc.add_argument("--project", help="project slug (default: cwd)")
    pc.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_pages)

    s = sub.add_parser("db", help="Browse / resolve indexed DB schema (Prisma / Drizzle / TypeORM / SQL)")
    ds = s.add_subparsers(dest="dcmd", required=True)
    dm = ds.add_parser("models", help="list every indexed model / table")
    dm.add_argument("--project", help="project slug (default: cwd)")
    dm.add_argument("--source", help="filter by source: prisma|drizzle|typeorm|sql-migration")
    dm.add_argument("--limit", type=int, default=200)
    df = ds.add_parser("fields", help="list every indexed field")
    df.add_argument("--project", help="project slug (default: cwd)")
    df.add_argument("--model", help="filter to one model")
    df.add_argument("--limit", type=int, default=500)
    dc = ds.add_parser("check", help="resolve MODEL.FIELD (or just FIELD across all models)")
    dc.add_argument("target", help="MODEL.FIELD or FIELD")
    dc.add_argument("--project", help="project slug (default: cwd)")
    dc.add_argument("--limit", type=int, default=5)
    dcm = ds.add_parser("check-model", help="resolve a model name (exact or fuzzy)")
    dcm.add_argument("target", help="model name")
    dcm.add_argument("--project", help="project slug (default: cwd)")
    dcm.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_db)

    s = sub.add_parser("graphql", help="Browse / resolve indexed GraphQL types + fields (SDL + inline-gql)")
    gs = s.add_subparsers(dest="gcmd", required=True)
    gt = gs.add_parser("types", help="list every indexed GraphQL type")
    gt.add_argument("--project", help="project slug (default: cwd)")
    gt.add_argument("--kind", help="filter: object|input|enum|interface|union|scalar|schema")
    gt.add_argument("--source", help="filter: sdl-file|inline-gql|codegen")
    gt.add_argument("--limit", type=int, default=200)
    gf = gs.add_parser("fields", help="list every indexed GraphQL field")
    gf.add_argument("--project", help="project slug (default: cwd)")
    gf.add_argument("--type", help="filter to one type")
    gf.add_argument("--limit", type=int, default=500)
    gc = gs.add_parser("check", help="resolve TYPE.FIELD or just FIELD")
    gc.add_argument("target")
    gc.add_argument("--project", help="project slug (default: cwd)")
    gc.add_argument("--limit", type=int, default=5)
    gck = gs.add_parser("check-type", help="resolve a GraphQL type name")
    gck.add_argument("target")
    gck.add_argument("--project", help="project slug (default: cwd)")
    gck.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_gql)

    s = sub.add_parser("ignore", help="Inspect the .dejavuignore filter — list rules or explain why a file is/isn't indexed")
    is_ = s.add_subparsers(dest="icmd", required=True)
    il = is_.add_parser("list", help="show every loaded ignore rule")
    isn = is_.add_parser("status", help="explain why a path would or wouldn't be indexed")
    isn.add_argument("path", help="absolute or relative file path")
    s.set_defaults(func=cmd_ignore)

    s = sub.add_parser("thresholds", help="List or recompute adaptive resolver thresholds (per-project, per-channel)")
    ts_ = s.add_subparsers(dest="tcmd", required=True)
    tl = ts_.add_parser("list", help="show all tuned thresholds")
    tl.add_argument("--project", help="filter by project slug")
    tt = ts_.add_parser("tune", help="recompute thresholds for this project from correction_outcomes")
    tt.add_argument("--project", help="project slug (default: cwd)")
    tt.add_argument("--channel", help="symbol|route|page|env|db|graphql|flag (default: all)")
    s.set_defaults(func=cmd_thresholds)

    s = sub.add_parser("flags", help="Browse / resolve indexed feature flag declarations (flags.json + featureFlags.ts + LD + Unleash + GrowthBook)")
    fs = s.add_subparsers(dest="fcmd", required=True)
    fl = fs.add_parser("list", help="list every indexed flag (deduped by key)")
    fl.add_argument("--project", help="project slug (default: cwd)")
    fl.add_argument("--source", help="filter: flags-json|feature-flags-ts|launchdarkly|unleash|growthbook|source-read")
    fl.add_argument("--kill-switches-only", action="store_true",
                     help="only show flags whose key matches kill/emergency/disable/panic/circuit heuristic")
    fl.add_argument("--limit", type=int, default=500)
    fc = fs.add_parser("check", help="resolve a flag key (exact or fuzzy)")
    fc.add_argument("flag_key")
    fc.add_argument("--project", help="project slug (default: cwd)")
    fc.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_flags)

    s = sub.add_parser("env", help="List or resolve indexed environment variable declarations")
    es = s.add_subparsers(dest="ecmd", required=True)
    el = es.add_parser("list", help="list every indexed env var (deduped by name)")
    el.add_argument("--project", help="project slug (default: cwd)")
    el.add_argument("--source", help="filter by source: env-example|zod-schema|envalid|docker-compose|github-actions|source-read|dotenv-runtime")
    el.add_argument("--secrets-only", action="store_true",
                     help="only show vars matching SECRET/TOKEN/KEY/PASSWORD heuristic")
    el.add_argument("--limit", type=int, default=500)
    ec = es.add_parser("check", help="resolve a proposed env var name (exact or fuzzy)")
    ec.add_argument("name", help="env var name, e.g. DATABASE_URL")
    ec.add_argument("--project", help="project slug (default: cwd)")
    ec.add_argument("--limit", type=int, default=5)
    ep = es.add_parser("propose",
                          help="Append a new env var declaration to "
                                 ".env.example (or similar) — useful "
                                 "for SDK conventions the project "
                                 "hasn't declared yet")
    ep.add_argument("name",
                      help="env var name, e.g. LAUNCHDARKLY_SDK_KEY")
    ep.add_argument("--package",
                      help="NPM/PyPI package whose convention this is "
                             "(used in the inline comment)")
    ep.add_argument("--file", default=".env.example",
                      help="target file (default: .env.example)")
    ep.add_argument("--apply", action="store_true",
                      help="actually write the change (default: dry run)")
    ep.set_defaults(func=cmd_env_propose)
    s.set_defaults(func=cmd_env)

    s = sub.add_parser("symbols", help="List indexed symbols or fuzzy-resolve a name")
    s.add_argument("name", nargs="?", help="symbol name to resolve (fuzzy by default)")
    s.add_argument("--project", help="project slug (default: cwd)")
    s.add_argument("--file", help="filter to a single file path")
    s.add_argument("--kind", choices=["function","class","method","type","interface","enum","const","variable"])
    s.add_argument("--language", choices=["python","typescript","javascript","go","rust","java","ruby","bash","php","c_sharp","kotlin"])
    s.add_argument("--exact", action="store_true", help="exact name match only (no trigram)")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--offset", type=int, default=0)
    s.set_defaults(func=cmd_symbols)

    s = sub.add_parser("lint", help="Lint a proposed edit — flag fabricated names + typos against the symbol index")
    s.add_argument("file", help="target file path (used for language detection)")
    s.add_argument("--project", help="project slug (default: cwd)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--content-file", help="read proposed content from this file")
    g.add_argument("--content-stdin", action="store_true", help="read proposed content from stdin")
    s.add_argument("--show-resolved", action="store_true", help="also print resolved references")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s.set_defaults(func=cmd_lint)

    s = sub.add_parser("about", help="Show who built claude-dejavu + project stats. No side effects.")
    s.add_argument("--project", help="project slug (default: cwd)")
    s.add_argument("--with-stats", action="store_true",
                    help="show indexed-domain counts even if --project is omitted")
    s.set_defaults(func=cmd_about)

    s = sub.add_parser("doctor", help="Diagnose install / runtime health (PG, Weaviate, venv, hooks, ...)")
    s.add_argument("--fix", action="store_true", help="apply safe auto-fixes (e.g. re-run install.py if marker says incomplete)")
    s.add_argument("--json", action="store_true", help="output structured JSON")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("config", help="Read/write claude-dejavu settings (config.env) via a typed schema")
    cs = s.add_subparsers(dest="action", required=True)
    cl = cs.add_parser("list", help="list every known setting + current/default")
    cl.add_argument("--json", action="store_true")
    cl.add_argument("--show-secrets", action="store_true", help="unmask secret values like PG_PASS")
    cg = cs.add_parser("get", help="print the current value of a setting")
    cg.add_argument("key")
    cse = cs.add_parser("set", help="write a value to config.env (validated against the schema)")
    cse.add_argument("key")
    cse.add_argument("value")
    cr = cs.add_parser("reset", help="remove a setting from config.env (revert to its default)")
    cr.add_argument("key")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("outcomes", help="Inspect correction_outcomes (what corrections were proposed)")
    s.add_argument("--project", help="project slug (default: cwd)")
    s.add_argument("--global", dest="global_", action="store_true", help="all projects")
    s.add_argument("--language", choices=["python","typescript","javascript","go","rust","java","ruby","bash","php","c_sharp","kotlin"])
    s.add_argument("--mode", choices=["lookup","lint","warn","suggest","autofix"])
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_outcomes)

    # workspace subcommand: list / create / add / remove / delete
    s = sub.add_parser("workspace")
    sw = s.add_subparsers(dest="action", required=True)
    sl = sw.add_parser("list")
    sc = sw.add_parser("create"); sc.add_argument("name"); sc.add_argument("--projects", required=True); sc.add_argument("--description")
    sa = sw.add_parser("add"); sa.add_argument("name"); sa.add_argument("project")
    sr = sw.add_parser("remove"); sr.add_argument("name"); sr.add_argument("project")
    sd = sw.add_parser("delete"); sd.add_argument("name")
    s.set_defaults(func=cmd_workspace)

    # cloud subcommands: login / logout / status
    from cloud_cli import cmd_login, cmd_logout, cmd_status

    sl = sub.add_parser("login",
                        help="sign in to claude-dejavu cloud (Pro tier)")
    sl.add_argument("--paste", action="store_true",
                    help="skip browser flow; paste a key directly")
    sl.add_argument("--license-url",
                    help="override license server URL (default: "
                         "https://license.hte.digital)")
    sl.set_defaults(func=lambda a: sys.exit(cmd_login(a)))

    sl = sub.add_parser("logout",
                        help="sign out — revoke server-side + erase local key")
    sl.add_argument("--license-url",
                    help="override license server URL")
    sl.add_argument("--skip-revoke", action="store_true",
                    help="erase local key only; don't ask server to revoke")
    sl.set_defaults(func=lambda a: sys.exit(cmd_logout(a)))

    sl = sub.add_parser("status",
                        help="show cloud-tier status (mode, key, quota)")
    sl.add_argument("--cloud", action="store_true",
                    help="(default already shows cloud info; reserved for future "
                         "subsections)")
    sl.set_defaults(func=lambda a: sys.exit(cmd_status(a)))

    # Cloud migration: drops + recreates Weaviate ClaudeDejavuTurn class with
    # vectorizer:none, preserving all existing 1024-dim vectors. One-shot;
    # idempotent (skips if already migrated).
    sl = sub.add_parser("migrate-to-cloud",
                        help="migrate Weaviate Turn class to vectorizer:none "
                             "(needed when fully switching to cloud mode and "
                             "stopping the local transformers container)")
    sl.add_argument("--wv-url", default="http://localhost:8888",
                    help="Weaviate URL (default: http://localhost:8888)")
    sl.add_argument("--apply", action="store_true",
                    help="actually execute (default is dry-run)")
    sl.add_argument("--limit", type=int, default=None,
                    help="cap re-insert (for testing on a subset)")
    def _run_migrate(a):
        import subprocess
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, os.path.join(plugin_dir, "migrate_to_cloud.py"),
               "--wv-url", a.wv_url]
        if a.apply:
            cmd.append("--apply")
        if a.limit is not None:
            cmd.extend(["--limit", str(a.limit)])
        sys.exit(subprocess.call(cmd))
    sl.set_defaults(func=_run_migrate)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
