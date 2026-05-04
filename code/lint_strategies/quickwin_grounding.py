"""
Quick-win grounding strategies (v0.3.14):

  css_grounding   — flags `className="rouded-lg"` typos.
  i18n_grounding  — flags `t('user.profle.name')` typos.
  npm_grounding   — flags `npm run buld` typos against package.json scripts.
  asset_grounding — flags `import logo from './logo.svp'` (file doesn't exist).

The CSS + i18n strategies use the DB-backed indexers in
`quickwin_indexer.py`. The npm + asset strategies are lint-only —
they read package.json / fs on-demand.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import LintContext, LintStrategy, Issue, IssueSeverity, register


# ─── className extractor (TS / JS / JSX / TSX) ──────────────────────────────


_CLASSNAME_RE = re.compile(
    r"\bclassName\s*=\s*[\"']((?:[^\"'\\]|\\.)*)[\"']"
)
_CLASSLIST_RE = re.compile(
    r"\bclass\s*=\s*[\"']((?:[^\"'\\]|\\.)*)[\"']"
)


def _extract_classes(text: str):
    """Yield (class_name, line_no) for every className/class attribute."""
    for rx in (_CLASSNAME_RE, _CLASSLIST_RE):
        for m in rx.finditer(text):
            line_no = text[:m.start()].count("\n") + 1
            for cls in m.group(1).split():
                if cls:
                    yield cls, line_no


class CssGroundingStrategy:
    name = "css_grounding"
    languages = {"typescript", "tsx", "javascript", "jsx"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        from quickwin_indexer import resolve_css_class, is_tailwind_class

        # Bail early on empty index.
        with ctx.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM css_classes WHERE project_slug = %s",
                        (ctx.project_slug,))
            if (cur.fetchone()[0] or 0) == 0:
                return []

        seen: set[str] = set()
        issues: list[Issue] = []
        for cls, line_no in _extract_classes(ctx.content):
            if cls in seen:
                continue
            seen.add(cls)
            # Tailwind built-ins: validate using the prefix walker (no DB hit).
            if is_tailwind_class(cls):
                continue
            # Dynamic / template-string class? skip silently.
            if "${" in cls or "{{" in cls:
                continue
            hits = resolve_css_class(cls, ctx.project_slug, ctx.conn,
                                       fuzzy=True, limit=3)
            if not hits:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=f"className:{cls}",
                    candidate=None,
                    confidence=0.85,
                    metadata={"class": cls, "line": line_no},
                    reason=(f"CSS class `{cls}` not declared in any project "
                            f"CSS module, global stylesheet, Tailwind config, "
                            f"or built-in Tailwind utility set"),
                    fix=("verify the class name; if it's intentional, declare "
                         "it in a stylesheet or use an arbitrary-value bracket"),
                ))
                continue
            top = hits[0]
            if top["match_kind"] == "exact":
                continue  # silent ok
            issues.append(Issue(
                strategy=self.name,
                severity=IssueSeverity.LOW_CONFIDENCE,
                name=f"className:{cls}",
                candidate=top["class_name"],
                confidence=float(top.get("trigram_sim", 0)),
                metadata={"class": cls, "line": line_no,
                          "candidate_source": top["source"],
                          "trigram_sim": round(top.get("trigram_sim", 0), 3)},
                reason=(f"close match `{top['class_name']}` "
                        f"({top['source']}, sim={top.get('trigram_sim', 0):.2f})"),
                fix=f"change `{cls}` to `{top['class_name']}`",
            ))
        return issues


# ─── i18n call-site extractor ───────────────────────────────────────────────


_I18N_CALL_RE = re.compile(
    r"\b(?:t|i18n\.t|intl\.formatMessage)\s*\(\s*[\{]?\s*"
    r"(?:id\s*:\s*)?[\"']((?:[^\"'\\]|\\.)*)[\"']"
)


def _extract_i18n_keys(text: str):
    for m in _I18N_CALL_RE.finditer(text):
        line_no = text[:m.start()].count("\n") + 1
        yield m.group(1), line_no


class I18nGroundingStrategy:
    name = "i18n_grounding"
    languages = {"typescript", "tsx", "javascript", "jsx", "python"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        from quickwin_indexer import resolve_i18n_key

        with ctx.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM i18n_keys WHERE project_slug = %s",
                        (ctx.project_slug,))
            if (cur.fetchone()[0] or 0) == 0:
                return []

        seen: set[str] = set()
        issues: list[Issue] = []
        for key, line_no in _extract_i18n_keys(ctx.content):
            if key in seen:
                continue
            seen.add(key)
            hits = resolve_i18n_key(key, ctx.project_slug, ctx.conn,
                                      fuzzy=True, limit=3)
            if not hits:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=f"i18n:{key}",
                    candidate=None,
                    confidence=0.9,
                    metadata={"key": key, "line": line_no},
                    reason=(f"i18n key `{key}` not declared in any locale file"),
                    fix=f"add `{key}` to your locale JSON / .po / .ftl files",
                ))
                continue
            top = hits[0]
            if top["match_kind"] == "exact":
                continue
            issues.append(Issue(
                strategy=self.name,
                severity=IssueSeverity.LOW_CONFIDENCE,
                name=f"i18n:{key}",
                candidate=top["key"],
                confidence=float(top.get("trigram_sim", 0)),
                metadata={"key": key, "candidate": top["key"],
                          "line": line_no},
                reason=(f"close match `{top['key']}` (sim={top.get('trigram_sim', 0):.2f})"),
                fix=f"change `{key}` to `{top['key']}`",
            ))
        return issues


# ─── npm scripts (lint-only, no DB) ─────────────────────────────────────────


_NPM_RUN_RE = re.compile(
    r"\b(?:npm|pnpm|yarn|bun)(?:\s+run)?\s+([\w:.-]+)\b"
)


def _find_package_json(file_path: str) -> Path | None:
    p = Path(file_path).resolve()
    if p.is_file():
        p = p.parent
    for ancestor in [p, *p.parents]:
        cand = ancestor / "package.json"
        if cand.is_file():
            return cand
    return None


class NpmScriptGroundingStrategy:
    name = "npm_script_grounding"
    languages = {"typescript", "tsx", "javascript", "jsx", "bash", "yaml"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        pkg = _find_package_json(ctx.file_path)
        if pkg is None:
            return []
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            return []
        scripts = data.get("scripts") or {}
        if not scripts:
            return []
        # NPM CLI built-ins that aren't in scripts but are valid.
        builtins = {"install", "i", "ci", "uninstall", "remove", "update",
                    "publish", "test", "start", "init", "version", "audit",
                    "fund", "ls", "list", "outdated", "dedupe", "doctor",
                    "exec", "x", "create", "link", "unlink",
                    "add", "fix", "info", "view", "run-script",
                    # pnpm
                    "dlx", "import", "fetch", "rebuild", "patch", "patch-commit",
                    # yarn
                    "berry", "set", "config", "constraints",
                    # frequent run names
                    "build", "dev", "lint", "format", "deploy"}
        issues: list[Issue] = []
        seen: set[str] = set()
        for m in _NPM_RUN_RE.finditer(ctx.content):
            cmd = m.group(1)
            if cmd in seen:
                continue
            seen.add(cmd)
            line_no = ctx.content[:m.start()].count("\n") + 1
            if cmd in builtins or cmd in scripts:
                continue
            # Fuzzy match against scripts.
            close = _closest(cmd, list(scripts.keys()))
            if close and close[1] >= 0.6:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.LOW_CONFIDENCE,
                    name=f"npm-script:{cmd}",
                    candidate=close[0],
                    confidence=close[1],
                    metadata={"cmd": cmd, "line": line_no,
                              "candidate": close[0]},
                    reason=(f"no script named `{cmd}` in {pkg}; "
                            f"close match `{close[0]}` (sim={close[1]:.2f})"),
                    fix=f"change `{cmd}` to `{close[0]}`",
                ))
            else:
                issues.append(Issue(
                    strategy=self.name,
                    severity=IssueSeverity.UNKNOWN,
                    name=f"npm-script:{cmd}",
                    candidate=None,
                    confidence=0.9,
                    metadata={"cmd": cmd, "line": line_no},
                    reason=f"no script `{cmd}` in {pkg}",
                    fix=("add the script to package.json's `scripts` map "
                         "or pick an existing one"),
                ))
        return issues


def _closest(needle: str, candidates: list[str]) -> tuple[str, float] | None:
    """Trigram-style similarity against a small list. Returns
    (best_candidate, similarity 0-1)."""
    def trigrams(s: str) -> set[str]:
        s = f"  {s} "
        return {s[i:i+3] for i in range(len(s) - 2)}
    nt = trigrams(needle.lower())
    if not nt:
        return None
    best = (None, 0.0)
    for c in candidates:
        ct = trigrams(c.lower())
        if not ct:
            continue
        sim = len(nt & ct) / len(nt | ct)
        if sim > best[1]:
            best = (c, sim)
    return best if best[0] else None


# ─── Asset paths (lint-only, fs check) ──────────────────────────────────────


# Match relative imports of asset-like files. We deliberately accept ANY
# 2-6 char extension so typos (`.svp` instead of `.svg`) are caught — the
# fs check then decides if the file actually exists. Skip JS/TS extensions
# (those are module imports, handled by v0.3.16 module_import_grounding).
_NON_ASSET_EXTS = {"ts", "tsx", "js", "jsx", "mjs", "cjs", "json", "py",
                    "go", "rs", "java", "rb", "sh"}
_ASSET_IMPORT_RE = re.compile(
    r"\bimport\s+\w+\s+from\s+[\"']((?:\./|\.\./)[^\"']+\.[A-Za-z0-9]{2,6})[\"']"
)
_REQUIRE_ASSET_RE = re.compile(
    r"\brequire\s*\(\s*[\"']((?:\./|\.\./)[^\"']+\.[A-Za-z0-9]{2,6})[\"']"
)


class AssetPathGroundingStrategy:
    name = "asset_path_grounding"
    languages = {"typescript", "tsx", "javascript", "jsx"}
    enabled_by_default = True

    def applies_to(self, ctx: LintContext) -> bool:
        return ctx.language in self.languages

    def check(self, ctx: LintContext) -> list[Issue]:
        issues: list[Issue] = []
        seen: set[str] = set()
        target_dir = Path(ctx.file_path).parent
        for rx in (_ASSET_IMPORT_RE, _REQUIRE_ASSET_RE):
            for m in rx.finditer(ctx.content):
                rel = m.group(1)
                if rel in seen:
                    continue
                ext = rel.rsplit(".", 1)[-1].lower()
                # Skip JS/TS imports — those are modules, not assets.
                if ext in _NON_ASSET_EXTS:
                    continue
                seen.add(rel)
                line_no = ctx.content[:m.start()].count("\n") + 1
                resolved = (target_dir / rel).resolve()
                if resolved.exists():
                    continue
                # Search nearby for a closely-named file.
                stem = resolved.stem
                ext_target = resolved.suffix
                close = None
                if resolved.parent.is_dir():
                    for sib in resolved.parent.iterdir():
                        if sib.suffix.lower() == ext_target.lower() and \
                                sib.stem.lower() == stem.lower():
                            close = sib
                            break
                    if close is None:
                        # Try fuzzy match on stem within the parent dir.
                        candidates = [s for s in resolved.parent.iterdir()
                                       if s.is_file()]
                        for sib in candidates:
                            if abs(len(sib.stem) - len(stem)) <= 2 and \
                                    _trigram_sim(sib.stem, stem) >= 0.6:
                                close = sib
                                break
                if close:
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.LOW_CONFIDENCE,
                        name=f"asset:{rel}",
                        candidate=str(close.relative_to(target_dir)) if target_dir in close.parents else str(close.name),
                        confidence=0.7,
                        metadata={"path": rel, "line": line_no,
                                  "candidate_path": str(close)},
                        reason=(f"file `{rel}` doesn't exist; close match: "
                                f"`{close.name}`"),
                        fix=f"change `{rel}` to point at `{close.name}`",
                    ))
                else:
                    issues.append(Issue(
                        strategy=self.name,
                        severity=IssueSeverity.UNKNOWN,
                        name=f"asset:{rel}",
                        candidate=None,
                        confidence=0.92,
                        metadata={"path": rel, "line": line_no},
                        reason=(f"file `{rel}` doesn't exist on disk; "
                                f"resolved to `{resolved}`"),
                        fix="verify the asset path or check the file extension",
                    ))
        return issues


def _trigram_sim(a: str, b: str) -> float:
    def trigrams(s: str) -> set[str]:
        s = f"  {s.lower()} "
        return {s[i:i+3] for i in range(len(s) - 2)}
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


register(CssGroundingStrategy())
register(I18nGroundingStrategy())
register(NpmScriptGroundingStrategy())
register(AssetPathGroundingStrategy())
