# Contributing to claude-dejavu

Thanks for considering a contribution. This is a small project and every issue and PR gets read.

## Before opening an issue

- **Bug?** Use the bug report template. Include OS, Python version, Docker version, and the failing command + output. The `claude-dejavu doctor` output is gold.
- **Feature?** Use the feature request template, or start a [Discussion](https://github.com/HthSolid/claude-dejavu/discussions) first. We're more likely to merge work we've discussed.
- **Question?** Use [Discussions](https://github.com/HthSolid/claude-dejavu/discussions), not Issues.
- **Security vulnerability?** **Do NOT open a public issue.** See [SECURITY.md](SECURITY.md).
- **Commercial licensing?** See [COMMERCIAL.md](COMMERCIAL.md) — email `dejavu@hendrikthurau.enterprises`.

## Development setup

```bash
git clone https://github.com/HthSolid/claude-dejavu.git
cd claude-dejavu

# Bring up Postgres + Weaviate + transformers (Docker required)
docker compose -f docker/docker-compose.minimal.yml up -d

# Install via the standard installer (sets up venv, applies schema, etc.)
python3 scripts/install.py

# Run tests
python3 tests/run_all.py    # infra-free assertions (~1055 tests as of v0.8.8)
python3 tests/smoke.py       # full smoke (~175 steps, requires PG + Weaviate)

# Install the pre-push hook (one-time per clone)
./scripts/install-hooks.sh
```

See [docs/INSTALL.md](docs/INSTALL.md) for the full installer flow.

## Local CI / pre-push gate

After running `./scripts/install-hooks.sh`, every `git push` first
runs `./scripts/local-ci.sh fast` and refuses the push if it fails.
The fast pass (`tests/run_all.py` + `compileall`) takes ~20s; the
full pass (`HTE_FULL_LOCAL_CI=1 git push`) adds the bridge-parity
probe on top, ~21s. Emergency bypass: `HTE_SKIP_LOCAL_CI=1 git push`
(logged to stderr).

Why it exists: silent-failure bugs (e.g. a method called with the
wrong kwarg that gets caught by `try/except: pass`, then unit tests
mock that path and don't notice) shipped twice during the v0.8.7-
v0.8.8 release pair. The local-CI gate plus the `tests/test_bridge_
signature_parity.py` regression net catches that whole bug class
before the push lands on `origin/main`.

## Pull request guidelines

- **One logical change per PR.** Bug fix in PR A, refactor in PR B. Easier to review, easier to revert.
- **Add a test.** Even small. Tests live in `tests/` organized by module.
- **Update `CHANGELOG.md`** if your change is user-visible.
- **Match existing code style.** No global reformatters in PRs that fix bugs.
- **Don't bump version numbers in PRs** — release versioning is centralized.
- **Don't add unrelated cleanup** alongside a feature ("while I was here, I also...").

## Licensing of contributions

claude-dejavu is licensed under the **Functional Source License, Version 1.1, ALv2 Future License (FSL-1.1-ALv2)**.

**By submitting a pull request, you agree that your contribution is licensed to HTE Switzerland under FSL-1.1-ALv2 on the same terms as the rest of the project**, including the eventual conversion to Apache 2.0 two years after each release.

This is a lightweight in-project CLA. If your employer requires a separate signed CLA before you can contribute, email `dejavu@hendrikthurau.enterprises` and we'll work it out.

## What we don't accept

- PRs that add telemetry, phone-home, or data collection of any kind
- PRs that introduce a non-FSL-compatible runtime dependency without prior discussion
- PRs that bundle unrelated changes
- AI-generated PRs submitted without human review (we'll spot the patterns and close them)
- PRs that delete or modify benchmark data after the fact

## Things we love

- Reproduction cases for bugs submitted as test fixtures
- Performance benchmarks alongside performance claims
- Documentation improvements (`docs/`, slash command help text, MCP tool descriptions)
- New grounding domains for languages or frameworks we haven't covered
- Real-world projects that exposed an edge case in our scoring or rewriter

## Project values

- **Lossless by default.** We don't summarize away information that the user might need later.
- **Off-tree by default.** Critical state never lives inside the user's project repo.
- **Cross-platform from day one.** If it doesn't work on Linux, macOS, and Windows, it doesn't ship.
- **Honest numbers.** If a benchmark isn't reproducible, it doesn't go in the README.

## Code of conduct

Be civil. Disagree on the technical merits. Don't punch down. We don't ship a separate Code of Conduct file but the spirit of the [Contributor Covenant](https://www.contributor-covenant.org/) applies — sustained bad-faith behavior gets you blocked.
