#!/usr/bin/env python3
"""
claude-dejavu — feature flag indexer (v0.3.10).

Catches `client.variation('typo-key', ...)` / `isEnabled('typo-key')` /
`checkGate('typo-key')` / `getFeatureFlag('typo-key')` against the
project's declared flags.

Sources (decreasing authority):

  flags-json         flags.json / featureFlags.json / feature-flags.json
                     in project root or a `config/` / `flags/` subfolder.
                     Treated as canonical: every top-level key is a flag.
  feature-flags-ts   `featureFlags.ts` / `feature-flags.ts` modules
                     exporting an object literal of flag-keyed defaults.
  launchdarkly       *.ld.json / launchdarkly-*.json — the LaunchDarkly
                     CLI's exported flag config files.
  unleash            unleash.json / unleash.config.{json,yaml}
  growthbook         growthbook.json — GrowthBook export
  source-read        Aggregated from `client.variation('K', …)` /
                     `isEnabled('K')` / `checkGate('K')` / etc. at all
                     call sites. Lowest authority — used as a fallback
                     "is this name at least seen anywhere".

Resolution semantics in flag_grounding.py:
  exact match in canonical (anything not source-read) → resolved
  exact match only via source-read → low_confidence
                                     "used somewhere but never declared"
  fuzzy match (sim ≥ 0.5) → low_confidence (typo)
  no match → unknown (block)
"""
from __future__ import annotations

import json
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


_KILL_SWITCH_HINTS = re.compile(r"(kill|emergency|disable|panic|circuit|killswitch)", re.I)


@dataclass
class FlagDecl:
    flag_key: str
    source: str
    source_file: str
    start_line: int = 1
    default_value: str | None = None
    description: str | None = None
    is_kill_switch: bool = False


# ─── JSON file parsing ─────────────────────────────────────────────────────


_FLAG_JSON_NAMES = {
    "flags.json", "featureflags.json", "feature-flags.json",
    "feature_flags.json", "growthbook.json",
    "unleash.json",
}


def _parse_flag_json(path: Path, source: str) -> Iterable[FlagDecl]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return
    # Recognize a few common shapes:
    # 1. { "flagKey": defaultValue, ... }            (flat dict)
    # 2. { "features": [{"name":"...","defaultValue":...}, ...] }  (LaunchDarkly export)
    # 3. { "flags": [{"key":"...","value":...}, ...] }              (Unleash export)
    # 4. { "features": [{"key":"...","defaultValue":..., "description":"..."}, ...] }  (GrowthBook)
    if isinstance(data, dict):
        if "features" in data and isinstance(data["features"], list):
            for f in data["features"]:
                if not isinstance(f, dict):
                    continue
                key = f.get("name") or f.get("key")
                if not key:
                    continue
                yield FlagDecl(
                    flag_key=key, source=source,
                    source_file=str(path), start_line=1,
                    default_value=json.dumps(f.get("defaultValue")) if "defaultValue" in f else None,
                    description=f.get("description"),
                    is_kill_switch=bool(_KILL_SWITCH_HINTS.search(key)),
                )
            return
        if "flags" in data and isinstance(data["flags"], list):
            for f in data["flags"]:
                if not isinstance(f, dict):
                    continue
                key = f.get("key") or f.get("name")
                if not key:
                    continue
                yield FlagDecl(
                    flag_key=key, source=source,
                    source_file=str(path), start_line=1,
                    default_value=json.dumps(f.get("value") or f.get("defaultValue")) if ("value" in f or "defaultValue" in f) else None,
                    description=f.get("description"),
                    is_kill_switch=bool(_KILL_SWITCH_HINTS.search(key)),
                )
            return
        # Flat dict: every top-level key is a flag (skip metadata-looking keys).
        for k, v in data.items():
            if k.startswith("$") or k in ("$schema", "version", "metadata"):
                continue
            yield FlagDecl(
                flag_key=k, source=source,
                source_file=str(path), start_line=1,
                default_value=(json.dumps(v) if not isinstance(v, dict) else None),
                description=(v.get("description") if isinstance(v, dict) else None),
                is_kill_switch=bool(_KILL_SWITCH_HINTS.search(k)),
            )


# ─── TS feature-flags.ts parser ─────────────────────────────────────────────


def _parse_feature_flags_ts(path: Path) -> Iterable[FlagDecl]:
    """Walk a TS file for an exported object literal of flag-keyed defaults.
    Patterns we recognize:
        export const featureFlags = { 'kebab-key': false, anotherKey: { … } }
        export default { ... }
        export const flags = { ... }

    The keys are quoted strings or identifiers. We only emit keys whose
    name LOOKS like a flag (kebab-case OR camelCase OR snake_case)."""
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

    def _name(node) -> str:
        return src_b[node.start_byte:node.end_byte].decode("utf-8", "ignore")

    # Look for variable_declarators whose name matches `*flag*` and whose
    # value is an object_literal. Top-level keys become flags.
    flag_const_names = ("flags", "featureflags", "feature_flags",
                         "featureflagsdefaults", "flagdefaults")

    def walk(node):
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            value_node = node.child_by_field_name("value")
            if not (name_node and value_node):
                pass
            else:
                const_name = _name(name_node).lower()
                if any(fn in const_name for fn in flag_const_names) \
                        and value_node.type == "object":
                    for pair in value_node.children:
                        if pair.type != "pair":
                            continue
                        kn = pair.child_by_field_name("key")
                        vn = pair.child_by_field_name("value")
                        if not kn:
                            continue
                        key = _name(kn).strip("\"'`")
                        # Sanity: only allow identifier-ish or kebab/snake keys.
                        if not re.fullmatch(r"[A-Za-z][\w\-]*", key):
                            continue
                        default_value = _name(vn).strip() if vn else None
                        yield FlagDecl(
                            flag_key=key, source="feature-flags-ts",
                            source_file=str(path),
                            start_line=pair.start_point[0] + 1,
                            default_value=default_value,
                            is_kill_switch=bool(_KILL_SWITCH_HINTS.search(key)),
                        )
        for child in node.children:
            yield from walk(child)

    yield from walk(tree.root_node)


# ─── Source-read aggregation ────────────────────────────────────────────────


# Patterns we recognize at call sites.
_FLAG_CALL_PATTERNS = [
    # LaunchDarkly: client.variation('key', ...) | client.variationDetail
    re.compile(r"\.\s*variation(?:Detail)?\s*\(\s*['\"]([\w\-./]+)['\"]"),
    # Unleash: isEnabled('key', ...) | unleash.isEnabled('key', ...)
    re.compile(r"\bisEnabled\s*\(\s*['\"]([\w\-./]+)['\"]"),
    # Statsig: Statsig.checkGate('key') / .getConfig('key') / .getFeatureGate
    re.compile(r"Statsig\.\s*(?:checkGate|getConfig|getFeatureGate|checkGateSync)\s*\(\s*['\"]([\w\-./]+)['\"]"),
    # PostHog: posthog.isFeatureEnabled('key') / .getFeatureFlag('key')
    re.compile(r"posthog\.\s*(?:isFeatureEnabled|getFeatureFlag)\s*\(\s*['\"]([\w\-./]+)['\"]"),
    # GrowthBook: gb.feature('key') / gb.evalFeature('key') / gb.isOn('key')
    re.compile(r"\b(?:gb|growthbook)\.\s*(?:feature|evalFeature|isOn|isOff|getFeatureValue)\s*\(\s*['\"]([\w\-./]+)['\"]"),
    # Generic: featureFlags['key'] / flags['key'] / flags.get('key')
    re.compile(r"(?:featureFlags|flags)\s*\[\s*['\"]([\w\-./]+)['\"]\s*\]"),
    re.compile(r"(?:featureFlags|flags)\.\s*get\s*\(\s*['\"]([\w\-./]+)['\"]"),
]


def extract_flag_reads_from_text(text: str) -> set[tuple[str, int]]:
    """Return (flag_key, line_no) for every flag call site in `text`."""
    out: set[tuple[str, int]] = set()
    for rx in _FLAG_CALL_PATTERNS:
        for m in rx.finditer(text):
            line_no = text[:m.start()].count("\n") + 1
            out.add((m.group(1), line_no))
    return out


# ─── Project walk ───────────────────────────────────────────────────────────


_SKIP_DIRS = {
    "node_modules", ".git", ".next", "dist", "build", "out", "target",
    "__pycache__", ".venv", "venv", "vendor", ".pytest_cache",
    ".mypy_cache", ".turbo", "coverage",
}


def index_project_flags(project_dir: str, project_slug: str, conn,
                          *, force: bool = False, flt=None) -> dict:
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"error": f"not_a_directory: {root}"}

    decls: list[FlagDecl] = []

    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(root)

    # ─── v0.4.5: fingerprint cache ─────────────────────────────────────
    from file_state import IndexerCache, files_under
    relevant = list(files_under(
        root,
        suffixes={".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".json"},
        flt=flt,
    ))
    cache = IndexerCache(conn, project_slug, "flag")
    if not force and cache.is_fresh(relevant):
        return cache.cached_stats_marked()

    # Walk filesystem.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                        if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
        filenames = [f for f in filenames
                       if not flt.should_skip(Path(dirpath) / f, is_dir=False)]
        for f in filenames:
            full = Path(dirpath) / f
            name_lower = f.lower()
            ext = full.suffix.lower()

            # JSON canonical configs.
            if name_lower in _FLAG_JSON_NAMES:
                src = "flags-json"
                if "growthbook" in name_lower:
                    src = "growthbook"
                elif "unleash" in name_lower:
                    src = "unleash"
                decls.extend(_parse_flag_json(full, src))
                continue
            if name_lower.endswith(".ld.json") or name_lower.startswith("launchdarkly"):
                decls.extend(_parse_flag_json(full, "launchdarkly"))
                continue

            # TS / JS feature-flags modules.
            if ext in (".ts", ".tsx", ".js", ".mjs", ".cjs"):
                # Cheap text-based gate.
                if not re.search(r"(featureflags?|feature-flags|flagdefaults)",
                                  name_lower):
                    continue
                decls.extend(_parse_feature_flags_ts(full))

    # Source-read aggregation: pass over every TS/JS/TSX/Python file.
    source_read_decls: dict[tuple[str, str], FlagDecl] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                        if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
        filenames = [f for f in filenames
                       if not flt.should_skip(Path(dirpath) / f, is_dir=False)]
        for f in filenames:
            if not f.endswith((".ts", ".tsx", ".js", ".mjs", ".cjs", ".py")):
                continue
            full = Path(dirpath) / f
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for key, line_no in extract_flag_reads_from_text(text):
                composite = (key, str(full))
                if composite in source_read_decls:
                    continue
                source_read_decls[composite] = FlagDecl(
                    flag_key=key, source="source-read",
                    source_file=str(full), start_line=line_no,
                    is_kill_switch=bool(_KILL_SWITCH_HINTS.search(key)),
                )
    decls.extend(source_read_decls.values())

    # Persist — full-replace per project.
    by_source: dict[str, int] = {}
    kill_switch_count = 0
    seen: set[tuple[str, str, str]] = set()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM feature_flags WHERE project_slug = %s",
                    (project_slug,))
        for d in decls:
            key = (d.flag_key, d.source, d.source_file)
            if key in seen:
                continue
            seen.add(key)
            cur.execute(
                """INSERT INTO feature_flags
                   (project_slug, flag_key, source, source_file, start_line,
                    default_value, description, is_kill_switch, indexed_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (project_slug, flag_key, source, source_file)
                     DO NOTHING""",
                (project_slug, d.flag_key, d.source, d.source_file,
                 d.start_line, d.default_value, d.description,
                 d.is_kill_switch),
            )
            by_source[d.source] = by_source.get(d.source, 0) + 1
            if d.is_kill_switch:
                kill_switch_count += 1
    stats = {
        "flags_added": sum(by_source.values()),
        "by_source": by_source,
        "kill_switches_flagged": kill_switch_count,
    }
    cache.store(relevant, stats=stats)
    return stats


# ─── Resolution ─────────────────────────────────────────────────────────────


_CANONICAL_FLAG_SOURCES = ("flags-json", "feature-flags-ts", "launchdarkly",
                            "unleash", "growthbook")


def resolve_flag(flag_key: str, project_slug: str, conn, *,
                  fuzzy: bool = True, limit: int = 5,
                  exclude_sources: tuple[str, ...] = ()) -> list[dict]:
    out: list[dict] = []
    seen: set[int] = set()
    excl = tuple(exclude_sources)
    with conn.cursor() as cur:
        if excl:
            cur.execute(
                """SELECT id, flag_key, source, source_file, start_line,
                          default_value, description, is_kill_switch
                   FROM feature_flags
                   WHERE project_slug = %s AND flag_key = %s
                     AND source != ALL(%s)
                   ORDER BY (source IN %s) DESC, source LIMIT %s""",
                (project_slug, flag_key, list(excl),
                 _CANONICAL_FLAG_SOURCES, limit),
            )
        else:
            cur.execute(
                """SELECT id, flag_key, source, source_file, start_line,
                          default_value, description, is_kill_switch
                   FROM feature_flags
                   WHERE project_slug = %s AND flag_key = %s
                   ORDER BY (source IN %s) DESC, source LIMIT %s""",
                (project_slug, flag_key, _CANONICAL_FLAG_SOURCES, limit),
            )
        for row in cur.fetchall():
            seen.add(row[0])
            out.append({
                "id": row[0], "flag_key": row[1], "source": row[2],
                "source_file": row[3], "start_line": row[4],
                "default_value": row[5], "description": row[6],
                "is_kill_switch": row[7],
                "match_kind": "exact", "trigram_sim": 1.0,
            })
        if out:
            return out
        if fuzzy:
            try:
                from adaptive_threshold import get_threshold
                t = get_threshold(project_slug, "flag", conn)
            except Exception:
                t = 0.5
            cur.execute("SELECT set_limit(%s)", (t,))
            if excl:
                cur.execute(
                    """SELECT id, flag_key, source, source_file, start_line,
                              default_value, description, is_kill_switch,
                              similarity(flag_key, %s) AS sim
                       FROM feature_flags
                       WHERE project_slug = %s
                         AND flag_key %% %s
                         AND source != ALL(%s)
                       ORDER BY sim DESC, (source IN %s) DESC LIMIT %s""",
                    (flag_key, project_slug, flag_key, list(excl),
                     _CANONICAL_FLAG_SOURCES, limit),
                )
            else:
                cur.execute(
                    """SELECT id, flag_key, source, source_file, start_line,
                              default_value, description, is_kill_switch,
                              similarity(flag_key, %s) AS sim
                       FROM feature_flags
                       WHERE project_slug = %s
                         AND flag_key %% %s
                       ORDER BY sim DESC, (source IN %s) DESC LIMIT %s""",
                    (flag_key, project_slug, flag_key,
                     _CANONICAL_FLAG_SOURCES, limit),
                )
            for row in cur.fetchall():
                if row[0] in seen:
                    continue
                out.append({
                    "id": row[0], "flag_key": row[1], "source": row[2],
                    "source_file": row[3], "start_line": row[4],
                    "default_value": row[5], "description": row[6],
                    "is_kill_switch": row[7],
                    "match_kind": "fuzzy",
                    "trigram_sim": float(row[8] or 0),
                })
    return out


def list_flags(project_slug: str, conn, *, source: str | None = None,
                kill_switches_only: bool = False, limit: int = 500) -> list[dict]:
    where = ["project_slug = %s"]
    params: list = [project_slug]
    if source:
        where.append("source = %s"); params.append(source)
    if kill_switches_only:
        where.append("is_kill_switch = TRUE")
    sql = (f"SELECT flag_key, "
           f"       array_agg(DISTINCT source ORDER BY source) AS sources, "
           f"       max(default_value) AS default_value, "
           f"       max(description) AS description, "
           f"       bool_or(is_kill_switch) AS is_kill_switch, "
           f"       (array_agg(source_file ORDER BY source))[1] AS canonical_file, "
           f"       (array_agg(start_line ORDER BY source))[1] AS canonical_line "
           f"FROM feature_flags WHERE {' AND '.join(where)} "
           f"GROUP BY flag_key ORDER BY flag_key LIMIT %s")
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
