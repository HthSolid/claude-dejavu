#!/usr/bin/env python3
"""
claude-dejavu config service — typed read/write for config.env.

Used by:
  - `claude-dejavu config get|set|list` CLI
  - `dejavu_config` MCP tool
  - `/dejavu-config` slash command
  - hooks/* (already read config.env directly; this just centralizes write)

Schema-driven so the user can't accidentally set typos or invalid values.
Each known key has a type, a default, an allowed-values set (optional),
and a description that surfaces in `config list`.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Setting:
    key: str
    type: str                    # 'bool' | 'enum' | 'int' | 'string' | 'readonly'
    default: str
    description: str
    allowed: tuple[str, ...] = ()
    secret: bool = False         # True for PG_PASS / WV_TOKEN style values
    runtime: str = "next_session"  # 'next_session' | 'next_install' | 'live'


# Authoritative settings catalog. New knobs go here, with type/default/docs.
SETTINGS: dict[str, Setting] = {
    # ─── Behaviour ──────────────────────────────────────────────────────
    "INJECT_SESSION_START": Setting(
        "INJECT_SESSION_START", "bool", "true",
        "Inject prior-session context preamble at SessionStart.",
        allowed=("true", "false"),
    ),
    "AUTO_REINDEX": Setting(
        "AUTO_REINDEX", "bool", "true",
        "Automatically index a project's source files on the first SessionStart "
        "where the symbols table for that project is empty.",
        allowed=("true", "false"),
    ),
    "UPDATE_CHECK": Setting(
        "UPDATE_CHECK", "bool", "true",
        "Check GitHub for newer plugin tags on SessionStart and surface a "
        "one-line update notice.",
        allowed=("true", "false"),
    ),
    "LINT_MODE": Setting(
        "LINT_MODE", "enum", "warn",
        "PreToolUse lint behavior on Edit/Write/MultiEdit. "
        "off=disabled, warn=advisory only (default), suggest=advisory + rename "
        "table, block=refuse fabricated names, autofix=refuse on any non-ok "
        "verdict and force a re-issue with renames.",
        allowed=("off", "warn", "suggest", "block", "autofix"),
    ),
    "LINT_TIMEOUT_S": Setting(
        "LINT_TIMEOUT_S", "int", "5",
        "Max seconds the PreToolUse lint hook may run before warning.",
    ),
    "CLAUDE_DEJAVU_DEFAULT_SCOPE": Setting(
        "CLAUDE_DEJAVU_DEFAULT_SCOPE", "enum", "workspace",
        "Default search scope when no --scope flag is passed.",
        allowed=("global", "project", "workspace"),
    ),

    # ─── JIT recall (v0.8.5 Context Economy phase 2 — G2) ───────────────
    # All five settings are opt-in: master switch defaults to false so
    # installing v0.8.5 changes nothing until the user explicitly turns
    # the UserPromptSubmit hook on. Marked readonly so they can only be
    # changed by editing config.env directly — this is a deliberate
    # "you know what you're doing" gate while the feature is new.
    "JIT_RECALL_ENABLED": Setting(
        "JIT_RECALL_ENABLED", "readonly", "false",
        "Master switch for the UserPromptSubmit JIT recall hook. When "
        "true, every user prompt fans out to a hybrid Weaviate search "
        "and injects up to JIT_RECALL_TOP_N gists from past sessions "
        "as system context. Off by default — opt in via config.env.",
        allowed=("true", "false"), runtime="next_session",
    ),
    "JIT_RECALL_TOP_N": Setting(
        "JIT_RECALL_TOP_N", "readonly", "3",
        "Maximum number of past-session gists to inject per prompt.",
        runtime="next_session",
    ),
    "JIT_RECALL_TOKEN_CAP": Setting(
        "JIT_RECALL_TOKEN_CAP", "readonly", "500",
        "Hard cap on the size of the injected block, expressed in "
        "tokens (≈chars/4). Truncate-and-mark if exceeded.",
        runtime="next_session",
    ),
    "JIT_RECALL_SCORE_THRESHOLD": Setting(
        "JIT_RECALL_SCORE_THRESHOLD", "readonly", "2.0",
        "Minimum top-hit BM25 score. The hook uses BM25 keyword search "
        "(not hybrid) because ClaudeDejavuTurn ships with "
        "vectorizer:none — BM25 scores typically range 1-20+ depending "
        "on term rarity. Default 2.0 keeps marginal matches out; bump "
        "to ~5.0 for stricter filtering, drop to ~0.5 to surface more "
        "weakly-matched context. If the best hit scores below this, "
        "the hook injects nothing — silence beats noise.",
        runtime="next_session",
    ),
    "JIT_RECALL_MIN_PROMPT_CHARS": Setting(
        "JIT_RECALL_MIN_PROMPT_CHARS", "readonly", "40",
        "Skip recall for prompts shorter than this. Short prompts "
        "(\"ok\", \"yes\", \"next\") have no semantic content worth "
        "searching against and would just produce false-positive hits.",
        runtime="next_session",
    ),

    # ─── topic_reweight (v0.8.9 Context Economy phase 6) ────────────────
    # Opt-in topic-aware re-ranking via dejavu_bridge.topic_reweight.
    # The bridge multiplies each hit's score by a factor depending on
    # whether the hit's session topic (derived from `ai_title`) matches
    # the query's topic. Our recall pipeline uses higher-is-better
    # scoring, so with the default factors (same=0.7, cross=1.3) this
    # produces a DIVERSITY BOOST: cross-topic hits get promoted, same-
    # topic hits get demoted. To invert into a relevance boost, swap
    # the factors (same=1.3, cross=0.7).
    "TOPIC_REWEIGHT_ENABLED": Setting(
        "TOPIC_REWEIGHT_ENABLED", "readonly", "false",
        "Master switch for opt-in topic-aware re-ranking applied to "
        "recall results (CLI search + JIT hook). Off by default. "
        "When true, the bridge's topic_reweight primitive multiplies "
        "each hit's score by SAME_FACTOR or CROSS_FACTOR depending "
        "on whether its session's ai_title topic matches the query's "
        "topic. Default factors produce a diversity boost — swap "
        "them for a relevance boost. Opt in via config.env.",
        allowed=("true", "false"), runtime="next_session",
    ),
    "TOPIC_REWEIGHT_SAME_FACTOR": Setting(
        "TOPIC_REWEIGHT_SAME_FACTOR", "readonly", "0.7",
        "Multiplicative factor applied to hits whose session topic "
        "matches the query topic. Default 0.7 (demote — diversity "
        "boost). Set to 1.3 to invert into a relevance boost.",
        runtime="next_session",
    ),
    "TOPIC_REWEIGHT_CROSS_FACTOR": Setting(
        "TOPIC_REWEIGHT_CROSS_FACTOR", "readonly", "1.3",
        "Multiplicative factor applied to hits whose session topic "
        "does NOT match the query topic (or is unknown). Default 1.3 "
        "(promote — diversity boost). Set to 0.7 to invert.",
        runtime="next_session",
    ),

    # ─── Connection / infra (readonly via this UI; change via install.py) ──
    "PG_HOST": Setting("PG_HOST", "readonly", "localhost",
                       "Postgres host (set by install.py).", runtime="next_install"),
    "PG_PORT": Setting("PG_PORT", "readonly", "5432",
                       "Postgres port.", runtime="next_install"),
    "PG_USER": Setting("PG_USER", "readonly", "postgres",
                       "Postgres user.", runtime="next_install"),
    "PG_PASS": Setting("PG_PASS", "readonly", "postgres",
                       "Postgres password.", secret=True, runtime="next_install"),
    "PG_DB":   Setting("PG_DB", "readonly", "",
                       "Postgres database name.", runtime="next_install"),
    "WV_URL":  Setting("WV_URL", "readonly", "http://localhost:8080",
                       "Weaviate base URL.", runtime="next_install"),
    "WV_CLASS": Setting("WV_CLASS", "readonly", "ClaudeDejavuTurn",
                        "Weaviate class name for ingested turns.", runtime="next_install"),
    "WV_TENANT": Setting("WV_TENANT", "readonly", "",
                         "Weaviate tenant (per-Linux-user isolation).", runtime="next_install"),
    "DATA_ROOT": Setting("DATA_ROOT", "readonly", "",
                         "Off-tree data directory (snapshots + venv + config).", runtime="next_install"),
    "OS_USER": Setting("OS_USER", "readonly", "",
                       "Linux/Mac/Windows user owning this install.", runtime="next_install"),
    "SHARED": Setting("SHARED", "readonly", "0",
                      "1 if --shared was used (single DB for all users).", runtime="next_install"),
}


# ─── Per-OS config.env path ────────────────────────────────────────────────


def _data_root() -> Path:
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-dejavu"
    if sys.platform.startswith("win"):
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or str(Path.home() / "AppData" / "Local")
        )
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "claude-dejavu" if xdg else Path.home() / ".local" / "share" / "claude-dejavu"


def _config_path() -> Path:
    return _data_root() / "config.env"


# ─── Read / write ──────────────────────────────────────────────────────────


def _parse_config_env(text: str) -> tuple[list[str], dict[str, str]]:
    """Return (lines, kv) — preserving order so `set` rewrites in place."""
    lines = text.splitlines()
    kv: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            kv[k.strip()] = v.strip()
    return lines, kv


def load() -> dict[str, str]:
    p = _config_path()
    if not p.exists():
        return {}
    return _parse_config_env(p.read_text(errors="replace"))[1]


def get(key: str) -> str:
    """Return current value (or default if not set / no config)."""
    if key not in SETTINGS:
        raise KeyError(f"Unknown setting: {key!r}. Try: claude-dejavu config list")
    cfg = load()
    return cfg.get(key, SETTINGS[key].default)


def set_value(key: str, value: str) -> None:
    """Persist a value to config.env. Validates against the schema."""
    if key not in SETTINGS:
        raise KeyError(f"Unknown setting: {key!r}. Try: claude-dejavu config list")
    s = SETTINGS[key]
    if s.type == "readonly":
        raise PermissionError(
            f"{key} is read-only via this UI. Change it by re-running "
            f"scripts/install.py or editing config.env directly."
        )
    if s.type == "bool":
        if value.lower() not in ("true", "false"):
            raise ValueError(f"{key} must be true or false (got {value!r})")
        value = value.lower()
    elif s.type == "enum":
        if value not in s.allowed:
            raise ValueError(
                f"{key} must be one of {s.allowed} (got {value!r})"
            )
    elif s.type == "int":
        try:
            int(value)
        except ValueError:
            raise ValueError(f"{key} must be an integer (got {value!r})")
    # 'string' has no validation

    p = _config_path()
    if not p.exists():
        # config.env doesn't exist yet — install.py hasn't run. Create
        # a minimal one with a comment so users know what happened.
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "# claude-dejavu config — created by `claude-dejavu config set`\n"
            f"{key}={value}\n"
        )
        return

    text = p.read_text(errors="replace")
    lines, kv = _parse_config_env(text)
    if key in kv:
        # Replace in place to preserve comments + ordering.
        new_lines = []
        replaced = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line); continue
            k = stripped.split("=", 1)[0].strip()
            if k == key and not replaced:
                new_lines.append(f"{key}={value}")
                replaced = True
            else:
                new_lines.append(line)
        p.write_text("\n".join(new_lines) + ("\n" if not new_lines[-1].endswith("\n") else ""))
    else:
        # Append at the end.
        with p.open("a") as f:
            if not text.endswith("\n"):
                f.write("\n")
            f.write(f"{key}={value}\n")


def reset(key: str) -> None:
    """Reset a key to its default by removing it from config.env."""
    if key not in SETTINGS:
        raise KeyError(f"Unknown setting: {key!r}")
    p = _config_path()
    if not p.exists():
        return
    text = p.read_text(errors="replace")
    new_text = re.sub(rf"^{re.escape(key)}\s*=.*\n?", "", text, flags=re.MULTILINE)
    p.write_text(new_text)


def list_settings(include_secrets: bool = False) -> list[dict]:
    """Return a list of all known settings + their current/default values
    + descriptions. Secret values are masked unless include_secrets=True.
    """
    cfg = load()
    out: list[dict] = []
    for key, s in SETTINGS.items():
        cur = cfg.get(key, s.default)
        if s.secret and not include_secrets and cur:
            cur = "***"
        out.append({
            "key": key,
            "type": s.type,
            "current": cur,
            "default": s.default,
            "is_default": cur == s.default,
            "allowed": list(s.allowed),
            "description": s.description,
            "runtime": s.runtime,
            "secret": s.secret,
        })
    return out


def list_to_text(rows: list[dict]) -> str:
    """Render a `config list` result as a compact terminal table."""
    if not rows:
        return "(no settings)"
    width = max(len(r["key"]) for r in rows)
    lines = []
    for r in rows:
        marker = " " if r["is_default"] else "*"
        cur = r["current"]
        kind = r["type"]
        suffix = ""
        if kind == "enum":
            suffix = f"  (allowed: {', '.join(r['allowed'])})"
        elif kind == "readonly":
            suffix = "  (read-only)"
        lines.append(f"  {marker} {r['key']:<{width}}  {cur!r:<10}  {kind:<10}  {r['description']}{suffix}")
    lines.append("")
    lines.append("  * = customized (differs from default)")
    return "\n".join(lines)


if __name__ == "__main__":
    # Tiny demo when run directly
    print(list_to_text(list_settings()))
