"""SDK env-convention auto-scraper.

For each package listed (or every package in the existing SDK catalog),
fetch its README and TypeScript declarations from the npm registry and
extract any `process.env.X`, `os.environ['X']`, or `os.getenv('X')`
references. Outputs proposed additions to sdk_env_catalog.json for
human review — never auto-merges.

Why scraped: SDKs evolve. New env vars get added. A scraper run
quarterly keeps the catalog fresh without bottlenecking on the
maintainer manually tracking every SDK release.

Usage:

  # Scrape all packages currently in the catalog (registry + unpkg)
  python scripts/scrape_sdk_envs.py

  # Specific packages
  python scripts/scrape_sdk_envs.py @launchdarkly/node-server-sdk

  # GitHub source-tree crawl — finds env vars documented in source
  # code, not just READMEs. Set GITHUB_TOKEN for higher rate limits;
  # unauthenticated GitHub code-search returns empty for most repos.
  GITHUB_TOKEN=ghp_... python scripts/scrape_sdk_envs.py \\
      @launchdarkly/node-server-sdk --source=github

  # Both sources
  python scripts/scrape_sdk_envs.py --source=both

  # Apply (write back to catalog). Default is dry run.
  python scripts/scrape_sdk_envs.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "code" / "sdk_env_catalog.json"

ENV_REF_RE = re.compile(
    r"(?:process\.env\.|os\.environ\[['\"]?|os\.getenv\(['\"])"
    r"([A-Z_][A-Z0-9_]+)['\"]?\)?")
NPM_REGISTRY = "https://registry.npmjs.org"
UNPKG = "https://unpkg.com"
UA = "claude-dejavu-sdk-scraper/0.1 (+https://github.com/HthSolid/claude-dejavu)"

# Common env-var noise that scrapers pick up but isn't really an SDK
# convention — exclude.
NOISE_NAMES = {"NODE_ENV", "DEBUG", "PATH", "HOME", "USER", "PWD",
                 "TZ", "LANG", "TERM", "HOSTNAME"}


def _fetch(url: str, timeout: int = 10) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None


def _registry_meta(pkg: str) -> Optional[dict]:
    """Fetch package metadata from npm registry."""
    text = _fetch(f"{NPM_REGISTRY}/{pkg.replace('/', '%2F')}")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _readme_for(pkg: str) -> Optional[str]:
    """Try README from registry metadata first, fall back to unpkg."""
    meta = _registry_meta(pkg)
    if meta and isinstance(meta.get("readme"), str):
        return meta["readme"]
    # unpkg serves the published README.md if it exists.
    for fname in ("README.md", "readme.md", "README.markdown"):
        text = _fetch(f"{UNPKG}/{pkg}/{fname}")
        if text:
            return text
    return None


def _types_dts_for(pkg: str) -> Optional[str]:
    """Try to fetch index.d.ts (or whatever `types` points at) from
    unpkg. Many SDKs document env vars in the .d.ts JSDoc comments."""
    meta = _registry_meta(pkg)
    types_path = "index.d.ts"
    if meta and isinstance(meta.get("versions"), dict):
        # Look at the latest version's package.json to find types.
        latest = (meta.get("dist-tags", {}).get("latest")
                    or list(meta["versions"].keys())[-1])
        latest_pkg = meta["versions"].get(latest, {})
        types_path = (latest_pkg.get("types") or latest_pkg.get("typings")
                       or "index.d.ts")
    return _fetch(f"{UNPKG}/{pkg}/{types_path}")


def extract_env_names(text: str) -> set[str]:
    """Pull every `process.env.X` / `os.environ[X]` from text."""
    return {m.group(1) for m in ENV_REF_RE.finditer(text)
              if m.group(1) not in NOISE_NAMES}


def _github_repo_for(pkg: str) -> Optional[str]:
    """Resolve the GitHub repo URL for an npm package via its
    registry metadata's `repository` field. Returns 'owner/repo' or
    None if no GitHub repo is declared."""
    meta = _registry_meta(pkg)
    if not meta:
        return None
    # Look at the latest version's repository field.
    versions = meta.get("versions") or {}
    latest = (meta.get("dist-tags", {}).get("latest")
                or (list(versions.keys())[-1] if versions else None))
    repo_info = ((versions.get(latest, {}) if latest else {})
                    .get("repository")
                    or meta.get("repository"))
    if not repo_info:
        return None
    url = (repo_info.get("url") if isinstance(repo_info, dict)
             else str(repo_info))
    if not url:
        return None
    # Normalize forms like:
    #   git+https://github.com/owner/repo.git
    #   git://github.com/owner/repo.git
    #   https://github.com/owner/repo
    m = re.search(r"github\.com[:/]+([^/]+/[^/.#]+)", url)
    return m.group(1) if m else None


def _github_search_code(repo: str, *, max_files: int = 30
                          ) -> set[str]:
    """Use GitHub's code-search API to find files containing
    `process.env.`/`os.environ[`/`os.getenv(` references in the
    repo, then fetch each candidate's raw text and extract names.

    Unauthenticated GitHub API allows ~10 req/min. Pass
    GITHUB_TOKEN env var for higher limits."""
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"User-Agent": UA,
                 "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Search for any of the three env-access patterns
    queries = [
        f"repo:{repo}+%22process.env.%22+language:TypeScript",
        f"repo:{repo}+%22os.environ%5B%22+language:Python",
        f"repo:{repo}+%22os.getenv%28%22+language:Python",
    ]
    found_files: list[str] = []
    for q in queries:
        url = f"https://api.github.com/search/code?q={q}&per_page=10"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8",
                                                       errors="ignore"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                  TimeoutError, json.JSONDecodeError):
            continue
        for item in (data.get("items") or [])[:max_files // 3]:
            raw = item.get("html_url", "").replace(
                "github.com", "raw.githubusercontent.com").replace(
                "/blob/", "/")
            if raw:
                found_files.append(raw)
    # Fetch each file's raw content and extract env names.
    names: set[str] = set()
    for url in found_files[:max_files]:
        text = _fetch(url, timeout=10)
        if text:
            names |= extract_env_names(text)
    return names


def scrape_package(pkg: str, *, source: str = "registry"
                     ) -> set[str]:
    """Fetch env names for `pkg`.

    source: "registry" (default — README + .d.ts via npm/unpkg) or
            "github"   (heavier — GitHub code-search across the
                          package's repo source). Github mode finds
                          conventions documented in source code, not
                          just READMEs.
    """
    found: set[str] = set()
    if source in ("registry", "both"):
        readme = _readme_for(pkg)
        if readme:
            found |= extract_env_names(readme)
        dts = _types_dts_for(pkg)
        if dts:
            found |= extract_env_names(dts)
    if source in ("github", "both"):
        repo = _github_repo_for(pkg)
        if repo:
            found |= _github_search_code(repo)
    return found


def diff_proposal(catalog: dict, scraped: dict[str, set[str]]
                    ) -> dict[str, list[str]]:
    """Compare scraped names to existing catalog. Return only new
    additions (the scraper never proposes deletions)."""
    proposal: dict[str, list[str]] = {}
    existing = catalog.get("env", {})
    for pkg, names in scraped.items():
        existing_set = set(existing.get(pkg, []))
        new = sorted(names - existing_set)
        if new:
            proposal[pkg] = new
    return proposal


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("packages", nargs="*",
                     help="Specific packages to scrape (default: all "
                            "packages in the catalog).")
    ap.add_argument("--apply", action="store_true",
                     help="Merge the proposal into sdk_env_catalog.json. "
                            "Default: dry run, prints proposal as JSON.")
    ap.add_argument("--source",
                     choices=("registry", "github", "both"),
                     default="registry",
                     help="Where to look. 'registry' = README + .d.ts "
                            "via npm/unpkg (fast, conservative). 'github' "
                            "= code-search across the package's repo "
                            "(heavier, finds conventions documented in "
                            "source code, not just READMEs). 'both' "
                            "merges results from both sources. Set "
                            "GITHUB_TOKEN env var for higher rate limits.")
    args = ap.parse_args()

    catalog = json.loads(CATALOG_PATH.read_text())
    if args.packages:
        targets = args.packages
    else:
        targets = [p for p in catalog.get("env", {})
                     if not p.startswith("_")]

    scraped: dict[str, set[str]] = {}
    for pkg in targets:
        sys.stderr.write(f"[scrape:{args.source}] {pkg}... ")
        sys.stderr.flush()
        names = scrape_package(pkg, source=args.source)
        scraped[pkg] = names
        sys.stderr.write(f"found {len(names)} env names\n")

    proposal = diff_proposal(catalog, scraped)

    if not proposal:
        print("No new SDK env names found.")
        return 0

    if args.apply:
        for pkg, new_names in proposal.items():
            existing = list(catalog["env"].get(pkg, []))
            catalog["env"][pkg] = sorted(set(existing) | set(new_names))
        CATALOG_PATH.write_text(json.dumps(catalog, indent=2) + "\n")
        print(f"Merged {sum(len(v) for v in proposal.values())} new "
                f"names across {len(proposal)} packages into "
                f"{CATALOG_PATH.name}.")
    else:
        print(json.dumps(proposal, indent=2))
        print(f"\nDry run. Re-run with --apply to merge.",
                file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
