#!/usr/bin/env python3
"""
claude-dejavu — quick-win indexers (v0.3.14).

Four small, high-leverage grounding domains in one module:

  CSS / Tailwind class names — `className="rouded-lg"` typo catcher
  i18n keys                  — `t('user.profle.name')` typo catcher
  npm scripts                — `npm run buld` typo catcher (lint-only,
                                no DB; reads package.json on demand)
  Asset paths                — `import logo from './logo.svp'` typo
                                (lint-only; verifies path resolves)

CSS + i18n have indexers (DB-backed) because they have many keys and
fuzzy matching benefits from the trigram index. The other two are
small enough to lint inline.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# ─── Tailwind built-in class set ────────────────────────────────────────────


# A compact representation of Tailwind v3+ utility classes. Not exhaustive —
# we list bases + common modifiers; the lint accepts any class that has a
# valid base prefix + valid scale suffix. For exact matching we also include
# a curated set of common literal classes.
_TAILWIND_BASES = {
    # spacing
    "m", "mx", "my", "ms", "me", "mt", "mr", "mb", "ml",
    "p", "px", "py", "ps", "pe", "pt", "pr", "pb", "pl",
    "space-x", "space-y", "gap", "gap-x", "gap-y",
    # sizing
    "w", "h", "min-w", "min-h", "max-w", "max-h",
    # typography
    "text", "font", "leading", "tracking", "indent", "align",
    # colors & background
    "bg", "border", "ring", "outline", "from", "to", "via", "fill", "stroke",
    # layout
    "flex", "grid", "col-span", "row-span", "col-start", "col-end",
    "row-start", "row-end", "auto-cols", "auto-rows",
    "justify", "items", "self", "place-content", "place-items", "place-self",
    "order",
    # display & positioning
    "block", "inline-block", "inline", "flex", "inline-flex", "grid",
    "inline-grid", "hidden", "absolute", "relative", "fixed", "sticky",
    "static", "inset", "top", "right", "bottom", "left",
    "z",
    # borders & rounded
    "rounded", "border", "border-x", "border-y", "border-t", "border-r",
    "border-b", "border-l", "divide-x", "divide-y", "ring", "ring-offset",
    # effects
    "shadow", "opacity", "blur", "brightness", "contrast", "drop-shadow",
    "grayscale", "hue-rotate", "invert", "saturate", "sepia",
    "backdrop-blur", "backdrop-brightness", "backdrop-contrast",
    # transforms / transitions
    "scale", "scale-x", "scale-y", "rotate", "translate", "translate-x",
    "translate-y", "skew", "skew-x", "skew-y", "transition", "duration",
    "delay", "ease", "animate",
    # interactivity
    "cursor", "select", "appearance", "outline", "pointer-events", "resize",
    "scroll", "snap", "touch", "user-select", "will-change",
    # typography utilities
    "decoration", "underline", "overline", "line-through", "no-underline",
    "uppercase", "lowercase", "capitalize", "normal-case",
    "italic", "not-italic", "tabular-nums", "ordinal", "slashed-zero",
    # tables
    "table", "table-fixed", "table-auto", "border-collapse", "border-separate",
    # sr-only
    "sr-only", "not-sr-only", "truncate",
    # aspect
    "aspect",
    # container
    "container",
    # group/peer
    "group", "peer",
    # overflow
    "overflow", "overflow-x", "overflow-y",
}

_TAILWIND_VARIANTS = {
    # responsive
    "sm", "md", "lg", "xl", "2xl",
    # state
    "hover", "focus", "active", "visited", "disabled", "checked",
    "focus-within", "focus-visible", "first", "last", "odd", "even",
    "empty", "open", "default", "indeterminate", "placeholder-shown",
    # group / peer
    "group-hover", "group-focus", "group-active", "peer-hover", "peer-focus",
    # dark mode
    "dark",
    # motion
    "motion-safe", "motion-reduce",
    # print
    "print",
    # rtl
    "rtl", "ltr",
    # data attrs
    "data-active", "data-open", "data-checked",
    # children
    "children", "first-child", "last-child",
}

# Curated "always valid" exact classes that aren't covered by the prefix
# heuristic. Things like `flex-1`, `flex-auto`, `gap-px`, etc.
_TAILWIND_LITERALS = {
    "flex-1", "flex-auto", "flex-initial", "flex-none",
    "flex-row", "flex-col", "flex-wrap", "flex-nowrap",
    "grid-cols-1", "grid-cols-2", "grid-cols-3", "grid-cols-4",
    "grid-cols-5", "grid-cols-6", "grid-cols-12",
    "items-start", "items-center", "items-end", "items-baseline",
    "items-stretch",
    "justify-start", "justify-center", "justify-end", "justify-between",
    "justify-around", "justify-evenly",
    "place-content-center", "place-items-center",
    "self-auto", "self-start", "self-center", "self-end", "self-stretch",
    "min-w-0", "min-h-0", "min-h-screen", "min-w-screen",
    "h-screen", "w-screen", "h-full", "w-full", "h-auto", "w-auto",
    "w-fit", "h-fit",
    "text-left", "text-center", "text-right", "text-justify",
    "uppercase", "lowercase", "capitalize", "normal-case",
    "leading-none", "leading-tight", "leading-snug", "leading-normal",
    "leading-relaxed", "leading-loose",
    "rounded", "rounded-none", "rounded-sm", "rounded-md", "rounded-lg",
    "rounded-xl", "rounded-2xl", "rounded-3xl", "rounded-full",
    "rounded-t-lg", "rounded-b-lg", "rounded-l-lg", "rounded-r-lg",
    "rounded-tl-lg", "rounded-tr-lg", "rounded-bl-lg", "rounded-br-lg",
    "border", "border-0", "border-2", "border-4", "border-8",
    "shadow", "shadow-sm", "shadow-md", "shadow-lg", "shadow-xl",
    "shadow-2xl", "shadow-inner", "shadow-none",
    "transition", "transition-none", "transition-all", "transition-colors",
    "transition-opacity", "transition-shadow", "transition-transform",
    "block", "inline-block", "inline", "flex", "inline-flex", "grid",
    "inline-grid", "hidden",
    "absolute", "relative", "fixed", "sticky", "static",
    "cursor-pointer", "cursor-default", "cursor-wait", "cursor-text",
    "cursor-move", "cursor-not-allowed",
    "select-none", "select-auto", "select-text", "select-all",
    "container",
    "truncate", "sr-only", "not-sr-only",
    "italic", "not-italic", "underline", "no-underline", "line-through",
    "antialiased", "subpixel-antialiased",
    "overflow-hidden", "overflow-auto", "overflow-visible", "overflow-scroll",
    "overflow-x-auto", "overflow-y-auto",
    "object-cover", "object-contain", "object-fill", "object-none",
    "pointer-events-auto", "pointer-events-none",
    "appearance-none",
    "list-none", "list-disc", "list-decimal",
    "whitespace-normal", "whitespace-nowrap", "whitespace-pre",
    "whitespace-pre-wrap", "whitespace-pre-line",
    "break-normal", "break-words", "break-all",
    "z-0", "z-10", "z-20", "z-30", "z-40", "z-50", "z-auto",
    "opacity-0", "opacity-25", "opacity-50", "opacity-75", "opacity-100",
    "isolate", "isolation-auto",
    "will-change-auto", "will-change-transform", "will-change-scroll",
    "table", "table-row", "table-cell", "table-caption",
    "table-fixed", "table-auto",
    "ring-1", "ring-2", "ring-4", "ring-8", "ring-inset",
    "ring-offset-0", "ring-offset-1", "ring-offset-2", "ring-offset-4",
    "ring-offset-8",
    "outline-none", "outline-dashed", "outline-dotted", "outline-double",
    "ml-auto", "mr-auto", "mx-auto", "my-auto",
}


def _tailwind_scale_classes() -> set:
    """Generate the most common base+scale combinations as exact strings."""
    out = set()
    sizes_num = ["0", "px", "0.5", "1", "1.5", "2", "2.5", "3", "3.5",
                 "4", "5", "6", "7", "8", "9", "10", "11", "12",
                 "14", "16", "20", "24", "28", "32", "36", "40", "44",
                 "48", "52", "56", "60", "64", "72", "80", "96",
                 "auto", "full", "screen", "min", "max", "fit"]
    fractions = ["1/2", "1/3", "2/3", "1/4", "2/4", "3/4",
                 "1/5", "2/5", "3/5", "4/5",
                 "1/6", "2/6", "3/6", "4/6", "5/6",
                 "1/12", "2/12", "11/12"]
    color_names = ["slate", "gray", "zinc", "neutral", "stone", "red",
                   "orange", "amber", "yellow", "lime", "green", "emerald",
                   "teal", "cyan", "sky", "blue", "indigo", "violet",
                   "purple", "fuchsia", "pink", "rose", "white", "black",
                   "transparent", "current", "inherit"]
    color_shades = ["50", "100", "200", "300", "400", "500", "600",
                    "700", "800", "900", "950"]
    text_sizes = ["xs", "sm", "base", "lg", "xl", "2xl", "3xl", "4xl",
                  "5xl", "6xl", "7xl", "8xl", "9xl"]
    fonts = ["sans", "serif", "mono", "thin", "extralight", "light",
             "normal", "medium", "semibold", "bold", "extrabold", "black"]
    rounded = ["none", "sm", "md", "lg", "xl", "2xl", "3xl", "full"]
    shadows = ["sm", "md", "lg", "xl", "2xl", "inner", "none"]

    # spacing utilities: m{x,y,s,e,t,r,b,l}-N, p... + num scale + fractions
    for base in ["m", "mx", "my", "ms", "me", "mt", "mr", "mb", "ml",
                  "p", "px", "py", "ps", "pe", "pt", "pr", "pb", "pl",
                  "gap", "gap-x", "gap-y", "space-x", "space-y",
                  "inset", "top", "right", "bottom", "left",
                  "translate-x", "translate-y", "skew-x", "skew-y",
                  "scale-x", "scale-y", "scale", "rotate"]:
        for s in sizes_num:
            out.add(f"{base}-{s}")
        for s in fractions:
            out.add(f"{base}-{s}")
        for s in ["1", "2", "4"]:
            out.add(f"-{base}-{s}")  # negative spacing
    # sizing: w-, h-, min-w-, etc.
    for base in ["w", "h", "min-w", "min-h", "max-w", "max-h"]:
        for s in sizes_num + fractions:
            out.add(f"{base}-{s}")
    # font sizes
    for s in text_sizes:
        out.add(f"text-{s}")
    for s in fonts:
        out.add(f"font-{s}")
    # text-color, bg-color, border-color, ring-color, fill-color, stroke-color
    for color_base in ["text", "bg", "border", "ring", "fill", "stroke",
                        "from", "to", "via", "outline", "decoration",
                        "divide", "placeholder", "caret", "accent"]:
        for cn in color_names:
            for shade in color_shades:
                out.add(f"{color_base}-{cn}-{shade}")
            out.add(f"{color_base}-{cn}")  # white/black/transparent
    # rounded variants
    out.add("rounded")
    for r in rounded:
        out.add(f"rounded-{r}")
        for side in ["t", "r", "b", "l", "tl", "tr", "bl", "br",
                       "s", "e", "ss", "se", "es", "ee"]:
            out.add(f"rounded-{side}-{r}")
    # shadow variants
    for s in shadows:
        out.add(f"shadow-{s}")
    # opacity / z-index
    for n in ["0", "5", "10", "20", "25", "30", "40", "50", "60",
               "70", "75", "80", "90", "95", "100"]:
        out.add(f"opacity-{n}")
    for n in ["0", "10", "20", "30", "40", "50", "auto", "minus-1"]:
        out.add(f"z-{n}")
    # ring widths
    for n in ["0", "1", "2", "4", "8"]:
        out.add(f"ring-{n}")
    # leading / tracking
    for n in ["3", "4", "5", "6", "7", "8", "9", "10"]:
        out.add(f"leading-{n}")
    for t in ["tighter", "tight", "normal", "wide", "wider", "widest"]:
        out.add(f"tracking-{t}")
    # aspect
    for a in ["square", "video", "auto"]:
        out.add(f"aspect-{a}")
    return out


_TAILWIND_BUILTIN = (_TAILWIND_LITERALS | _tailwind_scale_classes())


def _strip_variants(class_name: str) -> tuple[list[str], str]:
    """Tailwind classes can have variant prefixes: `md:hover:dark:bg-red-500`.
    Returns (variants, base_class)."""
    parts = class_name.split(":")
    base = parts[-1]
    variants = parts[:-1]
    return variants, base


def is_tailwind_class(class_name: str) -> bool:
    """Returns True if class_name looks like a valid Tailwind class.
    Negative classes (`-mt-2`) and arbitrary values (`text-[color:red]`)
    are accepted."""
    if not class_name:
        return False
    variants, base = _strip_variants(class_name)
    # Validate variants — each must be in the known set OR a media-query-like
    # bracket value.
    for v in variants:
        if v in _TAILWIND_VARIANTS:
            continue
        if v.startswith("[") and v.endswith("]"):
            continue
        if v.startswith("@") and "[" in v:  # custom media query
            continue
        return False
    # Strip leading `-` for negative scale (`-mt-2`).
    if base.startswith("-"):
        base = base[1:]
    # Arbitrary-value: `[whatever]` at the end is always accepted.
    if "[" in base and base.endswith("]"):
        return True
    if base in _TAILWIND_BUILTIN:
        return True
    if base in _TAILWIND_LITERALS:
        return True
    # Allow important modifier (`!`)
    if base.startswith("!") and base[1:] in _TAILWIND_BUILTIN:
        return True
    return False


# ─── Tailwind config parser ─────────────────────────────────────────────────


def parse_tailwind_config(path: Path) -> Iterable[str]:
    """Best-effort: extract class names from the user's tailwind config —
    `theme.extend.colors` keys, `theme.extend.spacing` keys, etc.
    Returns class-name strings to add to the project's known set."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    # Look for `extend: { colors: { brand: ..., accent: ... } }` style.
    extend_match = re.search(r"extend\s*:\s*\{", text)
    if not extend_match:
        return
    # Naive: find every quoted key inside the extend block.
    extend_start = extend_match.end()
    depth = 1
    i = extend_start
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    extend_text = text[extend_start:i - 1]
    keys = re.findall(r"['\"]([\w\-]+)['\"]\s*:", extend_text)
    keys += re.findall(r"\b([A-Za-z_][\w-]*)\s*:", extend_text)
    yield from keys


# ─── CSS / SCSS module class extraction ─────────────────────────────────────


def parse_css(path: Path) -> Iterable[str]:
    """Pull every `.class-name {` from a CSS / SCSS / module file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for m in re.finditer(r"\.([A-Za-z_][\w-]*)\s*[\{\,]", text):
        yield m.group(1)


# ─── i18n key extraction ────────────────────────────────────────────────────


def parse_i18n_json(path: Path) -> Iterable[tuple[str, str | None]]:
    """Walk JSON locale files. Yields (key_path, locale)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return
    locale = path.stem  # 'en.json' -> 'en'
    # Support {"en": {...}, "de": {...}} or {direct keys}
    if isinstance(data, dict):
        # Heuristic: if top-level keys all look like locale codes,
        # iterate per-locale.
        if all(re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", k) for k in data.keys()):
            for loc, sub in data.items():
                if isinstance(sub, dict):
                    yield from _walk_json_keys(sub, prefix="", locale=loc)
        else:
            yield from _walk_json_keys(data, prefix="", locale=locale)


def _walk_json_keys(obj, prefix: str, locale: str | None
                    ) -> Iterable[tuple[str, str | None]]:
    if not isinstance(obj, dict):
        return
    for k, v in obj.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            yield from _walk_json_keys(v, prefix=full, locale=locale)
        else:
            yield (full, locale)


def parse_po(path: Path) -> Iterable[tuple[str, str | None]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    locale = path.stem
    for m in re.finditer(r'^msgid\s+"((?:[^"\\]|\\.)*)"', text, re.MULTILINE):
        if m.group(1):  # skip empty msgid (file metadata)
            yield (m.group(1), locale)


def parse_ftl(path: Path) -> Iterable[tuple[str, str | None]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    locale = path.stem
    for m in re.finditer(r"^([a-z][\w-]*)\s*=", text, re.MULTILINE):
        yield (m.group(1), locale)


# ─── Project walks ──────────────────────────────────────────────────────────


def index_project_quickwins(project_dir: str, project_slug: str, conn,
                              *, flt=None, force: bool = False) -> dict:
    root = Path(project_dir).resolve()
    if not root.is_dir():
        return {"error": f"not_a_directory: {root}"}

    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(root)

    # ─── v0.4.5: fingerprint cache ─────────────────────────────────────
    # Quick-wins is the most expensive non-symbol indexer (~200s on
    # a large monorepo). Skip entirely if relevant files (CSS + i18n
    # + tailwind config) haven't changed.
    from file_state import IndexerCache, files_under
    relevant = list(files_under(
        root,
        suffixes={".css", ".scss", ".sass", ".less", ".po", ".ftl"},
        filenames={"tailwind.config.js", "tailwind.config.ts",
                    "tailwind.config.cjs", "tailwind.config.mjs"},
        flt=flt,
    ))
    # i18n JSONs are filtered by path-component; collect them too.
    for dirpath, _, fnames in os.walk(root):
        rel_parts = Path(dirpath).resolve().relative_to(root).parts
        if any(p in rel_parts for p in ("i18n", "locales", "messages",
                                          "lang", "translations")):
            for f in fnames:
                if f.endswith(".json") and not flt.should_skip(
                        Path(dirpath) / f, is_dir=False):
                    relevant.append(Path(dirpath) / f)

    cache = IndexerCache(conn, project_slug, "quickwin")
    if not force and cache.is_fresh(relevant):
        return cache.cached_stats_marked()

    # Tailwind built-ins are project-agnostic; insert once if absent.
    css_classes_count = 0
    i18n_keys_count = 0
    with conn.cursor() as cur:
        # ─── Wipe project-scoped data (full-replace) ──────────────────
        cur.execute("DELETE FROM css_classes WHERE project_slug = %s",
                    (project_slug,))
        cur.execute("DELETE FROM i18n_keys WHERE project_slug = %s",
                    (project_slug,))

        # ─── Tailwind built-ins ───────────────────────────────────────
        for cls in _TAILWIND_BUILTIN:
            cur.execute(
                """INSERT INTO css_classes (project_slug, class_name, source,
                                              source_file, indexed_at)
                   VALUES (%s, %s, 'tailwind-builtin', '', now())
                   ON CONFLICT (project_slug, class_name, source, source_file)
                     DO NOTHING""",
                (project_slug, cls),
            )
            css_classes_count += 1

        # ─── Project files ────────────────────────────────────────────
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                            if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
            for f in filenames:
                full = Path(dirpath) / f
                if flt.should_skip(full, is_dir=False):
                    continue
                ext = full.suffix.lower()
                fname = f.lower()

                # Tailwind config
                if fname.startswith("tailwind.config.") and ext in (".js", ".ts",
                                                                       ".cjs", ".mjs"):
                    for cls in parse_tailwind_config(full):
                        cur.execute(
                            """INSERT INTO css_classes
                               (project_slug, class_name, source, source_file)
                               VALUES (%s,%s,'tailwind-config',%s)
                               ON CONFLICT (project_slug, class_name, source, source_file)
                                 DO NOTHING""",
                            (project_slug, cls, str(full)),
                        )
                        css_classes_count += 1
                # CSS / SCSS / module CSS
                if ext in (".css", ".scss", ".sass", ".less"):
                    src = "css-module" if ".module." in fname else "global-css"
                    for cls in parse_css(full):
                        cur.execute(
                            """INSERT INTO css_classes
                               (project_slug, class_name, source, source_file)
                               VALUES (%s,%s,%s,%s)
                               ON CONFLICT (project_slug, class_name, source, source_file)
                                 DO NOTHING""",
                            (project_slug, cls, src, str(full)),
                        )
                        css_classes_count += 1
                # i18n JSON (under i18n/, locales/, messages/, lang/)
                rel = full.relative_to(root)
                rel_parts = rel.parts
                if (ext == ".json"
                        and any(p in rel_parts for p in
                                 ("i18n", "locales", "messages", "lang", "translations"))):
                    for key, locale in parse_i18n_json(full):
                        cur.execute(
                            """INSERT INTO i18n_keys
                               (project_slug, key, locale, source, source_file)
                               VALUES (%s,%s,%s,'json',%s)
                               ON CONFLICT (project_slug, key, locale, source_file)
                                 DO NOTHING""",
                            (project_slug, key, locale, str(full)),
                        )
                        i18n_keys_count += 1
                # i18n .po
                if ext == ".po":
                    for key, locale in parse_po(full):
                        cur.execute(
                            """INSERT INTO i18n_keys
                               (project_slug, key, locale, source, source_file)
                               VALUES (%s,%s,%s,'po',%s)
                               ON CONFLICT (project_slug, key, locale, source_file)
                                 DO NOTHING""",
                            (project_slug, key, locale, str(full)),
                        )
                        i18n_keys_count += 1
                # i18n .ftl (Fluent)
                if ext == ".ftl":
                    for key, locale in parse_ftl(full):
                        cur.execute(
                            """INSERT INTO i18n_keys
                               (project_slug, key, locale, source, source_file)
                               VALUES (%s,%s,%s,'ftl',%s)
                               ON CONFLICT (project_slug, key, locale, source_file)
                                 DO NOTHING""",
                            (project_slug, key, locale, str(full)),
                        )
                        i18n_keys_count += 1

    stats = {
        "css_classes_added": css_classes_count,
        "i18n_keys_added": i18n_keys_count,
    }
    cache.store(relevant, stats=stats)
    return stats


# ─── Resolution ─────────────────────────────────────────────────────────────


def resolve_css_class(class_name: str, project_slug: str, conn,
                        *, fuzzy: bool = True, limit: int = 5) -> list[dict]:
    out: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, class_name, source, source_file, start_line
               FROM css_classes
               WHERE project_slug = %s AND class_name = %s
               LIMIT %s""",
            (project_slug, class_name, limit),
        )
        for row in cur.fetchall():
            out.append({
                "id": row[0], "class_name": row[1], "source": row[2],
                "source_file": row[3], "start_line": row[4],
                "match_kind": "exact", "trigram_sim": 1.0,
            })
        if out:
            return out
        if fuzzy:
            cur.execute("SELECT set_limit(%s)", (0.5,))
            cur.execute(
                """SELECT id, class_name, source, source_file, start_line,
                          similarity(class_name, %s) AS sim
                   FROM css_classes
                   WHERE project_slug = %s AND class_name %% %s
                   ORDER BY sim DESC LIMIT %s""",
                (class_name, project_slug, class_name, limit),
            )
            for row in cur.fetchall():
                out.append({
                    "id": row[0], "class_name": row[1], "source": row[2],
                    "source_file": row[3], "start_line": row[4],
                    "match_kind": "fuzzy", "trigram_sim": float(row[5] or 0),
                })
    return out


def resolve_i18n_key(key: str, project_slug: str, conn,
                      *, fuzzy: bool = True, limit: int = 5) -> list[dict]:
    out: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, key, locale, source, source_file, start_line
               FROM i18n_keys
               WHERE project_slug = %s AND key = %s
               LIMIT %s""",
            (project_slug, key, limit),
        )
        for row in cur.fetchall():
            out.append({
                "id": row[0], "key": row[1], "locale": row[2],
                "source": row[3], "source_file": row[4],
                "start_line": row[5],
                "match_kind": "exact", "trigram_sim": 1.0,
            })
        if out:
            return out
        if fuzzy:
            cur.execute("SELECT set_limit(%s)", (0.5,))
            cur.execute(
                """SELECT id, key, locale, source, source_file, start_line,
                          similarity(key, %s) AS sim
                   FROM i18n_keys
                   WHERE project_slug = %s AND key %% %s
                   ORDER BY sim DESC LIMIT %s""",
                (key, project_slug, key, limit),
            )
            for row in cur.fetchall():
                out.append({
                    "id": row[0], "key": row[1], "locale": row[2],
                    "source": row[3], "source_file": row[4],
                    "start_line": row[5],
                    "match_kind": "fuzzy", "trigram_sim": float(row[6] or 0),
                })
    return out
