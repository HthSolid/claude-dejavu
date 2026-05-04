"""
claude-dejavu lint strategy registry.

Pluggable architecture for hallucination-detection strategies. Each
strategy is independent, declares which languages and references it
applies to, and contributes Issue records to a merged verdict.

Design principles:
  - Deterministic-first: strategies should use AST + index lookups
    + LSP / schema validation rather than AI judgement when possible.
    Custom code is faster, more debuggable, and doesn't introduce
    its own hallucinations.
  - Composable: many strategies vote on the same edit. The merger
    picks the strongest signal per name.
  - Self-describing: each strategy carries (name, languages, enabled).
    `dejavu config` exposes per-strategy enable/disable.
  - Outcome-logged: every strategy decision writes a row to
    correction_outcomes with `outcome_signal` = strategy name. The
    v0.3.3 adaptive-threshold work uses this to tune each strategy
    independently.

To add a new strategy: drop a file in this directory implementing
the LintStrategy interface and register it via @register.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Protocol


class IssueSeverity(str, Enum):
    OK = "ok"               # confirmed real
    LOW_CONFIDENCE = "low"  # close fuzzy match — likely a typo
    UNKNOWN = "unknown"     # no plausible match — likely fabricated


@dataclass
class Issue:
    """One finding from one strategy. The merger collects these
    across all strategies and picks the strongest verdict per name.
    """
    strategy: str                    # which strategy raised it ('symbol_grounding', 'route_grounding', ...)
    severity: IssueSeverity
    name: str                        # the referenced name as written in the proposed edit
    candidate: str | None = None     # closest known name (when severity != UNKNOWN with a candidate)
    confidence: float = 0.0          # 0.0..1.0 — strategy's confidence the candidate is correct
    metadata: dict = field(default_factory=dict)  # strategy-specific extras (file path, line, sim, ed, etc.)
    reason: str = ""                 # human-readable explanation for systemMessage
    fix: str = ""                    # actionable correction instruction


@dataclass
class LintContext:
    """Inputs passed to every strategy's check()."""
    content: str                     # full proposed file content
    file_path: str
    project_slug: str
    language: str                    # 'python' | 'typescript' | 'javascript' | 'tsx' | 'go' | 'rust' | 'java' | 'ruby' | 'bash'
    conn: Any                        # psycopg2 connection
    # Pre-extracted, shared across strategies that need them:
    defined: set[str] = field(default_factory=set)
    referenced: set[str] = field(default_factory=set)
    var_types: dict[str, str] = field(default_factory=dict)
    member_calls: list[tuple[str, str]] = field(default_factory=list)
    # Optional channels (lazy-evaluated — only computed if a strategy asks):
    weaviate_url: str | None = None
    extra: dict = field(default_factory=dict)


class LintStrategy(Protocol):
    """The interface every strategy implements. Pure protocol —
    strategies are simple classes, not subclasses.
    """
    name: str
    languages: set[str]              # 'all' = applies to every supported language
    enabled_by_default: bool

    def applies_to(self, ctx: LintContext) -> bool:
        ...

    def check(self, ctx: LintContext) -> list[Issue]:
        ...


_REGISTRY: dict[str, LintStrategy] = {}


def register(strategy: LintStrategy) -> LintStrategy:
    """Decorator-friendly registration. Idempotent — a re-register
    overwrites (so live-edit cycles don't accumulate stale strategies).
    """
    _REGISTRY[strategy.name] = strategy
    return strategy


def all_strategies() -> list[LintStrategy]:
    return list(_REGISTRY.values())


def enabled_strategies(disabled: Iterable[str] = ()) -> list[LintStrategy]:
    skip = set(disabled)
    return [s for s in _REGISTRY.values()
            if s.enabled_by_default and s.name not in skip]


def get_strategy(name: str) -> LintStrategy | None:
    return _REGISTRY.get(name)


# Auto-import strategies so registration happens on first import of this
# package. Order doesn't matter — all strategies are independent.
def _load_builtin_strategies() -> None:
    from importlib import import_module
    for mod in (
        "symbol_grounding",
        "semantic_intent",
        "route_grounding",
        "lsp_verification",
        "page_duplicate",
        "env_grounding",
        "db_grounding",
        "graphql_grounding",
        "flag_grounding",
        "quickwin_grounding",
        "import_grounding",
        "prose_grounding",
    ):
        try:
            import_module(f"lint_strategies.{mod}")
        except Exception as e:
            # A broken strategy must never block the rest of the pipeline.
            import sys
            sys.stderr.write(f"claude-dejavu: skipped strategy '{mod}' ({type(e).__name__}: {e})\n")


_load_builtin_strategies()
