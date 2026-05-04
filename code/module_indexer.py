#!/usr/bin/env python3
"""
claude-dejavu — npm module export indexer (v0.3.16).

Catches `import { fooBar } from 'lodash'` where `lodash` doesn't
actually export `fooBar`. Indexes the surface of every direct
dependency the project declares in package.json.

Sources of truth (in priority order):

  1. `<pkg>/package.json` — `exports`, `main`, `module`, `types`
     fields tell us where the type definitions live.
  2. `<pkg>/<types>.d.ts` (or main.js if no types) — parsed for
     `export const|function|class|type|interface|...`
  3. `<pkg>/index.d.ts` — fallback when `types` field is missing.

What we DON'T do (out of scope for v0.3.16):

  - Resolve nested barrel re-exports across deep paths
    (`export * from './sub'` chains). We capture the top level.
  - Type-check the imported names (we just verify presence).
  - Handle CommonJS-only modules with no .d.ts. Those are skipped.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_EXPORT_RE = re.compile(
    r"\bexport\s+"
    r"(?:declare\s+)?"
    r"(?:default\s+)?"
    r"(?:async\s+)?"
    r"(?P<kind>function|const|class|type|interface|enum|let|var|namespace|module|abstract\s+class)"
    r"\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)"
)
_EXPORT_NAMED_RE = re.compile(
    r"\bexport\s*\{\s*(?P<list>[^}]+)\s*\}"
)
_EXPORT_DEFAULT_RE = re.compile(r"\bexport\s+default\b")


def _scan_dts(path: Path) -> Iterable[tuple[str, str]]:
    """Yield (export_name, kind) for everything `path` exports."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for m in _EXPORT_RE.finditer(text):
        kind = m.group("kind").replace("abstract ", "").strip()
        kind_norm = {"const": "const", "let": "const", "var": "const",
                      "function": "function", "class": "class",
                      "type": "type", "interface": "interface",
                      "enum": "enum", "namespace": "namespace",
                      "module": "namespace"}.get(kind, kind)
        yield (m.group("name"), kind_norm)
    for m in _EXPORT_NAMED_RE.finditer(text):
        for spec in m.group("list").split(","):
            # `Foo` or `Foo as Bar` or `Foo as default`
            parts = spec.strip().split(" as ")
            if not parts[0].strip():
                continue
            export_name = parts[1].strip() if len(parts) > 1 else parts[0].strip()
            export_name = export_name.strip("\"'`")
            if export_name and re.match(r"[A-Za-z_$][\w$]*$", export_name):
                yield (export_name, "named")
    if _EXPORT_DEFAULT_RE.search(text):
        yield ("default", "default")


def _resolve_types_path(pkg_dir: Path, pkg_data: dict) -> Path | None:
    """Find the .d.ts entry point for a package."""
    for key in ("types", "typings"):
        v = pkg_data.get(key)
        if v:
            cand = (pkg_dir / v).resolve()
            if cand.is_file():
                return cand
    # exports field can have a `.types` or `import.types` entry
    exports = pkg_data.get("exports")
    if isinstance(exports, dict):
        root = exports.get(".") if "." in exports else exports
        if isinstance(root, dict):
            for k in ("types", "import", "default"):
                v = root.get(k)
                if isinstance(v, dict):
                    v = v.get("types") or v.get("default")
                if isinstance(v, str) and v.endswith(".d.ts"):
                    cand = (pkg_dir / v).resolve()
                    if cand.is_file():
                        return cand
    # Fallback: index.d.ts at root
    cand = pkg_dir / "index.d.ts"
    if cand.is_file():
        return cand
    return None


def index_project_modules(project_dir: str, project_slug: str, conn) -> dict:
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"error": f"not_a_directory: {root}"}

    pkg_json = root / "package.json"
    if not pkg_json.is_file():
        return {"packages_scanned": 0, "exports_added": 0,
                "note": "no package.json"}

    try:
        manifest = json.loads(pkg_json.read_text(encoding="utf-8",
                                                   errors="replace"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"package.json parse failed: {e}"}

    deps = {}
    deps.update(manifest.get("dependencies") or {})
    deps.update(manifest.get("devDependencies") or {})
    deps.update(manifest.get("peerDependencies") or {})

    nm = root / "node_modules"
    packages_scanned = 0
    exports_added = 0
    seen: set[tuple[str, str]] = set()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM module_exports WHERE project_slug = %s",
                    (project_slug,))
        for pkg_name in deps.keys():
            pkg_dir = nm / pkg_name
            pkg_meta = pkg_dir / "package.json"
            if not pkg_meta.is_file():
                continue
            try:
                pkg_data = json.loads(pkg_meta.read_text(encoding="utf-8",
                                                          errors="replace"))
            except (json.JSONDecodeError, OSError):
                continue
            packages_scanned += 1
            types_path = _resolve_types_path(pkg_dir, pkg_data)
            if types_path is None:
                # Mark default-only export so `import x from 'pkg'` works.
                cur.execute(
                    """INSERT INTO module_exports
                       (project_slug, package_name, export_name, kind,
                        source_file)
                       VALUES (%s,%s,'default','default',NULL)
                       ON CONFLICT (project_slug, package_name, export_name)
                         DO NOTHING""",
                    (project_slug, pkg_name),
                )
                exports_added += 1
                continue
            for name, kind in _scan_dts(types_path):
                key = (pkg_name, name)
                if key in seen:
                    continue
                seen.add(key)
                cur.execute(
                    """INSERT INTO module_exports
                       (project_slug, package_name, export_name, kind,
                        source_file)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (project_slug, package_name, export_name)
                         DO NOTHING""",
                    (project_slug, pkg_name, name, kind, str(types_path)),
                )
                exports_added += 1
    return {
        "packages_scanned": packages_scanned,
        "exports_added": exports_added,
    }


def resolve_module_export(package: str, export_name: str,
                            project_slug: str, conn,
                            *, fuzzy: bool = True, limit: int = 5
                            ) -> list[dict]:
    out = []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, package_name, export_name, kind, source_file
               FROM module_exports
               WHERE project_slug = %s AND package_name = %s
                 AND export_name = %s
               LIMIT %s""",
            (project_slug, package, export_name, limit),
        )
        for row in cur.fetchall():
            out.append({
                "id": row[0], "package_name": row[1],
                "export_name": row[2], "kind": row[3],
                "source_file": row[4],
                "match_kind": "exact", "trigram_sim": 1.0,
            })
        if out:
            return out
        if fuzzy:
            cur.execute("SELECT set_limit(%s)", (0.5,))
            cur.execute(
                """SELECT id, package_name, export_name, kind, source_file,
                          similarity(export_name, %s) AS sim
                   FROM module_exports
                   WHERE project_slug = %s AND package_name = %s
                     AND export_name %% %s
                   ORDER BY sim DESC LIMIT %s""",
                (export_name, project_slug, package, export_name, limit),
            )
            for row in cur.fetchall():
                out.append({
                    "id": row[0], "package_name": row[1],
                    "export_name": row[2], "kind": row[3],
                    "source_file": row[4],
                    "match_kind": "fuzzy",
                    "trigram_sim": float(row[5] or 0),
                })
    return out
