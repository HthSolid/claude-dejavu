#!/usr/bin/env python3
"""
claude-dejavu — env var indexer (v0.3.6).

Catches the class of bug where Claude generates code that reads an
environment variable that was either typo'd
(`process.env.DATABSE_URL`) or never declared anywhere
(`process.env.TOTALLY_FAKE`). The env_grounding lint strategy resolves
every read against this table.

Source tiers (decreasing authority):

  env-example      .env.example / .env.template / .env.sample
                   The canonical declaration of what THIS project expects.

  zod-schema       T3-stack-style validators that wrap process.env in a
                   typed schema:
                       const env = z.object({ DATABASE_URL: z.string(), ... })
                   Equivalent authority to .env.example (declares contract).

  envalid          envalid-style validators:
                       cleanEnv(process.env, { DATABASE_URL: str() })

  docker-compose   environment: blocks in docker-compose.yml — these
                   declare what the running container expects.

  github-actions   env: blocks in workflow YAML.

  source-read      process.env.X / os.getenv("X") / Deno.env.get("X") /
                   import.meta.env.X reads in source code. Lowest
                   authority — read somewhere ≠ declared anywhere.
                   Aggregated as a fallback so the lint can distinguish
                   "fabricated" from "undeclared but consistent" usage.

Resolution semantics in env_grounding.py:
  exact match in env-example/zod-schema/envalid → resolved (silent)
  exact match only via source-read              → low_confidence
                                                   "read in N other places
                                                    but never declared"
  fuzzy match (sim ≥ 0.6)                       → low_confidence
                                                   "did you mean Y?"
  no match at all                               → unknown (block)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
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


# ─── Patterns ───────────────────────────────────────────────────────────────

# .env line: KEY=value (comments via leading '#'). Allow surrounding whitespace
# and quoted values. We capture only the name; default_value capture is
# best-effort.
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$")
_ENV_COMMENT_RE = re.compile(r"^\s*#\s*(.*)$")

# Heuristic: is this likely a secret based on the name?
_SECRET_HINTS = re.compile(r"(SECRET|TOKEN|KEY|PASSWORD|PRIVATE|CREDENTIAL)", re.I)

# process.env.X / process.env["X"] / process.env['X']
_JS_ENV_RE = re.compile(
    r"(?:process\.env|import\.meta\.env)"
    r"(?:\.([A-Z_][A-Z0-9_]*)|"           # .X
    r"\[\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\])"  # ["X"]
)
# Deno.env.get("X")
_DENO_ENV_RE = re.compile(r"Deno\.env\.get\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\)")
# os.getenv("X") / os.environ["X"] / os.environ.get("X")
_PY_ENV_RE = re.compile(
    r"os\.(?:getenv|environ\.get)\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]"
    r"|os\.environ\[\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\]"
)
# Go: os.Getenv("X") and os.LookupEnv("X")
_GO_ENV_RE = re.compile(r"os\.(?:Getenv|LookupEnv)\(\s*\"([A-Z_][A-Z0-9_]*)\"\s*\)")
# Rust: std::env::var("X") and env::var("X")
_RUST_ENV_RE = re.compile(r"(?:std::)?env::var\(\s*\"([A-Z_][A-Z0-9_]*)\"\s*\)")
# Java: System.getenv("X")
_JAVA_ENV_RE = re.compile(r"System\.getenv\(\s*\"([A-Z_][A-Z0-9_]*)\"\s*\)")
# Ruby: ENV["X"] / ENV.fetch("X")
_RUBY_ENV_RE = re.compile(r"ENV(?:\[\s*['\"]([A-Z_][A-Z0-9_]*)['\"]\s*\]|"
                          r"\.fetch\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"])")
# Bash: ${VAR_NAME} or $VAR_NAME (only on left side of assignment / standalone refs)
_BASH_ENV_RE = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)\}?")

# zod env schema: looks for `z.object({ KEY: z.something(...) })` blocks.
# We rely on tree-sitter for accuracy (regex would over-match), but fall
# back to a regex for projects where tree-sitter isn't available.
_ZOD_KEY_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]+)\s*:\s*z\.")


# ─── Records ────────────────────────────────────────────────────────────────


@dataclass
class EnvVarDecl:
    name: str
    source: str            # 'env-example' | 'zod-schema' | 'envalid' | ...
    source_file: str
    start_line: int = 1
    default_value: str | None = None
    description: str | None = None
    is_secret: bool = False


# ─── .env file parsers ──────────────────────────────────────────────────────


_DOTENV_FILES = (
    ".env.example", ".env.template", ".env.sample",
    ".env.dist", ".env.defaults",
)
_RUNTIME_DOTENV = (
    ".env", ".env.local", ".env.development", ".env.production",
    ".env.staging", ".env.test",
)


def _parse_dotenv(path: Path, *, runtime: bool = False) -> Iterable[EnvVarDecl]:
    """Parse a .env-style file: KEY=value pairs with # comments. Captures
    the comment line immediately above each declaration as description."""
    src = "dotenv-runtime" if runtime else "env-example"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    pending_comment: str | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        cm = _ENV_COMMENT_RE.match(line)
        if cm:
            pending_comment = cm.group(1).strip()
            continue
        if not line.strip():
            pending_comment = None
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            pending_comment = None
            continue
        name = m.group(1)
        raw_val = m.group(2).strip()
        # Strip surrounding quotes for default_value capture.
        if (raw_val.startswith('"') and raw_val.endswith('"')) or \
           (raw_val.startswith("'") and raw_val.endswith("'")):
            default_value = raw_val[1:-1]
        else:
            default_value = raw_val.split("#", 1)[0].strip() or None
        yield EnvVarDecl(
            name=name,
            source=src,
            source_file=str(path),
            start_line=lineno,
            default_value=default_value,
            description=pending_comment,
            is_secret=bool(_SECRET_HINTS.search(name)),
        )
        pending_comment = None


# ─── Zod-schema parser (T3 stack pattern) ───────────────────────────────────


def _parse_zod_schema(path: Path) -> Iterable[EnvVarDecl]:
    """Tree-sitter-walk a TS/JS file looking for z.object({...}) patterns
    that look like env schemas. Heuristic: the object is the argument to
    `z.object(...)`, the file path or surrounding identifier mentions
    'env', and keys are SCREAMING_SNAKE_CASE."""
    if not _HAVE_TS:
        # Regex fallback — find every line that looks like KEY: z.foo(...).
        # This over-matches for non-env zod schemas but the heuristic
        # filter on file name keeps it sane.
        if "env" not in path.name.lower():
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        in_schema = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "z.object(" in line:
                in_schema = True
                continue
            if in_schema and (line.strip().startswith("})") or line.strip() == "}"):
                in_schema = False
                continue
            if in_schema:
                m = _ZOD_KEY_RE.match(line)
                if m:
                    name = m.group(1)
                    yield EnvVarDecl(
                        name=name,
                        source="zod-schema",
                        source_file=str(path),
                        start_line=lineno,
                        is_secret=bool(_SECRET_HINTS.search(name)),
                    )
        return

    # Tree-sitter path.
    ext = path.suffix.lower()
    lang_name = "tsx" if ext == ".tsx" else (
        "typescript" if ext == ".ts" else "javascript"
    )
    try:
        parser = _ts_get_parser(lang_name)
        text = path.read_bytes()
        tree = parser.parse(text)
    except Exception:
        return

    # Walk: find every `call_expression` where function == `z.object` and
    # the first argument is an `object`. For each pair in that object,
    # if the key is SCREAMING_SNAKE_CASE, it's an env var declaration.
    src_b = text

    def _name(node) -> str:
        return src_b[node.start_byte:node.end_byte].decode("utf-8", "ignore")

    # Heuristic gate: filename or call site must hint at env. The zod
    # idiom is z.object({...}) but that's also used for non-env schemas.
    # If the file path contains 'env' OR the object literal has a
    # SCREAMING_SNAKE_CASE majority, accept it.
    file_hints_env = "env" in path.name.lower() or "/env" in str(path).lower()

    def walk(node):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if fn and args and _name(fn) in ("z.object", "object"):
                # First arg should be an object literal.
                for child in args.children:
                    if child.type == "object":
                        keys = []
                        for pair in child.children:
                            if pair.type == "pair":
                                key_node = pair.child_by_field_name("key")
                                if key_node:
                                    k = _name(key_node).strip("\"'`")
                                    keys.append((k, pair.start_point[0] + 1))
                        if not keys:
                            continue
                        # Filter: are most keys SCREAMING_SNAKE_CASE?
                        screaming = [
                            (k, ln) for k, ln in keys
                            if re.fullmatch(r"[A-Z_][A-Z0-9_]*", k)
                        ]
                        if not file_hints_env and len(screaming) < 0.6 * len(keys):
                            continue
                        for name, lineno in screaming:
                            yield EnvVarDecl(
                                name=name,
                                source="zod-schema",
                                source_file=str(path),
                                start_line=lineno,
                                is_secret=bool(_SECRET_HINTS.search(name)),
                            )
        for child in node.children:
            yield from walk(child)

    yield from walk(tree.root_node)


# ─── Docker Compose / GitHub Actions YAML parsers ───────────────────────────


def _parse_docker_compose(path: Path) -> Iterable[EnvVarDecl]:
    """Best-effort YAML scan: look for `environment:` blocks under any
    service. We don't pull in PyYAML — a regex-based line walk gets us
    far enough for the lint use case (verifying NAME exists somewhere
    canonical)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    in_env_block = False
    env_indent = -1
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("environment:"):
            in_env_block = True
            env_indent = indent
            continue
        if in_env_block:
            if indent <= env_indent and stripped.endswith(":"):
                in_env_block = False
                continue
            if indent <= env_indent:
                in_env_block = False
            else:
                # List form: '- KEY=value' or '- KEY'
                # Map form: 'KEY: value'
                m = re.match(r"^-\s*([A-Z_][A-Z0-9_]*)\s*(?:=|$)", stripped)
                if m:
                    name = m.group(1)
                    yield EnvVarDecl(
                        name=name, source="docker-compose",
                        source_file=str(path), start_line=lineno,
                        is_secret=bool(_SECRET_HINTS.search(name)),
                    )
                    continue
                m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*:", stripped)
                if m:
                    name = m.group(1)
                    yield EnvVarDecl(
                        name=name, source="docker-compose",
                        source_file=str(path), start_line=lineno,
                        is_secret=bool(_SECRET_HINTS.search(name)),
                    )


def _parse_github_actions(path: Path) -> Iterable[EnvVarDecl]:
    """env: blocks in GitHub Actions workflow YAML."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    in_env_block = False
    env_indent = -1
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "env:" or stripped.startswith("env:"):
            in_env_block = True
            env_indent = indent
            continue
        if in_env_block:
            if indent <= env_indent:
                in_env_block = False
                continue
            m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*:", stripped)
            if m:
                name = m.group(1)
                yield EnvVarDecl(
                    name=name, source="github-actions",
                    source_file=str(path), start_line=lineno,
                    is_secret=bool(_SECRET_HINTS.search(name)),
                )


# ─── Source-code read aggregation ───────────────────────────────────────────


def extract_env_reads_from_text(text: str, language: str) -> set[str]:
    """Scan raw source for env var reads. Returns the distinct set of
    names referenced. Used both by the indexer (to populate the
    'source-read' tier) and by the env_grounding lint strategy (to know
    what names the proposed edit reads)."""
    out: set[str] = set()
    if language in ("javascript", "typescript", "tsx", "jsx"):
        for m in _JS_ENV_RE.finditer(text):
            n = m.group(1) or m.group(2)
            if n:
                out.add(n)
        for m in _DENO_ENV_RE.finditer(text):
            out.add(m.group(1))
    elif language == "python":
        for m in _PY_ENV_RE.finditer(text):
            n = m.group(1) or m.group(2)
            if n:
                out.add(n)
    elif language == "go":
        for m in _GO_ENV_RE.finditer(text):
            out.add(m.group(1))
    elif language == "rust":
        for m in _RUST_ENV_RE.finditer(text):
            out.add(m.group(1))
    elif language == "java":
        for m in _JAVA_ENV_RE.finditer(text):
            out.add(m.group(1))
    elif language == "ruby":
        for m in _RUBY_ENV_RE.finditer(text):
            n = m.group(1) or m.group(2)
            if n:
                out.add(n)
    elif language == "bash":
        # Bash refs are noisy ($PATH, $HOME, etc.). Skip the well-known
        # POSIX/shell built-ins that aren't user-declared env vars.
        for m in _BASH_ENV_RE.finditer(text):
            n = m.group(1)
            if n in _BASH_BUILTIN_VARS:
                continue
            out.add(n)
    return out


_BASH_BUILTIN_VARS = {
    "PATH", "HOME", "USER", "SHELL", "PWD", "OLDPWD", "PS1", "PS2",
    "IFS", "TERM", "LANG", "LC_ALL", "EDITOR", "VISUAL", "DISPLAY",
    "TMPDIR", "RANDOM", "SECONDS", "LINENO", "BASH", "BASH_VERSION",
    "BASH_SOURCE", "FUNCNAME", "BASHPID", "EUID", "UID", "GROUPS",
    "PIPESTATUS", "REPLY", "OPTARG", "OPTIND", "HOSTNAME",
    "OSTYPE", "MACHTYPE", "HOSTTYPE",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",  # positional
}


_LANG_BY_EXT = {
    ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "jsx", ".mjs": "javascript", ".cjs": "javascript",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".sh": "bash", ".bash": "bash",
}


def _scan_source_reads(repo_root: Path, project_slug: str,
                          flt=None) -> Iterable[EnvVarDecl]:
    """Walk the repo, extract every (name, source_file, line) where an
    env var is read, and emit one EnvVarDecl per file × name pair."""
    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(repo_root)
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames
                        if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
        filenames = [f for f in filenames
                       if not flt.should_skip(Path(dirpath) / f, is_dir=False)]
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            lang = _LANG_BY_EXT.get(ext)
            if not lang:
                continue
            full = Path(dirpath) / f
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            names = extract_env_reads_from_text(text, lang)
            if not names:
                continue
            # For each name, find the first line that mentions it (cheap
            # — gives the user a clickable jump target).
            for name in names:
                first_line = 1
                for i, ln in enumerate(text.splitlines(), start=1):
                    if name in ln:
                        first_line = i
                        break
                yield EnvVarDecl(
                    name=name,
                    source="source-read",
                    source_file=str(full),
                    start_line=first_line,
                    is_secret=bool(_SECRET_HINTS.search(name)),
                )


# ─── Top-level project walk ─────────────────────────────────────────────────


_SKIP_DIRS = {
    "node_modules", ".git", ".next", "dist", "build", "out", "target",
    "__pycache__", ".venv", "venv", "vendor", ".pytest_cache",
    ".mypy_cache", ".turbo", "coverage",
}


def index_project_env_vars(project_dir: str, project_slug: str, conn,
                             *, force: bool = False,
                             include_runtime_dotenv: bool = False,
                             skip_source_reads: bool = False,
                             flt=None) -> dict:
    """Walk a project, collect EnvVarDecls across all known sources, and
    persist them to env_var_decls. Full-replace per project (simpler
    than per-file delta — env files change rarely).

    `include_runtime_dotenv`: also index .env / .env.local / .env.development.
    Default OFF to avoid flagging dev-only secrets as canonical
    declarations.

    `skip_source_reads`: skip the source-code aggregation pass. Useful
    when you want only the canonical sources (env-example + zod-schema).
    """
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"error": f"not_a_directory: {root}"}

    decls: list[EnvVarDecl] = []

    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(root)

    # ─── v0.4.5: fingerprint cache ─────────────────────────────────────
    # env_indexer walks every TS/JS/Py file for `process.env.X` reads.
    # On an internal project that's 8k+ files. Skip if nothing changed.
    from file_state import IndexerCache, files_under
    relevant = list(files_under(
        root,
        suffixes={".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py",
                   ".go", ".rs", ".java", ".rb", ".sh", ".bash"},
        filenames={".env.example", ".env.template", ".env.sample",
                    ".env.dist", ".env.defaults",
                    "docker-compose.yml", "docker-compose.yaml"},
        flt=flt,
    ))
    cache = IndexerCache(conn, project_slug, "env")
    if not force and cache.is_fresh(relevant):
        return cache.cached_stats_marked()

    # 1. Dotenv-style files at the project root + common sub-folders.
    for d in (root, root / "apps" / "web", root / "packages",
              root / "config", root / "envs"):
        if not d.is_dir():
            continue
        for fname in os.listdir(d):
            full = d / fname
            if not full.is_file():
                continue
            if fname in _DOTENV_FILES:
                decls.extend(_parse_dotenv(full, runtime=False))
            elif include_runtime_dotenv and fname in _RUNTIME_DOTENV:
                decls.extend(_parse_dotenv(full, runtime=True))

    # 2. Zod / envalid schema files. Heuristic walk: any TS/JS file in
    # the repo whose name contains 'env' (env.ts, server-env.ts,
    # env.mjs, env.config.ts, etc.) is a candidate.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                        if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
        filenames = [f for f in filenames
                       if not flt.should_skip(Path(dirpath) / f, is_dir=False)]
        for f in filenames:
            if not re.search(r"env\b", f.lower()):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext not in (".ts", ".tsx", ".js", ".mjs", ".cjs"):
                continue
            decls.extend(_parse_zod_schema(Path(dirpath) / f))

    # 3. docker-compose.yml / docker-compose.*.yml
    for f in os.listdir(root):
        if f == "docker-compose.yml" or (
            f.startswith("docker-compose.") and f.endswith((".yml", ".yaml"))
        ):
            decls.extend(_parse_docker_compose(root / f))

    # 4. GitHub Actions workflows.
    gh = root / ".github" / "workflows"
    if gh.is_dir():
        for f in os.listdir(gh):
            if f.endswith((".yml", ".yaml")):
                decls.extend(_parse_github_actions(gh / f))

    # 5. Source-read aggregation (lowest-authority tier).
    if not skip_source_reads:
        decls.extend(_scan_source_reads(root, project_slug, flt=flt))

    # Persist — full-replace per project.
    by_source: dict[str, int] = {}
    secret_count = 0
    seen: set[tuple[str, str, str]] = set()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM env_var_decls WHERE project_slug = %s", (project_slug,))
        for d in decls:
            # Dedup on (name, source, source_file) — same file can ref
            # the same var on multiple lines; keep the first.
            key = (d.name, d.source, d.source_file)
            if key in seen:
                continue
            seen.add(key)
            cur.execute(
                """INSERT INTO env_var_decls
                   (project_slug, name, source, source_file, start_line,
                    default_value, description, is_secret, indexed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (project_slug, name, source, source_file)
                     DO NOTHING""",
                (project_slug, d.name, d.source, d.source_file, d.start_line,
                 d.default_value, d.description, d.is_secret),
            )
            by_source[d.source] = by_source.get(d.source, 0) + 1
            if d.is_secret:
                secret_count += 1

    stats = {
        "env_vars_added": sum(by_source.values()),
        "by_source": by_source,
        "secrets_flagged": secret_count,
    }
    cache.store(relevant, stats=stats)
    return stats


# ─── Resolution ─────────────────────────────────────────────────────────────


_CANONICAL_SOURCES = ("env-example", "zod-schema", "envalid",
                       "docker-compose", "github-actions")


def resolve_env_var(name: str, project_slug: str, conn, *,
                     fuzzy: bool = True, limit: int = 5,
                     exclude_sources: tuple[str, ...] = ()) -> list[dict]:
    """Resolve `name` against env_var_decls. Returns a ranked list of
    matches. The caller (env_grounding strategy) interprets the
    `match_kind` and `source` to pick a severity:

      match_kind=exact AND source ∈ canonical → resolved (silent)
      match_kind=exact AND source=source-read → low_confidence
                                                ('declared nowhere canonical')
      match_kind=fuzzy                        → low_confidence
                                                ('did you mean ...')
      no result                               → caller emits unknown
    """
    out: list[dict] = []
    seen: set[int] = set()
    excl = tuple(exclude_sources)
    with conn.cursor() as cur:
        # 1. Exact match (any source). If multiple, prefer canonical sources.
        if excl:
            cur.execute(
                """SELECT id, name, source, source_file, start_line,
                          default_value, description, is_secret
                   FROM env_var_decls
                   WHERE project_slug = %s AND name = %s AND source != ALL(%s)
                   ORDER BY (source IN %s) DESC, source, source_file
                   LIMIT %s""",
                (project_slug, name, list(excl), _CANONICAL_SOURCES, limit),
            )
        else:
            cur.execute(
                """SELECT id, name, source, source_file, start_line,
                          default_value, description, is_secret
                   FROM env_var_decls
                   WHERE project_slug = %s AND name = %s
                   ORDER BY (source IN %s) DESC, source, source_file
                   LIMIT %s""",
                (project_slug, name, _CANONICAL_SOURCES, limit),
            )
        for row in cur.fetchall():
            seen.add(row[0])
            out.append({
                "id": row[0], "name": row[1], "source": row[2],
                "source_file": row[3], "start_line": row[4],
                "default_value": row[5], "description": row[6],
                "is_secret": row[7],
                "match_kind": "exact",
                "trigram_sim": 1.0,
            })
        if out:
            return out

        # 2. Fuzzy via pg_trgm. Threshold tuned for env names —
        # SCREAMING_SNAKE_CASE makes them shorter on average than
        # function names, so the default 0.4 is fine, but raising the
        # floor to 0.5 reduces noise.
        if fuzzy:
            try:
                from adaptive_threshold import get_threshold
                t = get_threshold(project_slug, "env", conn)
            except Exception:
                t = 0.5
            cur.execute("SELECT set_limit(%s)", (t,))
            if excl:
                cur.execute(
                    """SELECT id, name, source, source_file, start_line,
                              default_value, description, is_secret,
                              similarity(name, %s) AS sim
                       FROM env_var_decls
                       WHERE project_slug = %s
                         AND name %% %s
                         AND source != ALL(%s)
                       ORDER BY sim DESC, (source IN %s) DESC
                       LIMIT %s""",
                    (name, project_slug, name, list(excl),
                     _CANONICAL_SOURCES, limit),
                )
            else:
                cur.execute(
                    """SELECT id, name, source, source_file, start_line,
                              default_value, description, is_secret,
                              similarity(name, %s) AS sim
                       FROM env_var_decls
                       WHERE project_slug = %s
                         AND name %% %s
                       ORDER BY sim DESC, (source IN %s) DESC
                       LIMIT %s""",
                    (name, project_slug, name, _CANONICAL_SOURCES, limit),
                )
            for row in cur.fetchall():
                if row[0] in seen:
                    continue
                out.append({
                    "id": row[0], "name": row[1], "source": row[2],
                    "source_file": row[3], "start_line": row[4],
                    "default_value": row[5], "description": row[6],
                    "is_secret": row[7],
                    "match_kind": "fuzzy",
                    "trigram_sim": float(row[8] or 0),
                })
    return out


def list_env_vars(project_slug: str, conn, *,
                   source: str | None = None,
                   secrets_only: bool = False,
                   limit: int = 500,
                   offset: int = 0) -> list[dict]:
    """List indexed env vars. Collapses multiple-source rows for the same
    name into one row with a `sources` list."""
    where = ["project_slug = %s"]
    params: list = [project_slug]
    if source:
        where.append("source = %s"); params.append(source)
    if secrets_only:
        where.append("is_secret = TRUE")
    canonical_set = "(source IN ('env-example','zod-schema','envalid'))"
    sql = (f"SELECT name, array_agg(DISTINCT source ORDER BY source) AS sources, "
           f"       max(default_value) AS default_value, "
           f"       max(description) AS description, "
           f"       bool_or(is_secret) AS is_secret, "
           f"       (array_agg(source_file ORDER BY {canonical_set} DESC, source))[1] AS canonical_file, "
           f"       (array_agg(start_line ORDER BY {canonical_set} DESC, source))[1] AS canonical_line "
           f"FROM env_var_decls WHERE {' AND '.join(where)} "
           f"GROUP BY name ORDER BY name LIMIT %s OFFSET %s")
    params += [limit, offset]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ─── CLI ────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from recall import _conn  # type: ignore

    p = argparse.ArgumentParser(description="Standalone env_indexer for testing.")
    p.add_argument("project_dir")
    p.add_argument("project_slug")
    p.add_argument("--include-runtime", action="store_true")
    p.add_argument("--skip-reads", action="store_true")
    p.add_argument("--resolve", help="resolve a name after indexing")
    args = p.parse_args()

    with _conn() as c:
        c.autocommit = True
        stats = index_project_env_vars(args.project_dir, args.project_slug, c,
                                        include_runtime_dotenv=args.include_runtime,
                                        skip_source_reads=args.skip_reads)
        print(stats)
        if args.resolve:
            hits = resolve_env_var(args.resolve, args.project_slug, c)
            print(f"resolve({args.resolve!r}) →")
            for h in hits:
                print(f"  {h['match_kind']:6s} {h['name']:25s} [{h['source']}]  "
                      f"{h['source_file']}:{h['start_line']}  "
                      f"sim={h.get('trigram_sim', 0):.2f}")
