# Security Policy

## Reporting a vulnerability

**Please do NOT open a public GitHub issue for security vulnerabilities.**

Instead, email **dejavu@hendrikthurau.enterprises** with:

- A description of the vulnerability
- Steps to reproduce
- The version affected (`claude-dejavu --version`)
- Any proof-of-concept code (encrypted attachments accepted; PGP key on request)

If GitHub Security Advisories are enabled on the repo, you can also use the
"Report a vulnerability" button on the [Security tab](https://github.com/HthSolid/claude-dejavu/security/advisories/new) — that creates a private discussion with the maintainers.

## Response commitment

| Severity | Initial response | Patch target |
|---|---|---|
| Critical (RCE, data exfiltration, privilege escalation) | within 24 hours | within 7 days |
| High (auth bypass, sensitive data exposure) | within 48 hours | within 14 days |
| Medium (DoS, info disclosure) | within 5 days | within 30 days |
| Low (hardening opportunities) | within 14 days | best-effort |

These targets assume a coordinated reporter who responds promptly to clarifying questions. We'll keep you informed throughout.

## Coordinated disclosure

We follow standard coordinated disclosure:

1. You report privately
2. We acknowledge within the timeframes above
3. We work on a fix; we keep you informed
4. We agree on a public disclosure date — typically 30 to 90 days from initial report depending on severity and complexity
5. We credit you in the release notes (unless you prefer to remain anonymous)

If a vulnerability is being actively exploited in the wild, we'll move
faster and may publish a patched release before the full advisory is
ready.

## In scope

Anything in this repository's code, including:

- The plugin source (`code/`, `mcp/`, `hooks/`, `scripts/`, `bin/`)
- The Docker compose configurations (`docker/`)
- The MCP server's exposed tools
- The pre-write linter's intervention modes
- The runtime trap generator output

Things we particularly care about:

- Arbitrary code execution on a user's machine (via plugin hook, MCP tool, or installer)
- Reading or writing files outside the user's project root without consent
- Outbound network requests to unauthorized hosts
- Leaking secrets from the Postgres / Weaviate stores
- Path-traversal in restore / backup paths
- SQL injection in any of the indexer or search code paths

## Out of scope

- Vulnerabilities in upstream dependencies (Postgres, Weaviate, transformer models, Python stdlib) — please report those upstream
- Theoretical attacks without a proof-of-concept
- Issues requiring physical access to the user's machine or root on the host
- Social engineering of HTE staff or users
- Vulnerabilities in older releases that have been superseded by a patched version

## Hall of fame

Reporters who responsibly disclose are credited here with their permission.

(Empty — be the first.)

## Commercial-license vulnerabilities

If you're using claude-dejavu under a commercial license and you find a vulnerability that affects the licensed surface area, the same email applies — `dejavu@hendrikthurau.enterprises` — and your support contract terms apply for response time.
