#!/usr/bin/env python3
"""
claude-dejavu — name registry indexer (v0.4.0).

Generalist grounding: catches typos in proper nouns, defined terms,
citation keys, character/place/concept names — anything that should
be consistent across a structured creative or professional document.

Source files (any project root):

  name_registry.json / name-registry.json
    Schema (flexible — detects shape):
      Form 1: {"characters": ["Aria", "Kael"], "places": ["Whitestone"], …}
      Form 2: [{"name": "Aria", "kind": "character", "aliases": ["The Veilweaver"]}, …]

  name_registry.yaml / name-registry.yaml
    Same structure, YAML syntax.

  *.bib (BibTeX) — extracts citation keys.

The lint strategy `prose_grounding` then reads `.md` / `.txt` /
`.tex` / `.rst` / `.org` content + flags Capitalized words and
quoted defined-terms that don't match registry entries.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable

try:
    import yaml  # type: ignore
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False


_REGISTRY_FILENAMES = {
    "name_registry.json", "name-registry.json", "names.json",
    "name_registry.yaml", "name-registry.yaml", "names.yaml",
    "name_registry.yml",  "name-registry.yml",  "names.yml",
}

# Heuristic: when reading Form 1 (key → list), map plural section names
# to canonical kind values.
_KIND_FROM_KEY = {
    "characters": "character", "people": "character", "actors": "character",
    "places": "place", "locations": "place", "settings": "place",
    "terms": "term", "glossary": "term", "definitions": "term",
    "citations": "citation", "references": "citation", "bibliography": "citation",
    "concepts": "concept", "ideas": "concept",
    "brands": "brand", "products": "brand", "trademarks": "brand",
    "parties": "party", "entities": "party",  # legal documents
    "drugs": "term", "diseases": "term",       # medical
}


def parse_registry_file(path: Path) -> Iterable[tuple[str, str, list[str], str | None]]:
    """Yield (name, kind, aliases, description) tuples."""
    name_lower = path.name.lower()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if name_lower.endswith((".yaml", ".yml")):
        if not _HAVE_YAML:
            return
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return

    if isinstance(data, dict):
        # Form 1: section keys map to lists of names.
        for section, vals in data.items():
            kind = _KIND_FROM_KEY.get(section.lower(), "other")
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, str):
                        yield (v, kind, [], None)
                    elif isinstance(v, dict):
                        nm = v.get("name") or v.get("title")
                        if nm:
                            yield (nm, v.get("kind", kind),
                                    v.get("aliases") or [],
                                    v.get("description"))
            elif isinstance(vals, dict):
                # nested dict: keys are names
                for nm, sub in vals.items():
                    desc = sub.get("description") if isinstance(sub, dict) else None
                    aliases = (sub.get("aliases") or []) if isinstance(sub, dict) else []
                    yield (nm, kind, aliases, desc)
    elif isinstance(data, list):
        # Form 2: flat array of {name, kind, ...} dicts.
        for v in data:
            if isinstance(v, dict):
                nm = v.get("name") or v.get("title")
                if nm:
                    yield (nm, v.get("kind", "other"),
                            v.get("aliases") or [],
                            v.get("description"))


_BIBTEX_KEY_RE = re.compile(r"@\w+\s*\{\s*([\w\-:./]+)\s*,")


def parse_bibtex(path: Path) -> Iterable[tuple[str, str, list[str], str | None]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for m in _BIBTEX_KEY_RE.finditer(text):
        yield (m.group(1), "citation", [], None)


def index_project_name_registry(project_dir: str, project_slug: str,
                                  conn, *, flt=None) -> dict:
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"error": f"not_a_directory: {root}"}

    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(root)

    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()

    # Fast-path: if there's no name_registry file at the project root
    # AND no .bib file anywhere shallow, skip the full-tree walk. Saves
    # ~3 minutes on a 8k-file repo where prose grounding isn't in use.
    has_registry = False
    try:
        for entry in os.scandir(root):
            if entry.is_file():
                lo = entry.name.lower()
                if lo in _REGISTRY_FILENAMES or lo.endswith(".bib"):
                    has_registry = True
                    break
    except OSError:
        has_registry = False
    if not has_registry:
        # Still scan immediate `docs/`, `content/`, `prose/` subdirs
        # — a common place for `*.bib` and registry files.
        for sub in ("docs", "content", "prose", "data", "manuscript"):
            d = root / sub
            if d.is_dir():
                try:
                    for entry in os.scandir(d):
                        if entry.is_file():
                            lo = entry.name.lower()
                            if lo in _REGISTRY_FILENAMES or lo.endswith(".bib"):
                                has_registry = True
                                break
                except OSError:
                    continue
            if has_registry:
                break
    if not has_registry:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM name_registry WHERE project_slug = %s",
                        (project_slug,))
        return {"names_added": 0, "by_kind": {}, "by_source": {},
                 "skipped": "no_registry_file_found"}

    with conn.cursor() as cur:
        cur.execute("DELETE FROM name_registry WHERE project_slug = %s",
                    (project_slug,))
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                            if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
            for f in filenames:
                full = Path(dirpath) / f
                if flt.should_skip(full, is_dir=False):
                    continue
                fname_lower = f.lower()
                if fname_lower in _REGISTRY_FILENAMES:
                    src = ("name-registry-yaml"
                           if fname_lower.endswith((".yaml", ".yml"))
                           else "name-registry-json")
                    for name, kind, aliases, desc in parse_registry_file(full):
                        key = (name, kind)
                        if key in seen:
                            continue
                        seen.add(key)
                        cur.execute(
                            """INSERT INTO name_registry
                               (project_slug, name, kind, aliases, description,
                                source, source_file)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (project_slug, name, kind)
                                 DO NOTHING""",
                            (project_slug, name, kind, aliases, desc,
                             src, str(full)),
                        )
                        by_kind[kind] = by_kind.get(kind, 0) + 1
                        by_source[src] = by_source.get(src, 0) + 1
                elif fname_lower.endswith(".bib"):
                    for name, kind, aliases, desc in parse_bibtex(full):
                        key = (name, kind)
                        if key in seen:
                            continue
                        seen.add(key)
                        cur.execute(
                            """INSERT INTO name_registry
                               (project_slug, name, kind, aliases, description,
                                source, source_file)
                               VALUES (%s,%s,%s,%s,%s,'bibtex',%s)
                               ON CONFLICT (project_slug, name, kind)
                                 DO NOTHING""",
                            (project_slug, name, kind, aliases, desc, str(full)),
                        )
                        by_kind[kind] = by_kind.get(kind, 0) + 1
                        by_source["bibtex"] = by_source.get("bibtex", 0) + 1

    return {
        "names_added": sum(by_kind.values()),
        "by_kind": by_kind,
        "by_source": by_source,
    }


def resolve_name(name: str, project_slug: str, conn,
                  *, kind: str | None = None,
                  fuzzy: bool = True, limit: int = 5) -> list[dict]:
    out = []
    with conn.cursor() as cur:
        if kind:
            cur.execute(
                """SELECT id, name, kind, aliases, description, source, source_file
                   FROM name_registry
                   WHERE project_slug = %s AND name = %s AND kind = %s
                   LIMIT %s""",
                (project_slug, name, kind, limit),
            )
        else:
            cur.execute(
                """SELECT id, name, kind, aliases, description, source, source_file
                   FROM name_registry
                   WHERE project_slug = %s AND name = %s
                   LIMIT %s""",
                (project_slug, name, limit),
            )
        for row in cur.fetchall():
            out.append({
                "id": row[0], "name": row[1], "kind": row[2],
                "aliases": row[3], "description": row[4],
                "source": row[5], "source_file": row[6],
                "match_kind": "exact", "trigram_sim": 1.0,
            })
        if out:
            return out
        # Check aliases too
        cur.execute(
            """SELECT id, name, kind, aliases, description, source, source_file
               FROM name_registry
               WHERE project_slug = %s AND %s = ANY(aliases)
               LIMIT %s""",
            (project_slug, name, limit),
        )
        for row in cur.fetchall():
            out.append({
                "id": row[0], "name": row[1], "kind": row[2],
                "aliases": row[3], "description": row[4],
                "source": row[5], "source_file": row[6],
                "match_kind": "alias", "trigram_sim": 1.0,
            })
        if out:
            return out
        if fuzzy:
            cur.execute("SELECT set_limit(%s)", (0.5,))
            cur.execute(
                """SELECT id, name, kind, aliases, description, source,
                          source_file, similarity(name, %s) AS sim
                   FROM name_registry
                   WHERE project_slug = %s AND name %% %s
                   ORDER BY sim DESC LIMIT %s""",
                (name, project_slug, name, limit),
            )
            for row in cur.fetchall():
                out.append({
                    "id": row[0], "name": row[1], "kind": row[2],
                    "aliases": row[3], "description": row[4],
                    "source": row[5], "source_file": row[6],
                    "match_kind": "fuzzy", "trigram_sim": float(row[7] or 0),
                })
    return out


def list_names(project_slug: str, conn, *, kind: str | None = None,
                limit: int = 500) -> list[dict]:
    where = ["project_slug = %s"]
    params: list = [project_slug]
    if kind:
        where.append("kind = %s"); params.append(kind)
    sql = (f"SELECT name, kind, aliases, description, source, source_file "
           f"FROM name_registry WHERE {' AND '.join(where)} "
           f"ORDER BY kind, name LIMIT %s")
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
