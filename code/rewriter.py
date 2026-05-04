"""Deterministic-first name rewriter.

Pipeline:
  1. Scorer locates names that don't exact-match the project manifest.
  2. For each bad name, run a domain-specific strategy stack. Each
     strategy votes (verdict, confidence, replacement). Highest
     confidence above the strategy's threshold wins.
  3. Apply REPLACE outcomes by string-substitution in the response.
  4. ACCEPT_SDK outcomes are logged but not rewritten — they're
     legitimate third-party defaults (gated by package.json).
  5. RESIDUAL outcomes are returned for the caller to optionally hand
     off to a focused, batched LLM clarification turn.

Each strategy is a small pure function — easy to add/remove/tune. The
domain stacks (see STRATEGY_STACKS) decide which strategies apply
where, since a typo-detection regime that works for env vars is
different from one for routes or GraphQL fields.

The rewriter never calls an LLM. The optional residual pass is the
caller's responsibility.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

Verdict = Literal["OK", "REPLACE", "ACCEPT_SDK", "RESIDUAL"]


@dataclass
class StrategyResult:
    verdict: Verdict
    confidence: float
    replacement: Optional[str] = None
    reason: str = ""
    strategy: str = ""


@dataclass
class RewriteOutcome:
    domain: str
    original: str
    final_verdict: Verdict
    chosen: Optional[StrategyResult] = None
    all_results: list[StrategyResult] = field(default_factory=list)


# ─── Strategy primitives ────────────────────────────────────────────────────


def _trigrams(s: str) -> set[str]:
    s = f"  {s.lower()} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _trigram_sim(a: str, b: str) -> float:
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _normalize_separators(s: str) -> str:
    """Collapse to a canonical lowercase token sequence.
    `databaseUrl` → `database_url` → `database url`.
    `DATABASE_URL` → `database url`.
    `database-url` → `database url`."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)  # camel → snake
    s = s.lower().replace("-", "_")
    s = s.replace("_", " ").strip()
    return re.sub(r"\s+", " ", s)


def _token_set(s: str) -> set[str]:
    return set(_normalize_separators(s).split())


# ─── Strategies ─────────────────────────────────────────────────────────────


def strategy_exact(name: str, candidates: list[str], **_) -> StrategyResult:
    if name in candidates:
        return StrategyResult("OK", 1.0, name, "exact match", "exact")
    return StrategyResult("RESIDUAL", 0.0, None, "no exact match", "exact")


def strategy_sdk_catalog(name: str, *, project_deps: set[str],
                            sdk_catalog: dict, **_) -> StrategyResult:
    """ACCEPT_SDK if the name appears in the env list of any package
    that's actually a dependency of the project."""
    env_table = sdk_catalog.get("env", {})
    for pkg, vars_list in env_table.items():
        # Accept "_universal" entries unconditionally (NODE_ENV, etc.)
        if pkg == "_universal" or pkg in project_deps:
            if name in vars_list:
                return StrategyResult(
                    "ACCEPT_SDK", 1.0, name,
                    f"standard env var for `{pkg}` (declared in deps)",
                    "sdk_catalog",
                )
    return StrategyResult("RESIDUAL", 0.0, None,
                           "not in any SDK catalog matching deps",
                           "sdk_catalog")


def strategy_trigram(name: str, candidates: list[str], *,
                       threshold: float = 0.7, **_) -> StrategyResult:
    if not candidates:
        return StrategyResult("RESIDUAL", 0.0, None, "no candidates", "trigram")
    best = max(((c, _trigram_sim(name, c)) for c in candidates),
                 key=lambda x: x[1])
    if best[1] >= threshold:
        return StrategyResult("REPLACE", best[1], best[0],
                                 f"trigram sim {best[1]:.2f} ≥ {threshold}",
                                 "trigram")
    return StrategyResult("RESIDUAL", best[1], best[0],
                           f"trigram sim {best[1]:.2f} below {threshold}",
                           "trigram")


def strategy_acronym(name: str, candidates: list[str], *,
                       abbreviations: dict, **_) -> StrategyResult:
    """Expand abbreviations and re-check. `LD_SDK_KEY` → `LAUNCHDARKLY_SDK_KEY`.
    Returns REPLACE if the expansion exact-matches a candidate."""
    parts = name.split("_")
    expanded_variants: list[list[str]] = [parts[:]]  # also try original
    for i, part in enumerate(parts):
        if part in abbreviations:
            new_variants = []
            for v in expanded_variants:
                copy = v[:]
                copy[i] = abbreviations[part]
                new_variants.append(copy)
            expanded_variants.extend(new_variants)
    for variant in expanded_variants:
        candidate = "_".join(variant)
        if candidate != name and candidate in candidates:
            return StrategyResult("REPLACE", 0.95, candidate,
                                     f"acronym expansion: "
                                     f"{name} → {candidate}",
                                     "acronym")
    return StrategyResult("RESIDUAL", 0.0, None,
                           "no acronym expansion matched",
                           "acronym")


def strategy_case_normalize(name: str, candidates: list[str], **_
                                 ) -> StrategyResult:
    """Match ignoring case + separator style."""
    norm_name = _normalize_separators(name)
    for c in candidates:
        if _normalize_separators(c) == norm_name and c != name:
            return StrategyResult("REPLACE", 0.95, c,
                                     f"case/separator match: "
                                     f"{name} → {c}",
                                     "case_normalize")
    return StrategyResult("RESIDUAL", 0.0, None,
                           "no case-normalized match",
                           "case_normalize")


def strategy_token_set(name: str, candidates: list[str], **_
                         ) -> StrategyResult:
    """Same token bag in any order. `USER_API_TOKEN` ↔ `API_USER_TOKEN`."""
    name_set = _token_set(name)
    if not name_set:
        return StrategyResult("RESIDUAL", 0.0, None, "empty token set",
                                 "token_set")
    for c in candidates:
        if _token_set(c) == name_set and c != name:
            return StrategyResult("REPLACE", 0.9, c,
                                     f"token-set equality: "
                                     f"{name} → {c}",
                                     "token_set")
    return StrategyResult("RESIDUAL", 0.0, None,
                           "no token-set match", "token_set")


def strategy_levenshtein_word(name: str, candidates: list[str], *,
                                  threshold: int = 1, **_
                                  ) -> StrategyResult:
    """Fix a single inserted/deleted/substituted word.
    `STRIPE_KEY` → `STRIPE_SECRET_KEY` (one inserted)."""
    name_tokens = name.split("_")
    for c in candidates:
        c_tokens = c.split("_")
        if abs(len(name_tokens) - len(c_tokens)) > threshold:
            continue
        # Compute token-level edit distance (small inputs, brute-force OK)
        d = _token_edit_distance(name_tokens, c_tokens)
        if 0 < d <= threshold:
            return StrategyResult("REPLACE", 0.85, c,
                                     f"token edit distance {d} ≤ "
                                     f"{threshold}: {name} → {c}",
                                     "levenshtein_word")
    return StrategyResult("RESIDUAL", 0.0, None,
                           "no near-token match", "levenshtein_word")


def _token_edit_distance(a: list[str], b: list[str]) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        dp[i][0] = i
    for j in range(len(b) + 1):
        dp[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1,
                            dp[i][j - 1] + 1,
                            dp[i - 1][j - 1] + cost)
    return dp[-1][-1]


def strategy_suffix_pattern(name: str, candidates: list[str], **_
                                 ) -> StrategyResult:
    """Same prefix + same purpose suffix. `STRIPE_PUBLIC_KEY` near
    `STRIPE_SECRET_KEY` — flag as paired-purpose candidate, not
    auto-replace (likely the model needs a second key the user
    hasn't declared yet)."""
    suffixes = ("_KEY", "_SECRET", "_TOKEN", "_URL", "_HOST")
    for sfx in suffixes:
        if not name.endswith(sfx):
            continue
        prefix = name[:-len(sfx)]
        for c in candidates:
            for c_sfx in suffixes:
                if c.endswith(c_sfx) and c[:-len(c_sfx)] == prefix:
                    return StrategyResult(
                        "RESIDUAL", 0.4, c,
                        f"paired-purpose candidate: same prefix "
                        f"`{prefix}`, different suffix `{sfx}` vs "
                        f"`{c_sfx}` — likely needs declaration",
                        "suffix_pattern")
    return StrategyResult("RESIDUAL", 0.0, None,
                           "no suffix-pattern relative", "suffix_pattern")


def strategy_cross_domain(name: str, *, manifest: dict,
                              current_domain: str, **_) -> StrategyResult:
    """Detect domain-misclassification: a name typed by the model
    as one kind that actually exists as another kind in the project.
    E.g. model writes `process.env.checkout-flow-v2` but it's a flag
    declared in flags.json. Returns RESIDUAL with a hint — the
    rewriter doesn't auto-rewrite cross-domain confusions because
    fixing one requires changing the *call site syntax*, not the
    name."""
    other_domains = {
        "env": [("env_vars", "env var")],
        "flag": [("flags", "feature flag")],
        "db": [("models", "DB model"),
                ("user_fields", "User field"),
                ("post_fields", "Post field")],
        "graphql": [("gql_types", "GraphQL type")],
        "route": [("routes", "HTTP route")],
    }
    for dom, fields in other_domains.items():
        if dom == current_domain:
            continue
        for field, kind_label in fields:
            if name in manifest.get(field, []):
                return StrategyResult(
                    "RESIDUAL", 0.6, name,
                    f"`{name}` is declared as a {kind_label} but "
                    f"used here as a {current_domain} reference — "
                    f"likely cross-domain confusion",
                    "cross_domain")
    return StrategyResult("RESIDUAL", 0.0, None,
                           "no cross-domain match", "cross_domain")


_COMMENT_TODO_RE = re.compile(
    r"(?:#|//|/\*|;)\s*.*\b(?:TODO|FIXME|XXX|HACK|NOTE)\b",
    re.IGNORECASE)


def strategy_comment_context(name: str, *, source_text: str, **_
                                  ) -> StrategyResult:
    """If the name appears inside or adjacent to a TODO/FIXME comment,
    treat it as intentional scaffolding rather than a fabrication.
    The model is acknowledging the gap, not inventing the name."""
    if not source_text:
        return StrategyResult("RESIDUAL", 0.0, None,
                                 "no source text", "comment_context")
    lines = source_text.split("\n")
    name_lines = [i for i, ln in enumerate(lines) if name in ln]
    for i in name_lines:
        # Check ±2 lines for a TODO/FIXME comment that mentions name.
        lo = max(0, i - 2)
        hi = min(len(lines), i + 3)
        for j in range(lo, hi):
            ln = lines[j]
            if _COMMENT_TODO_RE.search(ln) and name in ln:
                return StrategyResult(
                    "ACCEPT_SDK", 0.85, name,
                    "intentional scaffolding (TODO/FIXME comment "
                    "mentions the name)",
                    "comment_context")
        # Or — name itself is inside a comment line.
        ln = lines[i]
        for marker in ("#", "//", "/*", ";"):
            if marker in ln and ln.find(marker) < ln.find(name):
                return StrategyResult(
                    "ACCEPT_SDK", 0.7, name,
                    "name appears inside a comment, not in code",
                    "comment_context")
    return StrategyResult("RESIDUAL", 0.0, None,
                           "no comment context", "comment_context")


def strategy_project_history(name: str, candidates: list[str], *,
                                  history_names: Optional[set[str]] = None,
                                  **_) -> StrategyResult:
    """If the name used to be declared in this project (per git log
    of the env/schema/flags files) but isn't now, that's a regression
    or recently-removed identifier — flag it as RESIDUAL with the
    historical hint, so the user knows whether to re-add it."""
    if not history_names or name not in history_names:
        return StrategyResult("RESIDUAL", 0.0, None,
                                 "no historical record", "project_history")
    if name in candidates:
        # Already in current manifest — exact_match would have hit.
        return StrategyResult("RESIDUAL", 0.0, None,
                                 "already in current manifest",
                                 "project_history")
    return StrategyResult(
        "RESIDUAL", 0.5, name,
        f"`{name}` was declared in this project's history but has "
        f"been removed — likely a regression, ask user before "
        f"re-introducing",
        "project_history")


def strategy_embedding(name: str, candidates: list[str], *,
                            project_slug: Optional[str] = None,
                            embedding_threshold: float = 0.85,
                            **_) -> StrategyResult:
    """Strategy #8: semantic similarity via the existing dejavu
    Weaviate index. Catches naming-convention drift that fuzzy
    misses (`STRIPE_KEY` → `STRIPE_SECRET_KEY` semantically; or
    `getDate` → `fetchDate` for symbols).

    Gracefully no-ops when Weaviate isn't reachable — semantic is
    strictly additive on top of the lexical strategies. Stderr is
    suppressed around the probe so a missing Weaviate doesn't pollute
    the rewriter's output."""
    import contextlib
    import os as _os
    try:
        from semantic_search import search, is_available  # noqa
    except ImportError:
        return StrategyResult("RESIDUAL", 0.0, None,
                                 "semantic_search unavailable",
                                 "embedding")
    try:
        # Probe is the noisy path — silence its stderr.
        with open(_os.devnull, "w") as _devnull, \
                  contextlib.redirect_stderr(_devnull):
            available = is_available()
        if not available:
            return StrategyResult("RESIDUAL", 0.0, None,
                                     "Weaviate offline",
                                     "embedding")
    except Exception:  # noqa: BLE001
        return StrategyResult("RESIDUAL", 0.0, None,
                                 "Weaviate probe failed",
                                 "embedding")

    if not project_slug:
        return StrategyResult("RESIDUAL", 0.0, None,
                                 "no project_slug for semantic search",
                                 "embedding")

    try:
        with open(_os.devnull, "w") as _devnull, \
                  contextlib.redirect_stderr(_devnull):
            hits = search(intent=name, project_slug=project_slug,
                            limit=3)
    except Exception:  # noqa: BLE001
        return StrategyResult("RESIDUAL", 0.0, None,
                                 "semantic_search query failed",
                                 "embedding")

    for h in hits:
        target = h.get("name") or ""
        # Weaviate returns distance (lower is closer); convert to a
        # confidence in [0, 1] where 1.0 is identical.
        dist = h.get("_additional", {}).get("distance", 1.0) \
                  if isinstance(h.get("_additional"), dict) else 1.0
        confidence = max(0.0, 1.0 - dist)
        if (target and target != name and target in candidates
                and confidence >= embedding_threshold):
            return StrategyResult(
                "REPLACE", confidence, target,
                f"semantic neighbor (cosine ~ {confidence:.2f})",
                "embedding")
    return StrategyResult("RESIDUAL", 0.0, None,
                           "no semantic neighbor above threshold",
                           "embedding")


def strategy_import_gating(name: str, *, source_text: str,
                              project_deps: set[str], sdk_catalog: dict, **_
                              ) -> StrategyResult:
    """Boost SDK-catalog confidence if the source text actually
    imports the SDK whose default vars include `name`."""
    env_table = sdk_catalog.get("env", {})
    for pkg, vars_list in env_table.items():
        if name not in vars_list:
            continue
        # Look for `from "<pkg>"` or `require("<pkg>")` in source
        if (re.search(rf'["\']{{1}}{re.escape(pkg)}["\']{{1}}', source_text)
                or pkg in project_deps):
            return StrategyResult(
                "ACCEPT_SDK", 1.0, name,
                f"`{pkg}` imported in source AND `{name}` is "
                f"its standard env var",
                "import_gating")
    return StrategyResult("RESIDUAL", 0.0, None,
                           "no matching import found",
                           "import_gating")


# ─── Per-domain strategy stacks (per-strategy thresholds) ───────────────────


def _strategy(fn: Callable, **kwargs):
    """Wrap a strategy with bound kwargs for the cascade."""
    def runner(*args, **call_kwargs):
        merged = {**kwargs, **call_kwargs}
        return fn(*args, **merged)
    runner.__name__ = fn.__name__
    return runner


STRATEGY_STACKS: dict[str, list[Callable]] = {
    "env": [
        strategy_exact,
        strategy_comment_context,             # intentional scaffolding
        strategy_import_gating,               # strongest SDK signal
        strategy_sdk_catalog,                  # SDK convention without import
        strategy_cross_domain,                 # flag-as-env, etc.
        strategy_acronym,                      # LD → LAUNCHDARKLY
        strategy_case_normalize,
        _strategy(strategy_levenshtein_word, threshold=1),
        _strategy(strategy_trigram, threshold=0.7),
        strategy_token_set,
        _strategy(strategy_embedding, embedding_threshold=0.85),
        strategy_project_history,              # regressions
        strategy_suffix_pattern,               # last (RESIDUAL only)
    ],
    "db": [
        strategy_exact,
        strategy_comment_context,
        strategy_cross_domain,
        _strategy(strategy_trigram, threshold=0.75),
        strategy_token_set,
        strategy_case_normalize,
        _strategy(strategy_embedding, embedding_threshold=0.82),
        strategy_project_history,
    ],
    "route": [
        strategy_exact,
        strategy_comment_context,
        _strategy(strategy_trigram, threshold=0.7),
        _strategy(strategy_embedding, embedding_threshold=0.82),
        strategy_project_history,
    ],
    "graphql": [
        strategy_exact,
        strategy_comment_context,
        strategy_cross_domain,
        _strategy(strategy_trigram, threshold=0.75),
        strategy_token_set,
        strategy_case_normalize,
        _strategy(strategy_embedding, embedding_threshold=0.82),
        strategy_project_history,
    ],
    "flag": [
        strategy_exact,
        strategy_comment_context,
        strategy_cross_domain,
        _strategy(strategy_trigram, threshold=0.7),
        strategy_case_normalize,
        _strategy(strategy_embedding, embedding_threshold=0.85),
        strategy_project_history,
    ],
}


# ─── Cascade orchestrator ───────────────────────────────────────────────────


# Verdict ranking — higher = stronger.
_VERDICT_PRIORITY = {"OK": 4, "ACCEPT_SDK": 3, "REPLACE": 2,
                       "RESIDUAL": 1}


def cascade(name: str, domain: str, candidates: list[str], *,
              project_deps: set[str], sdk_catalog: dict,
              source_text: str = "",
              manifest: Optional[dict] = None,
              history_names: Optional[set[str]] = None
              ) -> RewriteOutcome:
    """Run every strategy for the domain. Pick the highest-priority
    verdict, breaking ties by confidence."""
    stack = STRATEGY_STACKS.get(domain, [strategy_exact])
    results: list[StrategyResult] = []
    common_kwargs = {
        "project_deps": project_deps,
        "sdk_catalog": sdk_catalog,
        "source_text": source_text,
        "abbreviations": sdk_catalog.get("abbreviations", {}),
        "manifest": manifest or {},
        "current_domain": domain,
        "history_names": history_names or set(),
        "project_slug": (manifest or {}).get("project_slug"),
    }
    for fn in stack:
        try:
            r = fn(name, candidates, **common_kwargs)
        except TypeError:
            # Strategy doesn't take candidates — call without it.
            r = fn(name, **common_kwargs)
        results.append(r)

    chosen = max(
        results,
        key=lambda r: (_VERDICT_PRIORITY[r.verdict], r.confidence),
    )
    return RewriteOutcome(domain=domain, original=name,
                            final_verdict=chosen.verdict,
                            chosen=chosen, all_results=results)


# ─── Manifest helpers ───────────────────────────────────────────────────────


def manifest_candidates(manifest: dict, domain: str,
                          parent: Optional[str] = None) -> list[str]:
    if domain == "env":
        return list(manifest.get("env_vars", []))
    if domain == "db":
        if parent == "User":
            return list(manifest.get("user_fields", []))
        if parent == "Post":
            return list(manifest.get("post_fields", []))
        return (list(manifest.get("user_fields", []))
                + list(manifest.get("post_fields", [])))
    if domain == "route":
        return list(manifest.get("routes", []))
    if domain == "graphql":
        return list(manifest.get("gql_types", []))
    if domain == "flag":
        return list(manifest.get("flags", []))
    return []


# ─── Public entry point ─────────────────────────────────────────────────────


def load_sdk_catalog(path: Optional[Path] = None) -> dict:
    if path is None:
        path = Path(__file__).parent / "sdk_env_catalog.json"
    return json.loads(path.read_text())


def rewrite(text: str, manifest: dict, *, project_deps: set[str],
              bad_names: dict[str, list[str]],
              sdk_catalog: Optional[dict] = None,
              history_names: Optional[set[str]] = None,
              fabrication_conn: Optional[Any] = None
              ) -> tuple[str, list[RewriteOutcome]]:
    """Apply deterministic rewrites to `text`. Returns the rewritten
    text plus per-name analytics. Caller is responsible for re-scoring
    and (optionally) routing RESIDUAL outcomes through an LLM.

    If `fabrication_conn` is provided (psycopg2 connection), every
    REPLACE / RESIDUAL outcome is recorded in the project's
    fabrication_history table for later triage and short-circuit
    optimization. Names already marked 'blacklisted' for this project
    skip the cascade entirely and emit RESIDUAL with that reason."""
    if sdk_catalog is None:
        sdk_catalog = load_sdk_catalog()

    project_slug = (manifest or {}).get("project_slug")
    fab_mod = None
    if fabrication_conn is not None and project_slug:
        try:
            from fabrication_memory import (record_fabrication,
                                                is_blacklisted)
            fab_mod = (record_fabrication, is_blacklisted)
        except ImportError:
            fab_mod = None

    outcomes: list[RewriteOutcome] = []
    rewritten = text

    for domain, names in bad_names.items():
        for name in names:
            # Short-circuit: blacklisted names never run the cascade.
            if fab_mod:
                _record, _is_bl = fab_mod
                if _is_bl(fabrication_conn, project_slug, name, domain):
                    outcomes.append(RewriteOutcome(
                        domain=domain, original=name,
                        final_verdict="RESIDUAL",
                        chosen=StrategyResult(
                            "RESIDUAL", 1.0, None,
                            "blacklisted by user via "
                            "fabrication_history", "blacklist"),
                        all_results=[]))
                    continue
            candidates = manifest_candidates(manifest, domain)
            outcome = cascade(name, domain, candidates,
                                project_deps=project_deps,
                                sdk_catalog=sdk_catalog,
                                source_text=text,
                                manifest=manifest,
                                history_names=history_names)
            outcomes.append(outcome)
            if outcome.final_verdict == "REPLACE" and outcome.chosen \
                    and outcome.chosen.replacement \
                    and outcome.chosen.replacement != name:
                # Word-boundary string replace — avoid replacing
                # substrings inside other identifiers.
                pat = re.compile(rf"\b{re.escape(name)}\b")
                rewritten = pat.sub(outcome.chosen.replacement,
                                       rewritten)
            # Memory: record any non-OK / non-ACCEPT_SDK outcome so
            # we surface repeat fabrications to the user.
            if fab_mod and outcome.final_verdict in ("RESIDUAL",
                                                          "REPLACE"):
                _record, _is_bl = fab_mod
                _record(fabrication_conn, project_slug, name, domain,
                          verdict=outcome.final_verdict)
    return rewritten, outcomes


# ─── Manifest extension helpers (Strategy #11 + #14 plumbing) ───────────────


_ENV_REF_RE = re.compile(
    r"(?:process\.env\.|os\.environ\[['\"]?|os\.getenv\(['\"])"
    r"([A-Z_][A-Z0-9_]*)['\"]?\)?")
_YAML_ENV_KEY_RE = re.compile(
    r"^\s*(?:-\s+name:\s*|env:\s*$|^\s+)([A-Z_][A-Z0-9_]*)\s*[:=]",
    re.MULTILINE)
_DOCKERFILE_ENV_RE = re.compile(
    r"^\s*(?:ENV|ARG)\s+([A-Z_][A-Z0-9_]*)", re.MULTILINE)


def extend_manifest_from_sources(manifest: dict, project_root: "Path"  # noqa: F821
                                     ) -> dict:
    """Strategy #11: walk extra env-declaring source files in the
    project and add discovered names to manifest['env_vars'] (and a
    new 'env_var_sources' map for analytics).

    Sources scanned:
      - .github/workflows/*.yml, *.yaml
      - docker-compose.yml, docker-compose.*.yml
      - Dockerfile, Dockerfile.*
      - next.config.js, next.config.mjs, next.config.ts
      - vercel.json
      - AST scan of *.py, *.ts, *.tsx, *.js for process.env.X /
        os.environ['X'] / os.getenv('X')

    The synthetic bench fixture has none of these, so this is a no-op
    in the bench. In production it dramatically reduces RESIDUAL
    rates for projects that declare envs in CI/Compose but not in
    .env.example."""
    from pathlib import Path
    root = Path(project_root)
    found: dict[str, list[str]] = {}  # name → [source files]

    def _add(name: str, source: str) -> None:
        found.setdefault(name, []).append(source)

    # GitHub workflows
    for f in (root / ".github" / "workflows").glob("*.y*ml") \
            if (root / ".github" / "workflows").is_dir() else []:
        try:
            txt = f.read_text(errors="ignore")
        except OSError:
            continue
        for m in _YAML_ENV_KEY_RE.finditer(txt):
            _add(m.group(1), str(f.relative_to(root)))
        for m in _ENV_REF_RE.finditer(txt):
            _add(m.group(1), str(f.relative_to(root)))

    # docker-compose / Dockerfile
    for pat in ("docker-compose.yml", "docker-compose.*.yml",
                  "compose.yml", "compose.*.yml",
                  "Dockerfile", "Dockerfile.*"):
        for f in root.glob(pat):
            try:
                txt = f.read_text(errors="ignore")
            except OSError:
                continue
            for m in _YAML_ENV_KEY_RE.finditer(txt):
                _add(m.group(1), str(f.relative_to(root)))
            for m in _DOCKERFILE_ENV_RE.finditer(txt):
                _add(m.group(1), str(f.relative_to(root)))

    # Next + vercel
    for fname in ("next.config.js", "next.config.mjs", "next.config.ts",
                   "vercel.json"):
        f = root / fname
        if not f.is_file():
            continue
        try:
            txt = f.read_text(errors="ignore")
        except OSError:
            continue
        for m in _ENV_REF_RE.finditer(txt):
            _add(m.group(1), fname)

    # AST-light scan of source code (regex — not a full parser, but
    # accurate for `process.env.X` / `os.environ['X']` / `os.getenv('X')`)
    for ext in ("py", "ts", "tsx", "js", "mjs"):
        for f in root.rglob(f"*.{ext}"):
            # Skip vendored/built dirs
            if any(p in {"node_modules", "dist", "build", ".next",
                          "__pycache__", ".venv", "venv"}
                     for p in f.parts):
                continue
            try:
                txt = f.read_text(errors="ignore")
            except OSError:
                continue
            for m in _ENV_REF_RE.finditer(txt):
                _add(m.group(1), str(f.relative_to(root)))

    extended = dict(manifest)
    existing = set(manifest.get("env_vars", []))
    new_env = list(manifest.get("env_vars", []))
    for name in found:
        if name not in existing:
            new_env.append(name)
    extended["env_vars"] = new_env
    extended["env_var_sources"] = {n: srcs for n, srcs in found.items()}
    return extended


def project_history_names(project_root: "Path", *,  # noqa: F821
                              max_commits: int = 200) -> set[str]:
    """Strategy #14 plumbing: walk the last `max_commits` commits of
    .env.example and similar files, collect every env name that
    has *ever* been declared. The strategy_project_history function
    then flags fabricated names that match the historical set."""
    import subprocess
    from pathlib import Path
    root = Path(project_root)
    files = [".env.example", ".env.template", ".env.sample",
              "flags.json", "schema.prisma"]
    history: set[str] = set()
    for f in files:
        target = root / f
        if not target.is_file() and not (root / ".git").is_dir():
            continue
        try:
            log = subprocess.run(
                ["git", "log", "-p", "--pretty=format:",
                  f"-{max_commits}", "--", f],
                cwd=str(root),
                capture_output=True, text=True, timeout=10,
                check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if log.returncode != 0:
            continue
        for line in log.stdout.splitlines():
            # `+FOO=bar` or `-FOO=bar` lines in env diffs.
            # Skip diff headers (`+++`, `---`) and only consider
            # the body lines that start with a single +/-.
            if (line.startswith(("+", "-"))
                    and not line.startswith(("+++", "---"))):
                # Match the env-var name regardless of value form.
                m = re.match(r"^([A-Z_][A-Z0-9_]+)\s*=", line[1:])
                if m:
                    history.add(m.group(1))
            # Flag keys in JSON (true/false flag toggles in flags.json).
            m = re.search(r'"([a-z][a-z0-9-]+)"\s*:\s*(?:true|false)',
                            line)
            if m:
                history.add(m.group(1))
    return history


def summarize_outcomes(outcomes: list[RewriteOutcome]) -> dict:
    """Aggregate analytics for tuning."""
    by_strategy = {}
    by_verdict = {}
    for o in outcomes:
        by_verdict[o.final_verdict] = by_verdict.get(o.final_verdict, 0) + 1
        if o.chosen:
            s = o.chosen.strategy
            by_strategy.setdefault(s, {"hits": 0, "domains": set()})
            by_strategy[s]["hits"] += 1
            by_strategy[s]["domains"].add(o.domain)
    # JSON-friendly
    by_strategy_out = {
        s: {"hits": v["hits"], "domains": sorted(v["domains"])}
        for s, v in by_strategy.items()
    }
    return {
        "total_bad_names": len(outcomes),
        "by_verdict": by_verdict,
        "by_strategy": by_strategy_out,
    }
