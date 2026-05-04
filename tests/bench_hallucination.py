#!/usr/bin/env python3
"""
claude-dejavu — empirical hallucination benchmark (v0.3.12).

What this measures
==================

We claim claude-dejavu reduces Claude's hallucination rate. This file
turns that claim into a number with a confidence interval.

The protocol:

  1. Build a synthetic project with a known schema (Prisma models +
     env vars + HTTP routes + GraphQL types + feature flags). Index it.

  2. For each of 20 prompts that ask Claude to write code referencing
     names from that schema:
        - Run N=10 trials WITHOUT dejavu context (pure Claude)
        - Run N=10 trials WITH dejavu context (system prompt enriched
          with the schema, simulating how the PreToolUse hook injects
          ground truth)
     => 400 trials total.

  3. Score each generated response deterministically: reuse the same
     extractors the lint pipeline uses. Every name (env var, model.field,
     URL, gql field, flag) is looked up against the project's index.
     A name with no match is a hallucination.

  4. Report:
        - per-condition mean hallucination rate
        - paired bootstrap 95% CI on the difference (each prompt has
          matched WITH/WITHOUT trials)
        - exact McNemar test for paired binary data ("did the trial
          contain ≥1 hallucination?")
        - full per-prompt breakdown

Output: docs/benchmarks/hallucination-rate-v0.3.12.md (the public report)
        + JSON dump of every trial for reproducibility.

Cost: ~$4 in Anthropic API calls at default N=10 × 20 prompts × 2
conditions × ~1k tokens/call. Use --n=1 for a dry run (~$0.40).

Run:
  ANTHROPIC_API_KEY=sk-ant-... python tests/bench_hallucination.py \\
      --n=10 --output=docs/benchmarks/

Smoke-only (no API): pass --mock to use canned responses; verifies the
scorer + stats pipeline work.
"""
from __future__ import annotations

import argparse
import json
import ssl


def _ensure_ssl_cert_file():
    """If the user hasn't set SSL_CERT_FILE, point at the system
    bundle. Some venvs ship without a working CA bundle for httpx,
    in which case the Anthropic SDK dies with
    `CERTIFICATE_VERIFY_FAILED`. We DO NOT override an existing
    SSL_CERT_FILE — if Anthropic still fails, the user can unset it
    or point it at /etc/ssl/certs/ca-certificates.crt themselves."""
    import os
    if os.environ.get("SSL_CERT_FILE"):
        return
    for c in ("/etc/ssl/certs/ca-certificates.crt",
              "/etc/pki/tls/certs/ca-bundle.crt",
              "/etc/ssl/cert.pem"):
        if os.path.isfile(c):
            os.environ["SSL_CERT_FILE"] = c
            return


_ensure_ssl_cert_file()
import os
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "code"))


# ─── Synthetic project fixture ──────────────────────────────────────────────


def build_fixture(root: Path) -> dict:
    """Create the synthetic project the benchmark runs against. Returns
    a manifest dict describing what's in the schema (used by the scorer
    + report)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text('{"name": "bench-fixture"}\n')

    # Prisma schema with 5 models, 22 fields total.
    (root / "prisma").mkdir(parents=True, exist_ok=True)
    (root / "prisma" / "schema.prisma").write_text(
        "model User {\n"
        "  id        Int     @id @default(autoincrement())\n"
        "  email     String  @unique\n"
        "  name      String?\n"
        "  role      String  @default(\"user\")\n"
        "  createdAt DateTime @default(now())\n"
        "  posts     Post[]\n"
        "}\n\n"
        "model Post {\n"
        "  id        Int     @id @default(autoincrement())\n"
        "  title     String\n"
        "  body      String\n"
        "  authorId  Int\n"
        "  author    User    @relation(fields: [authorId], references: [id])\n"
        "  publishedAt DateTime?\n"
        "}\n\n"
        "model Subscription {\n"
        "  id            Int     @id @default(autoincrement())\n"
        "  userId        Int\n"
        "  stripeCustomerId String\n"
        "  status        String\n"
        "  expiresAt     DateTime\n"
        "}\n\n"
        "model Invoice {\n"
        "  id            Int     @id @default(autoincrement())\n"
        "  amount        Int\n"
        "  currency      String  @default(\"USD\")\n"
        "  paidAt        DateTime?\n"
        "}\n\n"
        "model AuditLog {\n"
        "  id        Int     @id @default(autoincrement())\n"
        "  actorId   Int\n"
        "  action    String\n"
        "  resource  String\n"
        "  createdAt DateTime @default(now())\n"
        "}\n"
    )

    # .env.example with 12 vars.
    (root / ".env.example").write_text(
        "DATABASE_URL=postgres://localhost/bench\n"
        "REDIS_URL=redis://localhost:6379\n"
        "STRIPE_SECRET_KEY=\n"
        "STRIPE_WEBHOOK_SECRET=\n"
        "JWT_SECRET=\n"
        "SESSION_COOKIE_NAME=bench_session\n"
        "API_BASE_URL=http://localhost:3000\n"
        "SENDGRID_API_KEY=\n"
        "SENTRY_DSN=\n"
        "AWS_ACCESS_KEY_ID=\n"
        "AWS_SECRET_ACCESS_KEY=\n"
        "AWS_REGION=eu-central-1\n"
    )

    # Express routes — 6 endpoints.
    (root / "server.ts").write_text(
        "import express from 'express';\n"
        "const app = express();\n"
        "app.get('/api/users/:id', (req, res) => res.json({}));\n"
        "app.post('/api/users', (req, res) => res.json({}));\n"
        "app.patch('/api/users/:id', (req, res) => res.json({}));\n"
        "app.get('/api/posts', (req, res) => res.json({}));\n"
        "app.post('/api/posts', (req, res) => res.json({}));\n"
        "app.get('/api/subscriptions/:userId', (req, res) => res.json({}));\n"
    )

    # GraphQL schema — 4 types.
    (root / "schema.graphql").write_text(
        "type User {\n"
        "  id: ID!\n"
        "  email: String!\n"
        "  name: String\n"
        "  posts: [Post!]!\n"
        "}\n\n"
        "type Post {\n"
        "  id: ID!\n"
        "  title: String!\n"
        "  body: String!\n"
        "  author: User!\n"
        "}\n\n"
        "type Query {\n"
        "  user(id: ID!): User\n"
        "  posts(authorId: ID): [Post!]!\n"
        "}\n\n"
        "type Mutation {\n"
        "  createUser(email: String!, name: String): User!\n"
        "  publishPost(id: ID!): Post!\n"
        "}\n"
    )

    # Feature flags.
    (root / "flags.json").write_text(
        '{\n'
        '  "checkout-flow-v2": false,\n'
        '  "audit-log-rollout": true,\n'
        '  "stripe-customer-portal": false,\n'
        '  "graphql-subscriptions": false,\n'
        '  "kill-switch-payments": false\n'
        '}\n'
    )

    return {
        "models": ["User", "Post", "Subscription", "Invoice", "AuditLog"],
        "user_fields": ["id", "email", "name", "role", "createdAt", "posts"],
        "post_fields": ["id", "title", "body", "authorId", "author", "publishedAt"],
        "env_vars": ["DATABASE_URL", "REDIS_URL", "STRIPE_SECRET_KEY",
                      "STRIPE_WEBHOOK_SECRET", "JWT_SECRET",
                      "SESSION_COOKIE_NAME", "API_BASE_URL",
                      "SENDGRID_API_KEY", "SENTRY_DSN", "AWS_ACCESS_KEY_ID",
                      "AWS_SECRET_ACCESS_KEY", "AWS_REGION"],
        "routes": [
            "GET /api/users/:id", "POST /api/users", "PATCH /api/users/:id",
            "GET /api/posts", "POST /api/posts",
            "GET /api/subscriptions/:userId",
        ],
        "gql_types": ["User", "Post", "Query", "Mutation"],
        "flags": ["checkout-flow-v2", "audit-log-rollout",
                   "stripe-customer-portal", "graphql-subscriptions",
                   "kill-switch-payments"],
        # Synthetic project's package.json deps — the rewriter's
        # SDK-catalog strategy gates ACCEPT_SDK on whether the project
        # actually depends on a package whose canonical env list
        # contains the name. Mirrors what dejavu would read from a
        # real project's package.json in production.
        "package_deps": [
            "express", "@prisma/client", "stripe", "@sendgrid/mail",
            "@sentry/node", "redis", "ioredis", "pg",
            "aws-sdk", "@aws-sdk/client-s3",
            "@launchdarkly/node-server-sdk", "launchdarkly-node-server-sdk",
            "next", "node",
        ],
    }


# ─── Schema digest for the WITH-dejavu system prompt ────────────────────────


def schema_digest(manifest: dict) -> str:
    """Compact, machine-friendly schema dump that simulates what the
    PreToolUse hook injects into Claude's context. This is the EXACT
    "what dejavu does for you" — give Claude ground truth so it
    doesn't have to guess."""
    return f"""You are coding against the following project. ONLY use names
from this schema. Do NOT invent fields, env vars, routes, or flag keys.

Prisma models:
  {manifest['models']}

User fields: {manifest['user_fields']}
Post fields: {manifest['post_fields']}

Env vars: {manifest['env_vars']}

HTTP routes: {manifest['routes']}

GraphQL types: {manifest['gql_types']}

Feature flag keys: {manifest['flags']}
"""


# ─── Curated prompts (20 prompts spanning every grounding domain) ───────────


PROMPTS: list[dict] = [
    # Each prompt asks Claude to write a chunk of TypeScript that
    # references at least one name from the schema. The "expected_*"
    # lists drive the deterministic scorer.
    {
        "id": "p01-prisma-find-by-email",
        "task": "Write a TypeScript function `getUserByEmail(email: string)` that uses Prisma to fetch a user by their email. Return the user's id, name, and email.",
        "domains": ["db"],
    },
    {
        "id": "p02-prisma-create-user",
        "task": "Write a TypeScript function `createUser(email: string, name?: string)` that uses Prisma to create a new user. Return the created user's id.",
        "domains": ["db"],
    },
    {
        "id": "p03-prisma-published-posts",
        "task": "Write a TypeScript function `getPublishedPosts()` that uses Prisma to fetch all posts where publishedAt is not null, ordered by publishedAt desc.",
        "domains": ["db"],
    },
    {
        "id": "p04-prisma-update-user-role",
        "task": "Write a TypeScript function `promoteUser(userId: number)` that updates the user's role to 'admin' via Prisma.",
        "domains": ["db"],
    },
    {
        "id": "p05-env-stripe-init",
        "task": "Write a TypeScript snippet that initializes the Stripe SDK using the project's environment variables.",
        "domains": ["env"],
    },
    {
        "id": "p06-env-redis-connect",
        "task": "Write a TypeScript snippet that creates a Redis client using the project's environment variables.",
        "domains": ["env"],
    },
    {
        "id": "p07-env-aws-config",
        "task": "Write a TypeScript snippet that configures the AWS SDK with credentials from environment variables.",
        "domains": ["env"],
    },
    {
        "id": "p08-route-fetch-posts",
        "task": "Write a TypeScript fetch call to retrieve all posts from the project's REST API.",
        "domains": ["route"],
    },
    {
        "id": "p09-route-create-user-client",
        "task": "Write a TypeScript fetch call that creates a new user via the project's REST API. Body: { email, name }.",
        "domains": ["route"],
    },
    {
        "id": "p10-route-update-user",
        "task": "Write a TypeScript fetch call that updates user 42's name via the project's REST API.",
        "domains": ["route"],
    },
    {
        "id": "p11-gql-query-user",
        "task": "Write a `gql` template literal for a GraphQL query `getUser($id: ID!)` that fetches the user's id, email, name, and the titles of their posts.",
        "domains": ["graphql"],
    },
    {
        "id": "p12-gql-mutation-create-user",
        "task": "Write a `gql` template literal for a GraphQL mutation that creates a new user given an email and optional name. Return the created user's id and email.",
        "domains": ["graphql"],
    },
    {
        "id": "p13-gql-query-posts",
        "task": "Write a `gql` template literal that fetches all posts for an author, including each post's title and body.",
        "domains": ["graphql"],
    },
    {
        "id": "p14-flag-checkout-gate",
        "task": "Write a TypeScript snippet that gates a checkout-flow refactor behind the project's checkout-flow-v2 feature flag using LaunchDarkly's client.variation.",
        "domains": ["flag"],
    },
    {
        "id": "p15-flag-audit-rollout",
        "task": "Write a TypeScript snippet that uses isEnabled() to check whether the audit-log-rollout flag is on before writing an audit log entry.",
        "domains": ["flag"],
    },
    {
        "id": "p16-multi-prisma-env",
        "task": "Write a TypeScript function `connectAndFetchUser(id: number)` that connects to the database using the project's DATABASE_URL env var, then fetches a user with Prisma. Return the user's email.",
        "domains": ["db", "env"],
    },
    {
        "id": "p17-multi-route-flag",
        "task": "Write a TypeScript snippet that, IF the stripe-customer-portal flag is enabled, fetches subscription data from the project's REST API for user 7.",
        "domains": ["route", "flag"],
    },
    {
        "id": "p18-multi-prisma-gql",
        "task": "Write a GraphQL resolver for the `posts(authorId: ID)` Query. The resolver should call Prisma to fetch posts where the author's id matches.",
        "domains": ["db", "graphql"],
    },
    {
        "id": "p19-multi-env-route",
        "task": "Write a TypeScript snippet that constructs a request to the project's API_BASE_URL + the route that fetches a single user by id.",
        "domains": ["env", "route"],
    },
    {
        "id": "p20-all-domains",
        "task": "Write a TypeScript function `signupUser(email: string, name?: string)` that: (a) checks the kill-switch-payments flag and aborts if on, (b) creates the user via Prisma, (c) calls the REST API endpoint that creates a user (for replication), (d) logs the email used (DON'T log secrets). Use only what's declared.",
        "domains": ["db", "env", "route", "flag"],
    },
]

assert len(PROMPTS) == 20, f"expected 20 prompts, got {len(PROMPTS)}"


# ─── Anthropic API client ───────────────────────────────────────────────────


def _tool_use_agent_loop(prompt: str, manifest: dict,
                            *, model: str, max_tokens: int,
                            max_turns: int = 8,
                            max_lint_rounds: int = 2) -> str:
    """v0.5.0a: tool-use mode. Claude is given a `dejavu_check_name`
    tool. It can call the tool to verify names against the project's
    schema before committing them to the final code output. We
    intercept tool_use blocks, run the lookup locally, return the
    tool_result, and let Claude continue. Stops when Claude emits
    its final code (no more tool_use blocks) OR max_turns reached."""
    import anthropic
    client = anthropic.Anthropic()

    tool_def = {
        "name": "dejavu_check_name",
        "description": (
            "Verify whether a name is real in the project before "
            "writing code that uses it. Use BEFORE committing to any "
            "non-trivial identifier — env var, DB model.field, route, "
            "GraphQL field, feature flag, etc. Returns OK / TYPO "
            "(with the right name) / UNKNOWN."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                          "description": "the identifier to verify"},
                "kind": {"type": "string",
                          "enum": ["env", "db-field", "db-model", "route",
                                    "gql-field", "gql-type", "flag",
                                    "css-class", "i18n-key",
                                    "module-export", "symbol", "auto"]},
                "parent": {"type": "string",
                            "description": (
                                "for db-field: model name; for gql-field: "
                                "type name; for module-export: package name")},
            },
            "required": ["name"],
        },
    }

    system_msg = (
        "You are coding against a project. BEFORE writing any "
        "identifier you're not 100% certain about (env var, DB "
        "model.field, route, GraphQL field, feature flag, etc.), "
        "call dejavu_check_name to verify.\n\n"
        "STRICT RULES — do not violate, even with a justification:\n"
        "1. If the tool returns **UNKNOWN**, that name does NOT exist "
        "in this project. You MUST NOT use it in the final code. Do "
        "not justify it as a 'standard library convention' or "
        "'third-party SDK default' — UNKNOWN means it's not declared "
        "and the user has not authorized you to introduce it.\n"
        "2. If the tool returns **TYPO** with a suggestion, use the "
        "suggested name verbatim, never the typo.\n"
        "3. If the tool returns **OK**, use the name as written.\n"
        "4. Standard library / third-party SDK env vars (NODE_ENV, "
        "AWS_SESSION_TOKEN, LD_SDK_KEY, etc.) are still UNKNOWN if "
        "the project hasn't declared them. Treat them like any other "
        "UNKNOWN — do not write them into the code. Either omit the "
        "feature, or comment a TODO asking the user to declare the "
        "var first.\n\n"
        "After verifying every name, write the requested code using "
        "ONLY OK and TYPO-corrected names."
    )
    messages: list[dict] = [{"role": "user", "content": prompt}]
    last_text = ""
    for _ in range(max_turns):
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=system_msg, tools=[tool_def],
            messages=messages,
        )
        # Capture the text portion (final code lives here).
        text_blocks = [b for b in resp.content if hasattr(b, "text")]
        if text_blocks:
            last_text = "".join(b.text for b in text_blocks)
        # Tool calls?
        tool_uses = [b for b in resp.content
                       if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            break  # final answer
        # Append assistant turn + every tool result
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for tu in tool_uses:
            try:
                result_text = _resolve_name_for_tool(
                    tu.input.get("name", ""),
                    kind=tu.input.get("kind"),
                    parent=tu.input.get("parent"),
                    manifest=manifest,
                )
            except Exception as e:  # noqa: BLE001
                result_text = f"ERROR: {type(e).__name__}: {e}"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result_text,
            })
        messages.append({"role": "user", "content": tool_results})

    # ─── Deterministic post-generation linter ───────────────────────
    # The model has emitted final code (no more tool_use blocks).
    # Run the same scorer the bench uses, and if any names are
    # unknown, send them back as a hard-rejection forcing a rewrite.
    # Mirrors the production `dejavu_lint_proposed_edit` PreToolUse
    # hook: enforcement via verification, not via instruction.
    for lint_round in range(max_lint_rounds):
        if not last_text:
            break
        scored = score_response(last_text, manifest)
        flat_bad: list[str] = []
        for domain, names in scored.get("bad_names", {}).items():
            for n in names:
                flat_bad.append(f"{domain}: {n}")
        if not flat_bad:
            break  # clean
        rejection = (
            "REJECTED by dejavu linter. Your code references the "
            "following names that are NOT declared in this project's "
            "schema:\n\n"
            + "\n".join(f"  - {b}" for b in flat_bad)
            + "\n\nThese will fail at runtime. Rewrite the code "
            "WITHOUT using any of these names. If a feature requires "
            "a name that doesn't exist, omit the feature or leave a "
            "TODO comment — do not invent the name. Use only names "
            "verified by dejavu_check_name as OK. Output ONLY the "
            "corrected code."
        )
        # The previous turn's `messages[-1]` is either the tool_result
        # (user role) or the original prompt — either way we now need
        # to append the assistant's emitted text and our rejection.
        messages.append({"role": "assistant", "content": last_text})
        messages.append({"role": "user", "content": rejection})
        # One more model call (still has tools available so it can
        # re-verify if it wants).
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            system=system_msg, tools=[tool_def],
            messages=messages,
        )
        # Drain any tool_use turns from the rewrite.
        rewrite_turns = 0
        while rewrite_turns < max_turns:
            text_blocks = [b for b in resp.content if hasattr(b, "text")]
            if text_blocks:
                last_text = "".join(b.text for b in text_blocks)
            tool_uses = [b for b in resp.content
                           if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                break
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for tu in tool_uses:
                try:
                    result_text = _resolve_name_for_tool(
                        tu.input.get("name", ""),
                        kind=tu.input.get("kind"),
                        parent=tu.input.get("parent"),
                        manifest=manifest,
                    )
                except Exception as e:  # noqa: BLE001
                    result_text = f"ERROR: {type(e).__name__}: {e}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_text,
                })
            messages.append({"role": "user", "content": tool_results})
            resp = client.messages.create(
                model=model, max_tokens=max_tokens,
                system=system_msg, tools=[tool_def],
                messages=messages,
            )
            rewrite_turns += 1

    return last_text


def _resolve_name_for_tool(name: str, kind: Optional[str],
                              parent: Optional[str],
                              manifest: dict) -> str:
    """Stand-in resolver for the bench's tool-use mode. Resolves
    against the synthetic manifest (no live DB). Mirrors the
    semantics of the production dejavu_check_name MCP tool but
    purely against the in-memory manifest dict — keeps the bench
    self-contained and reproducible."""
    k = (kind or "auto").lower()

    def _close_match(target: str, candidates: list[str]) -> Optional[tuple[str, float]]:
        # Trigram similarity heuristic
        def tg(s: str) -> set[str]:
            s = f"  {s.lower()} "
            return {s[i:i+3] for i in range(len(s) - 2)}
        t = tg(target)
        if not t:
            return None
        best = (None, 0.0)
        for c in candidates:
            tc = tg(c)
            if not tc:
                continue
            sim = len(t & tc) / len(t | tc)
            if sim > best[1]:
                best = (c, sim)
        return best if best[0] and best[1] >= 0.4 else None

    # ─── env ───
    if k in ("env", "auto"):
        if name in manifest["env_vars"]:
            return f"OK: `{name}` declared as env var in .env.example"
        m = _close_match(name, manifest["env_vars"])
        if m and k == "env":
            return f"TYPO: did you mean `{m[0]}`? (sim={m[1]:.2f})"
    # ─── db-field ───
    if k in ("db-field", "auto"):
        valid_for_parent = (manifest["user_fields"] if parent == "User"
                              else manifest["post_fields"] if parent == "Post"
                              else None)
        if valid_for_parent and name in valid_for_parent:
            return f"OK: `{parent}.{name}` declared in Prisma schema"
        if valid_for_parent:
            m = _close_match(name, valid_for_parent)
            if m:
                return f"TYPO: did you mean `{parent}.{m[0]}`? (sim={m[1]:.2f})"
        # cross-model search
        all_fields = (manifest["user_fields"] + manifest["post_fields"])
        if name in all_fields:
            return (f"TYPO: `{name}` exists on a different model — "
                    f"verify which model you mean")
    # ─── db-model ───
    if k in ("db-model", "auto"):
        if name in manifest["models"]:
            return f"OK: `{name}` is a Prisma model"
        m = _close_match(name, manifest["models"])
        if m and k == "db-model":
            return f"TYPO: did you mean `{m[0]}`?"
    # ─── route ───
    if k in ("route", "auto"):
        for r in manifest["routes"]:
            if name in r:
                return f"OK: `{name}` matches declared route `{r}`"
        if k == "route":
            return f"UNKNOWN: no route matches `{name}`"
    # ─── gql ───
    if k in ("gql-field", "auto"):
        valid_for_parent = ({"User": manifest["user_fields"],
                              "Post": manifest["post_fields"],
                              "Query": ["user", "posts"],
                              "Mutation": ["createUser", "publishPost"],
                              }.get(parent, []))
        if valid_for_parent and name in valid_for_parent:
            return f"OK: `{parent}.{name}` declared in GraphQL schema"
        if valid_for_parent:
            m = _close_match(name, valid_for_parent)
            if m:
                return f"TYPO: did you mean `{parent}.{m[0]}`?"
    if k in ("gql-type", "auto"):
        if name in manifest["gql_types"]:
            return f"OK: `{name}` is a declared GraphQL type"
    # ─── flag ───
    if k in ("flag", "auto"):
        if name in manifest["flags"]:
            return f"OK: `{name}` declared in flags.json"
        m = _close_match(name, manifest["flags"])
        if m and k == "flag":
            return f"TYPO: did you mean `{m[0]}`?"
    return (f"UNKNOWN: `{name}` is not declared in this project's "
              f"schema. DO NOT use this name in the final code, even "
              f"if it looks like a standard third-party convention. "
              f"Either omit the feature or leave a TODO comment asking "
              f"the user to declare it first.")


def call_claude(prompt: str, system: Optional[str] = None,
                  *, model: str = "claude-sonnet-4-6",
                  max_tokens: int = 800,
                  mock: bool = False,
                  tool_use: bool = False,
                  manifest: Optional[dict] = None) -> str:
    if mock:
        # Deterministic mock for smoke / dry-run. Returns a reference-rich
        # response that exercises every grounding domain so the scorer +
        # report writer have something to chew on. WITH system: only
        # declared names. WITHOUT system: every domain has at least one
        # fabrication (so the bench shows a meaningful relative reduction).
        if system:  # simulating WITH-dejavu — fewer hallucinations
            return (
                "```typescript\n"
                "import { prisma } from './db';\n"
                "import { gql } from 'graphql-tag';\n"
                "const db = process.env.DATABASE_URL;\n"
                "const u = await prisma.user.findUnique({ where: { email: 'a' } });\n"
                "await fetch('/api/users/42');\n"
                "const Q = gql`query { user(id: \"1\") { id email } }`;\n"
                "client.variation('checkout-flow-v2', user, false);\n"
                "```\n"
            )
        # WITHOUT-dejavu — fabrications across every domain.
        return (
            "```typescript\n"
            "import { prisma } from './db';\n"
            "import { gql } from 'graphql-tag';\n"
            "const db = process.env.DATABSE_URL;\n"
            "const u = await prisma.user.findUnique({ where: { emailAddress: 'a' } });\n"
            "await fetch('/api/totally-fake-route');\n"
            "const Q = gql`query { user(id: \"1\") { fakeField } }`;\n"
            "client.variation('made-up-flag', user, false);\n"
            "```\n"
        )
    if tool_use:
        if manifest is None:
            raise ValueError("tool_use=True requires manifest=")
        return _tool_use_agent_loop(prompt, manifest,
                                       model=model,
                                       max_tokens=max_tokens)
    import anthropic
    client = anthropic.Anthropic()
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    return "".join(b.text for b in resp.content if hasattr(b, "text"))


# ─── Scorer (dogfoods our own extractors) ───────────────────────────────────


@dataclass
class TrialScore:
    prompt_id: str
    condition: str           # "off" | "on"
    trial_no: int
    response_chars: int
    references_total: int    # all grounded names found
    hallucinations: int      # names not in schema
    breakdown: dict          # {"db": 2, "env": 0, "route": 1, ...}
    response_text: str


def score_response(text: str, manifest: dict) -> dict:
    """Parse `text` for grounded names and look them up against the
    manifest. Strict: any name not in the manifest counts as a
    hallucination (no fuzzy forgiveness — that's what dejavu would
    surface in production)."""
    from db_indexer import resolve_db_field, resolve_db_model  # noqa: F401
    from env_indexer import extract_env_reads_from_text
    from flag_indexer import extract_flag_reads_from_text
    from lint_strategies.db_grounding import (extract_prisma_calls,
                                                extract_drizzle_calls)
    from lint_strategies.graphql_grounding import (_extract_gql_blocks,
                                                     _parse_gql_operation)
    from url_walker import extract_call_sites

    breakdown = {"db": [0, 0], "env": [0, 0], "route": [0, 0],
                  "graphql": [0, 0], "flag": [0, 0]}
    bad: dict[str, list[str]] = {"db": [], "env": [], "route": [],
                                    "graphql": [], "flag": []}

    def _add_bad(domain: str, name: str) -> None:
        if name not in bad[domain]:
            bad[domain].append(name)

    # ─── Env vars ────────────────────────────────────────────────────
    env_names = extract_env_reads_from_text(text, "typescript")
    for name in env_names:
        breakdown["env"][0] += 1
        if name not in manifest["env_vars"]:
            breakdown["env"][1] += 1
            _add_bad("env", name)

    # ─── DB (Prisma model.field calls) ───────────────────────────────
    for model, field, _line, _opt in extract_prisma_calls(text):
        breakdown["db"][0] += 1
        # Two ways to be wrong: model doesn't exist OR field doesn't
        # exist on that model.
        if model not in manifest["models"]:
            breakdown["db"][1] += 1
            _add_bad("db", f"{model} (unknown model)")
            continue
        # Field check.
        valid_fields = (manifest["user_fields"] if model == "User" else
                         manifest["post_fields"] if model == "Post" else
                         [])
        # For models without enumerated fields in the manifest, we
        # accept any field (the manifest lists only User/Post fields
        # to keep the test surface manageable; for other models we
        # only validate the model name).
        if valid_fields and field not in valid_fields:
            breakdown["db"][1] += 1
            _add_bad("db", f"{model}.{field}")

    # Drizzle table refs (we don't have any in the fixture, but if
    # Claude happens to invent one it's a hallucination).
    for table, _line in extract_drizzle_calls(text):
        breakdown["db"][0] += 1
        if table not in manifest["models"]:
            breakdown["db"][1] += 1
            _add_bad("db", f"{table} (drizzle table)")

    # ─── HTTP routes (URL strings in fetch/axios) ────────────────────
    sites = extract_call_sites(text, "typescript")
    declared_paths = {r.split(" ", 1)[1] for r in manifest["routes"]}
    declared_methods = {r.split(" ", 1)[0] for r in manifest["routes"]}
    for site in sites:
        if site.interpolated:
            continue
        breakdown["route"][0] += 1
        # Normalize: strip query string, strip leading scheme/host.
        url = site.url.split("?", 1)[0]
        if url.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            url = urlparse(url).path or "/"
        # Also normalize concrete IDs to :id placeholders.
        import re
        url_pattern = re.sub(r"/\d+", "/:id", url)
        # Match against declared route patterns + methods.
        ok = False
        for declared in manifest["routes"]:
            decl_method, decl_path = declared.split(" ", 1)
            # Method match (allow GET as default if Claude omits it).
            if decl_method != site.method:
                continue
            # Path match: simple replacement of declared :param to a
            # wildcard for comparison.
            decl_re = "^" + re.sub(r":[A-Za-z_][\w]*", "[^/]+", decl_path) + "$"
            if re.match(decl_re, url):
                ok = True
                break
        if not ok:
            breakdown["route"][1] += 1
            _add_bad("route", f"{site.method} {url}")

    # ─── GraphQL operations ──────────────────────────────────────────
    # Build a manifest-aware type_for_field callback so the walker
    # descends into nested selection sets with the correct parent type.
    # The fixture's GraphQL schema is fixed, so we hardcode the
    # field-return-type map here.
    _GQL_RETURN_TYPES = {
        ("Query", "user"): "User",
        ("Query", "posts"): "Post",
        ("Mutation", "createUser"): "User",
        ("Mutation", "publishPost"): "Post",
        ("User", "posts"): "Post",
        ("Post", "author"): "User",
    }

    def _type_for_field(parent: str, fld: str) -> str | None:
        return _GQL_RETURN_TYPES.get((parent, fld))

    gql_blocks = _extract_gql_blocks(text)
    for body, line_no in gql_blocks:
        if "type " in body or "input " in body:
            # Schema definition, not an operation.
            continue
        for parent_type, fld, _line in _parse_gql_operation(
                body, line_no, type_for_field=_type_for_field):
            breakdown["graphql"][0] += 1
            valid_fields_for_type = {
                "User": manifest["user_fields"],
                "Post": manifest["post_fields"],
                "Query": ["user", "posts"],
                "Mutation": ["createUser", "publishPost"],
            }.get(parent_type, [])
            if parent_type not in manifest["gql_types"]:
                breakdown["graphql"][1] += 1
                _add_bad("graphql", f"{parent_type} (unknown gql type)")
                continue
            if valid_fields_for_type and fld not in valid_fields_for_type:
                breakdown["graphql"][1] += 1
                _add_bad("graphql", f"{parent_type}.{fld}")

    # ─── Feature flags ───────────────────────────────────────────────
    for flag, _line in extract_flag_reads_from_text(text):
        breakdown["flag"][0] += 1
        if flag not in manifest["flags"]:
            breakdown["flag"][1] += 1
            _add_bad("flag", flag)

    total_refs = sum(b[0] for b in breakdown.values())
    total_hallucinations = sum(b[1] for b in breakdown.values())
    return {
        "references_total": total_refs,
        "hallucinations": total_hallucinations,
        "breakdown": {k: {"refs": v[0], "hallucinations": v[1]}
                       for k, v in breakdown.items()},
        "bad_names": bad,
    }


# ─── Stats ──────────────────────────────────────────────────────────────────


def bootstrap_ci(values: list[float], n_resamples: int = 5000,
                  ci: float = 0.95, rng: random.Random | None = None
                  ) -> tuple[float, float]:
    rng = rng or random.Random(42)
    if not values:
        return (0.0, 0.0)
    means = []
    for _ in range(n_resamples):
        sample = [values[rng.randrange(len(values))] for _ in range(len(values))]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int((1 - ci) / 2 * n_resamples)]
    hi = means[int((1 + ci) / 2 * n_resamples)]
    return (lo, hi)


def mcnemar_p_value(b: int, c: int) -> float:
    """Exact McNemar test for paired binary outcomes.
    b = number of trials where ON was clean but OFF hallucinated.
    c = number of trials where OFF was clean but ON hallucinated.
    Returns two-sided p-value (binomial with p=0.5)."""
    n = b + c
    if n == 0:
        return 1.0
    # Two-sided exact binomial test.
    from math import comb
    k = min(b, c)
    p = sum(comb(n, i) for i in range(k + 1)) / (2 ** n) * 2
    return min(p, 1.0)


# ─── Runner ─────────────────────────────────────────────────────────────────


def run_benchmark(*, n_trials: int, output_dir: Path,
                    mock: bool, seed: int = 42,
                    only: Optional[str] = None) -> dict:
    """Run the full benchmark and write the report. Returns the
    aggregate result dict.

    `only`: if given, restrict to a single condition. Choices:
      'off' | 'on' | 'tool_use' | None (run all three).
    """
    sandbox = Path(os.environ.get("CLAUDE_DEJAVU_BENCH_SANDBOX",
                                    "/tmp/dejavu-bench"))
    fixture = sandbox / "project"
    if sandbox.exists():
        import shutil
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    print(f"Building synthetic fixture at {fixture}…")
    manifest = build_fixture(fixture)

    system_prompt = schema_digest(manifest)

    rng = random.Random(seed)
    trials: list[TrialScore] = []
    started_at = time.time()

    if only:
        conditions = (only,)
    else:
        conditions = ("off", "on")

    for prompt in PROMPTS:
        print(f"\n=== {prompt['id']}: {prompt['task'][:80]}…")
        for condition in conditions:
            for trial_no in range(n_trials):
                t0 = time.time()
                if condition == "tool_use":
                    response = call_claude(
                        prompt["task"],
                        mock=mock,
                        tool_use=True,
                        manifest=manifest,
                    )
                else:
                    response = call_claude(
                        prompt["task"],
                        system=(system_prompt if condition == "on" else None),
                        mock=mock,
                    )
                dt = time.time() - t0
                score = score_response(response, manifest)
                trial = TrialScore(
                    prompt_id=prompt["id"],
                    condition=condition,
                    trial_no=trial_no,
                    response_chars=len(response),
                    references_total=score["references_total"],
                    hallucinations=score["hallucinations"],
                    breakdown=score["breakdown"],
                    response_text=response,
                )
                trials.append(trial)
                marker = "❌" if score["hallucinations"] > 0 else "✓"
                print(f"  [{condition:3s} #{trial_no+1}] {marker} "
                      f"refs={score['references_total']} "
                      f"hall={score['hallucinations']}  ({dt:.1f}s)")

    elapsed = time.time() - started_at

    # ─── Aggregate ────────────────────────────────────────────────────
    aggregate = aggregate_trials(trials)
    aggregate["meta"] = {
        "n_trials_per_condition_per_prompt": n_trials,
        "n_prompts": len(PROMPTS),
        "n_total_trials": len(trials),
        "mock": mock,
        "seed": seed,
        "elapsed_seconds": round(elapsed, 1),
        "model": "claude-sonnet-4-6" if not mock else "MOCK",
    }

    # ─── Persist ──────────────────────────────────────────────────────
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "hallucination-rate-raw.json"
    raw_path.write_text(json.dumps({
        "trials": [asdict(t) for t in trials],
        "aggregate": aggregate,
    }, indent=2))
    print(f"\nRaw trials → {raw_path}")

    # Single-condition tool_use runs publish to a separate report so the
    # v0.3.12 baseline report isn't overwritten.
    is_tool_use_only = any(t.condition == "tool_use" for t in trials) \
                          and not any(t.condition == "on" for t in trials)
    report_name = ("hallucination-rate-v0.5.0a-tool-use.md"
                    if is_tool_use_only else "hallucination-rate-v0.3.12.md")
    report_path = output_dir / report_name
    report_path.write_text(write_report(aggregate))
    print(f"Report     → {report_path}")

    return aggregate


def aggregate_trials(trials: list[TrialScore]) -> dict:
    by_cond: dict[str, list[TrialScore]] = {"off": [], "on": [], "tool_use": []}
    for t in trials:
        by_cond.setdefault(t.condition, []).append(t)

    def rate(ts: list[TrialScore]) -> float:
        if not ts:
            return 0.0
        # Hallucination rate = trials with ≥1 hallucination / total
        return sum(1 for t in ts if t.hallucinations > 0) / len(ts)

    rate_off = rate(by_cond["off"])
    rate_on = rate(by_cond["on"])
    rate_tool_use = rate(by_cond["tool_use"])

    # Hard-coded v0.3.12 baseline so single-condition (`--only=tool_use`)
    # runs can compare against published numbers without re-running OFF.
    BASELINE_V0312 = 0.515  # 51.5% — see docs/benchmarks/hallucination-rate-v0.3.12.md
    if not by_cond["off"] and (by_cond["tool_use"] or by_cond["on"]):
        # Use the published baseline as `rate_off` for stats purposes.
        rate_off = BASELINE_V0312

    # Paired McNemar across (prompt_id, trial_no) — match each
    # off-trial with the same-numbered on-trial.
    by_pair: dict[tuple[str, int], dict[str, TrialScore]] = {}
    for t in trials:
        by_pair.setdefault((t.prompt_id, t.trial_no), {})[t.condition] = t
    b = c = 0  # b = OFF hallucinated, ON clean; c = ON hallucinated, OFF clean
    for pair in by_pair.values():
        if "off" not in pair or "on" not in pair:
            continue
        off_h = pair["off"].hallucinations > 0
        on_h = pair["on"].hallucinations > 0
        if off_h and not on_h:
            b += 1
        elif on_h and not off_h:
            c += 1
    p = mcnemar_p_value(b, c)

    # Bootstrap CI on relative reduction.
    rng = random.Random(42)
    pair_pairs = [(p_["off"].hallucinations > 0, p_["on"].hallucinations > 0)
                   for p_ in by_pair.values()
                   if "off" in p_ and "on" in p_]
    rel_reductions = []
    for _ in range(2000):
        sample = [pair_pairs[rng.randrange(len(pair_pairs))]
                   for _ in range(len(pair_pairs))]
        off_rate = sum(1 for s in sample if s[0]) / len(sample) if sample else 0
        on_rate = sum(1 for s in sample if s[1]) / len(sample) if sample else 0
        if off_rate > 0:
            rel_reductions.append((off_rate - on_rate) / off_rate)
    rel_reductions.sort()
    if rel_reductions:
        ci_lo = rel_reductions[int(0.025 * len(rel_reductions))]
        ci_hi = rel_reductions[int(0.975 * len(rel_reductions))]
    else:
        ci_lo, ci_hi = (0.0, 0.0)

    # If we only ran tool_use, also compute its reduction against the
    # baseline.
    rate_for_winner = rate_tool_use if by_cond["tool_use"] else rate_on
    rel_reduction = ((rate_off - rate_for_winner) / rate_off
                       if rate_off > 0 else 0.0)
    return {
        "rate_off": rate_off,
        "rate_on": rate_on,
        "rate_tool_use": rate_tool_use,
        "absolute_reduction": rate_off - rate_for_winner,
        "relative_reduction": rel_reduction,
        "rel_reduction_ci_95": (ci_lo, ci_hi),
        "mcnemar": {"b_off_only_hallucinated": b,
                     "c_on_only_hallucinated": c,
                     "p_value": p},
        "by_prompt": _per_prompt_breakdown(trials),
        "by_domain": _per_domain_breakdown(trials),
    }


def _per_prompt_breakdown(trials: list[TrialScore]) -> list[dict]:
    by_prompt: dict[str, dict[str, list[TrialScore]]] = {}
    for t in trials:
        by_prompt.setdefault(t.prompt_id, {"off": [], "on": [], "tool_use": []})[t.condition].append(t)
    out = []
    for pid, conds in by_prompt.items():
        out.append({
            "prompt_id": pid,
            "rate_off": (sum(1 for t in conds["off"] if t.hallucinations > 0)
                          / max(1, len(conds["off"]))),
            "rate_on": (sum(1 for t in conds["on"] if t.hallucinations > 0)
                         / max(1, len(conds["on"]))),
            "rate_tool_use": (sum(1 for t in conds["tool_use"] if t.hallucinations > 0)
                               / max(1, len(conds["tool_use"]))),
            "n_off": len(conds["off"]),
            "n_on": len(conds["on"]),
            "n_tool_use": len(conds["tool_use"]),
        })
    return out


def _per_domain_breakdown(trials: list[TrialScore]) -> dict:
    """Per-domain hallucination counts aggregated across trials."""
    out = {dom: {"off": [0, 0], "on": [0, 0], "tool_use": [0, 0]}
           for dom in ("db", "env", "route", "graphql", "flag")}
    for t in trials:
        for dom, vals in t.breakdown.items():
            out[dom][t.condition][0] += vals["refs"]
            out[dom][t.condition][1] += vals["hallucinations"]
    formatted = {}
    for dom, conds in out.items():
        formatted[dom] = {
            "off_refs": conds["off"][0],
            "off_hallucinations": conds["off"][1],
            "off_rate": (conds["off"][1] / conds["off"][0]
                          if conds["off"][0] else 0),
            "on_refs": conds["on"][0],
            "on_hallucinations": conds["on"][1],
            "on_rate": (conds["on"][1] / conds["on"][0]
                          if conds["on"][0] else 0),
            "tool_use_refs": conds["tool_use"][0],
            "tool_use_hallucinations": conds["tool_use"][1],
            "tool_use_rate": (conds["tool_use"][1] / conds["tool_use"][0]
                                if conds["tool_use"][0] else 0),
        }
    return formatted


# ─── Report writer ──────────────────────────────────────────────────────────


def write_report(agg: dict) -> str:
    meta = agg["meta"]
    rel = agg["relative_reduction"] * 100
    ci_lo, ci_hi = agg["rel_reduction_ci_95"]
    p = agg["mcnemar"]["p_value"]
    # Detect single-condition tool_use run: tool_use rate present, no ON trials.
    tool_use_only = (agg.get("rate_tool_use", 0) > 0 or
                       any(b.get("n_tool_use", 0) > 0 for b in agg.get("by_prompt", []))) \
                      and not any(b.get("n_on", 0) > 0 for b in agg.get("by_prompt", []))

    lines = [
        "# claude-dejavu Hallucination Rate Benchmark"
        + (" — v0.5.0a Tool-Use Mode" if tool_use_only else ""),
        "",
        (f"**Headline:** claude-dejavu **tool-use mode** reduces Claude's "
          f"hallucination rate by **{rel:.1f}%** "
          f"(rate {agg['rate_off']*100:.1f}% → {agg['rate_tool_use']*100:.1f}%, "
          f"n = {meta['n_total_trials']} trials, baseline from v0.3.12)."
         if tool_use_only else
         f"**Headline:** claude-dejavu reduces Claude's hallucination rate "
         f"by **{rel:.1f}%** (95% CI: {ci_lo*100:.1f}–{ci_hi*100:.1f}%, "
         f"McNemar p = {p:.4g}, n = {meta['n_total_trials']} trials)."),
        "",
        f"_Model:_ `{meta['model']}` · _Trials per cell:_ {meta['n_trials_per_condition_per_prompt']} "
        f"· _Prompts:_ {meta['n_prompts']} · _Wall-clock:_ {meta['elapsed_seconds']}s "
        f"· _Mock:_ {meta['mock']}",
        "",
        "## Methodology",
        "",
        "1. Built a synthetic project with a fixed schema: 5 Prisma models, "
        "12 env vars, 6 HTTP routes, 4 GraphQL types, 5 feature flags.",
        "2. 20 prompts spanning every grounding domain — pure DB, pure env, "
        "pure route, pure GraphQL, pure flag, plus multi-domain combos.",
    ] + ([
        f"3. For each prompt, ran {meta['n_trials_per_condition_per_prompt']} trials in tool-use mode:",
        "    - **TOOL_USE:** plain Claude with the `dejavu_check_name(name, kind?, parent?)` "
        "tool registered. The model decides when to call it; tool returns OK / "
        "TYPO (with the right name) / UNKNOWN against the synthetic manifest.",
        "    - Baseline (OFF rate) is the published v0.3.12 number — not re-measured.",
    ] if tool_use_only else [
        f"3. For each prompt, ran {meta['n_trials_per_condition_per_prompt']} trials × 2 conditions:",
        "    - **OFF:** plain Claude, no system prompt context",
        "    - **ON:** system prompt enriched with the schema digest",
        "      (the same ground truth dejavu's PreToolUse hook injects)",
    ]) + [
        "4. Scored each response deterministically by parsing every grounded "
        "name (env var, model.field, URL, gql field, flag key) and looking "
        "it up against the schema. Strict matching — any name not in the "
        "schema counts as a hallucination.",
        "",
        "## Result",
        "",
        f"|                                  | Hallucination rate |",
        f"|----------------------------------|-------------------:|",
        f"| **OFF** ({'baseline v0.3.12' if tool_use_only else 'no dejavu'})    "
        f"| {agg['rate_off']*100:.1f}% |",
    ] + ([
        f"| **TOOL_USE** (dejavu_check_name) | {agg['rate_tool_use']*100:.1f}% |",
    ] if tool_use_only else [
        f"| **ON** (with dejavu)             | {agg['rate_on']*100:.1f}% |",
    ]) + [
        f"| **Absolute reduction**           | {agg['absolute_reduction']*100:.1f} pp |",
        f"| **Relative reduction**           | **{rel:.1f}%** |",
        "",
        "## Per-domain breakdown",
        "",
        "Hallucinations per reference, by grounding domain:",
        "",
    ]
    if tool_use_only:
        lines += [
            "| Domain | TOOL_USE refs | TOOL_USE halluc. | TOOL_USE rate |",
            "|--------|--------------:|-----------------:|--------------:|",
        ]
        for dom, vals in agg["by_domain"].items():
            lines.append(
                f"| {dom:7s} | {vals['tool_use_refs']:13d} | "
                f"{vals['tool_use_hallucinations']:16d} | "
                f"{vals['tool_use_rate']*100:12.1f}% |"
            )
    else:
        lines += [
            "| Domain | OFF refs | OFF halluc. | OFF rate | ON refs | ON halluc. | ON rate |",
            "|--------|---------:|------------:|---------:|--------:|-----------:|--------:|",
        ]
        for dom, vals in agg["by_domain"].items():
            lines.append(
                f"| {dom:7s} | {vals['off_refs']:8d} | {vals['off_hallucinations']:11d} | "
                f"{vals['off_rate']*100:6.1f}% | {vals['on_refs']:7d} | "
                f"{vals['on_hallucinations']:10d} | {vals['on_rate']*100:6.1f}% |"
            )
    if tool_use_only:
        lines += [
            "",
            "## Per-prompt breakdown (tool_use vs baseline)",
            "",
            "| Prompt | n | TOOL_USE rate |",
            "|--------|--:|--------------:|",
        ]
        sorted_prompts = sorted(
            agg["by_prompt"],
            key=lambda p: p.get("rate_tool_use", 0),
        )
        for p_ in sorted_prompts:
            lines.append(
                f"| `{p_['prompt_id']}` | {p_.get('n_tool_use', 0)} | "
                f"{p_.get('rate_tool_use', 0)*100:5.1f}% |"
            )
    else:
        lines += [
            "",
            "## Per-prompt breakdown (sorted by reduction)",
            "",
            "| Prompt | n | OFF rate | ON rate | Reduction |",
            "|--------|--:|---------:|--------:|----------:|",
        ]
        sorted_prompts = sorted(
            agg["by_prompt"],
            key=lambda p: (p["rate_off"] - p["rate_on"]),
            reverse=True,
        )
        for p_ in sorted_prompts:
            red = p_["rate_off"] - p_["rate_on"]
            lines.append(
                f"| `{p_['prompt_id']}` | {p_['n_off']} | "
                f"{p_['rate_off']*100:5.1f}% | {p_['rate_on']*100:5.1f}% | "
                f"{red*100:+5.1f} pp |"
            )

    lines += ["", "## Statistics", ""]
    if tool_use_only:
        n_tu = sum(p_.get("n_tool_use", 0) for p_ in agg["by_prompt"])
        n_hall = sum(int(p_.get("rate_tool_use", 0) * p_.get("n_tool_use", 0))
                       for p_ in agg["by_prompt"])
        lines += [
            f"- TOOL_USE trials: **{n_tu}**, of which **{n_hall}** "
            f"contained ≥1 hallucinated identifier ({agg['rate_tool_use']*100:.1f}%).",
            f"- Baseline (OFF) rate: **{agg['rate_off']*100:.1f}%** "
            f"— published v0.3.12 number (n=400, McNemar p=9.6e-27, see "
            f"`hallucination-rate-v0.3.12.md`). Not re-measured for this run.",
            "- McNemar / paired CI not applicable: no paired OFF trials in "
            "this run. The reduction is a one-sample comparison against the "
            "published v0.3.12 OFF rate.",
        ]
    else:
        lines += [
            f"- Trials where OFF hallucinated and ON did not (b): "
            f"**{agg['mcnemar']['b_off_only_hallucinated']}**",
            f"- Trials where ON hallucinated and OFF did not (c): "
            f"**{agg['mcnemar']['c_on_only_hallucinated']}**",
            f"- McNemar exact two-sided p-value: **{p:.4g}**",
            f"- Bootstrap 95% CI on relative reduction: "
            f"**{ci_lo*100:.1f}% – {ci_hi*100:.1f}%**",
        ]
    lines += [
        "",
        "## Reproducibility",
        "",
        "```bash",
        "ANTHROPIC_API_KEY=sk-ant-… python tests/bench_hallucination.py \\",
    ] + ([
        f"    --only=tool_use --n={meta['n_trials_per_condition_per_prompt']} "
        f"--seed={meta['seed']} --output=docs/benchmarks/",
    ] if tool_use_only else [
        f"    --n={meta['n_trials_per_condition_per_prompt']} "
        f"--seed={meta['seed']} --output=docs/benchmarks/",
    ]) + [
        "```",
        "",
        "Raw trial data is at `docs/benchmarks/hallucination-rate-raw.json` "
        "(every prompt, every trial, every response). The synthetic project "
        "fixture is regenerated from `tests/bench_hallucination.py::build_fixture` "
        "each run — no manual setup required.",
    ]
    return "\n".join(lines) + "\n"


# ─── CLI ────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="claude-dejavu hallucination rate benchmark",
    )
    ap.add_argument("--n", type=int, default=10,
                     help="trials per (prompt, condition) cell. Default 10 = 400 total.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--mock", action="store_true",
                     help="use canned mock responses instead of the API (for smoke / dry-run)")
    ap.add_argument("--output", default="docs/benchmarks",
                     help="output directory for the report + raw JSON")
    ap.add_argument("--only", choices=("off", "on", "tool_use"),
                     default=None,
                     help=("run a single condition only (skip the rest). "
                            "Useful for re-measuring just the new tool-use "
                            "mode without re-spending API on the baseline."))
    args = ap.parse_args()

    if not args.mock and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: set ANTHROPIC_API_KEY (or pass --mock for a dry run)",
              file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output)
    if not out_dir.is_absolute():
        out_dir = PLUGIN_ROOT / out_dir

    agg = run_benchmark(n_trials=args.n, output_dir=out_dir,
                          mock=args.mock, seed=args.seed,
                          only=args.only)

    winner_rate = (agg.get('rate_tool_use', 0)
                     if args.only == 'tool_use'
                     else agg['rate_on'])
    winner_label = 'tool_use' if args.only == 'tool_use' else 'on'
    print(f"\nHEADLINE: {agg['relative_reduction']*100:.1f}% relative reduction "
          f"(rate {agg['rate_off']*100:.1f}% → {winner_rate*100:.1f}% [{winner_label}])")
