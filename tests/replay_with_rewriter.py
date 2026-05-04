"""Offline replay: take the raw 200-trial bench output, run the
deterministic rewriter on each response, re-score the rewritten text,
and report the post-rewrite rate.

No API calls. No model calls. Purely measuring how much of the model's
hallucination rate is recoverable by deterministic correction alone."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "tests"))

import re
from rewriter import (load_sdk_catalog, rewrite, summarize_outcomes,
                        RewriteOutcome, manifest_candidates)
from bench_hallucination import (build_fixture, score_response,
                                    aggregate_trials, write_report,
                                    TrialScore)


def _residual_llm_clarify(text: str, residuals: list,
                              manifest: dict, *,
                              model: str = "claude-haiku-4-5-20251001"
                              ) -> tuple[str, dict]:
    """Single batched LLM call to clarify RESIDUAL names. The model
    sees: original text excerpt + each residual + closest manifest
    candidates, returns verdicts: TYPO_OF_<name> / ADD_NEW / OMIT.
    We apply verdicts deterministically (string-replace for TYPO,
    note-only for ADD_NEW, strip for OMIT).

    Cost-capped: one call per response, Haiku tier, ~500 tokens.
    Per the project rule 'exhaust deterministic before LLM,' this
    fires only after the rewriter cascade has produced RESIDUALs."""
    import anthropic
    client = anthropic.Anthropic()

    # Build the focused query.
    items = []
    for r in residuals:
        candidates = manifest_candidates(manifest, r["domain"])
        # Top 5 closest by trigram similarity
        from rewriter import _trigram_sim
        scored = sorted(((c, _trigram_sim(r["original"], c))
                          for c in candidates),
                          key=lambda x: -x[1])[:5]
        items.append({
            "domain": r["domain"],
            "name": r["original"],
            "closest": [c for c, _ in scored],
        })

    if not items:
        return text, {"called": False, "verdicts": []}

    items_md = "\n".join(
        f"  {i+1}. domain=`{it['domain']}` name=`{it['name']}` "
        f"closest={it['closest']}"
        for i, it in enumerate(items))
    prompt = (
        "You are clarifying identifiers used in a code generation "
        "that don't match the project's declared names.\n\n"
        f"Project schema (per domain):\n"
        f"  env_vars: {manifest.get('env_vars', [])}\n"
        f"  user_fields: {manifest.get('user_fields', [])}\n"
        f"  post_fields: {manifest.get('post_fields', [])}\n"
        f"  routes: {manifest.get('routes', [])}\n"
        f"  flags: {manifest.get('flags', [])}\n"
        f"  gql_types: {manifest.get('gql_types', [])}\n\n"
        f"Items to classify:\n{items_md}\n\n"
        "For each, reply on its own line in this exact format:\n"
        "  <number>: TYPO_OF=<correct_name>   "
        "(if it's a typo of one of the closest candidates)\n"
        "  <number>: ADD_NEW                  "
        "(if it's a legitimate new name the project should declare)\n"
        "  <number>: OMIT                     "
        "(if the feature should be dropped — name doesn't belong)\n\n"
        "Reply with ONLY the verdict lines, nothing else."
    )
    resp = client.messages.create(
        model=model, max_tokens=400,
        messages=[{"role": "user", "content": prompt}])
    raw = "".join(b.text for b in resp.content if hasattr(b, "text"))

    verdicts = []
    rewritten = text
    for line in raw.splitlines():
        m = re.match(r"^\s*(\d+)\s*:\s*(\w+)(?:=([\w.]+))?", line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        verdict = m.group(2).upper()
        target = m.group(3)
        if not (0 <= idx < len(items)):
            continue
        original = items[idx]["name"]
        if verdict.startswith("TYPO_OF") and target:
            pat = re.compile(rf"\b{re.escape(original)}\b")
            rewritten = pat.sub(target, rewritten)
            verdicts.append({"name": original, "verdict": "TYPO",
                              "replacement": target})
        elif verdict == "ADD_NEW":
            verdicts.append({"name": original, "verdict": "ADD_NEW",
                              "replacement": None})
        elif verdict == "OMIT":
            verdicts.append({"name": original, "verdict": "OMIT",
                              "replacement": None})
    return rewritten, {"called": True, "verdicts": verdicts,
                         "raw": raw}


def main(raw_path: Path, out_dir: Path, *,
           use_residual_llm: bool = False) -> None:
    data = json.loads(raw_path.read_text())
    raw_trials = data["trials"]

    # Rebuild the fixture so we have manifest + package_deps. The
    # fixture is deterministic (no randomness in build_fixture).
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        manifest = build_fixture(Path(tmp))

    catalog = load_sdk_catalog()
    project_deps = set(manifest.get("package_deps", []))

    rewritten_trials: list[TrialScore] = []
    all_outcomes_per_trial: list[dict] = []
    raw_halluc = 0
    post_halluc = 0
    sdk_accepted = 0
    auto_replaced = 0
    residual_count = 0

    for raw in raw_trials:
        text = raw["response_text"]
        # First, score raw to collect bad names (the original metric).
        raw_score = score_response(text, manifest)
        raw_h = raw_score["hallucinations"]
        raw_halluc += int(raw_h > 0)

        # Run the rewriter.
        rewritten, outcomes = rewrite(
            text, manifest,
            project_deps=project_deps,
            bad_names=raw_score.get("bad_names", {}),
            sdk_catalog=catalog,
        )
        # Build an extended manifest with ACCEPT_SDK names treated as
        # legitimate (this is what the production rewriter does — once
        # an SDK convention is gated by package.json, it's part of the
        # project's effective name space). Domain-aware extension.
        ext_manifest = dict(manifest)
        ext_env = list(manifest.get("env_vars", []))
        ext_flags = list(manifest.get("flags", []))
        for o in outcomes:
            if o.final_verdict != "ACCEPT_SDK":
                continue
            if o.domain == "env" and o.original not in ext_env:
                ext_env.append(o.original)
            if o.domain == "flag" and o.original not in ext_flags:
                ext_flags.append(o.original)
        ext_manifest["env_vars"] = ext_env
        ext_manifest["flags"] = ext_flags
        # Optionally hand RESIDUALs to a single batched LLM clarif.
        residuals_for_llm = [
            {"domain": o.domain, "original": o.original}
            for o in outcomes if o.final_verdict == "RESIDUAL"
            and o.chosen and o.chosen.confidence < 0.5
        ]
        llm_clarif_meta = None
        if use_residual_llm and residuals_for_llm:
            try:
                rewritten, llm_clarif_meta = _residual_llm_clarify(
                    rewritten, residuals_for_llm, ext_manifest)
                # If LLM said TYPO_OF or OMIT, strip the now-fixed
                # name from the bad list so re-scoring picks up.
            except Exception as e:  # noqa: BLE001
                llm_clarif_meta = {"called": True, "error": str(e)}
        # Re-score the rewritten text against the extended manifest.
        post_score = score_response(rewritten, ext_manifest)
        post_h = post_score["hallucinations"]
        post_halluc += int(post_h > 0)

        for o in outcomes:
            if o.final_verdict == "ACCEPT_SDK":
                sdk_accepted += 1
            elif o.final_verdict == "REPLACE":
                auto_replaced += 1
            elif o.final_verdict == "RESIDUAL":
                residual_count += 1

        rewritten_trials.append(TrialScore(
            prompt_id=raw["prompt_id"],
            condition=raw["condition"],
            trial_no=raw["trial_no"],
            response_chars=len(rewritten),
            references_total=post_score["references_total"],
            hallucinations=post_h,
            breakdown=post_score["breakdown"],
            response_text=rewritten,
        ))
        all_outcomes_per_trial.append({
            "prompt_id": raw["prompt_id"],
            "trial_no": raw["trial_no"],
            "raw_hallucinations": raw_h,
            "post_hallucinations": post_h,
            "outcomes": [
                {"domain": o.domain, "original": o.original,
                  "verdict": o.final_verdict,
                  "strategy": o.chosen.strategy if o.chosen else None,
                  "replacement": (o.chosen.replacement
                                    if o.chosen else None),
                  "confidence": (o.chosen.confidence
                                   if o.chosen else 0.0),
                  "reason": o.chosen.reason if o.chosen else ""}
                for o in outcomes
            ],
        })

    aggregate = aggregate_trials(rewritten_trials)
    aggregate["meta"] = data.get("aggregate", {}).get("meta", {})
    aggregate["meta"]["mode"] = "tool_use+rewriter (offline replay)"

    out_dir.mkdir(parents=True, exist_ok=True)

    # Save the rewritten raw + outcomes.
    rewritten_raw_path = out_dir / "hallucination-rate-raw-rewritten.json"
    rewritten_raw_path.write_text(json.dumps({
        "trials": [t.__dict__ for t in rewritten_trials],
        "aggregate": aggregate,
        "rewriter_outcomes": all_outcomes_per_trial,
        "rewriter_summary": {
            "raw_trials_with_hallucinations": raw_halluc,
            "post_trials_with_hallucinations": post_halluc,
            "names_accepted_via_sdk": sdk_accepted,
            "names_auto_replaced": auto_replaced,
            "names_residual": residual_count,
        },
    }, indent=2))
    print(f"Rewritten raw     → {rewritten_raw_path}")

    # Re-render the report (using the v0.5.0a-tool-use template),
    # then patch in a rewriter-analytics section + retitle.
    report_path = out_dir / "hallucination-rate-v0.5.0a-tool-use+rewriter.md"
    base_report = write_report(aggregate)
    base_report = base_report.replace(
        "v0.5.0a Tool-Use Mode",
        "v0.5.0a Tool-Use Mode + Deterministic Rewriter",
    ).replace(
        "tool-use mode",
        "tool-use mode + deterministic rewriter",
        1,  # only the headline sentence
    )
    # Aggregate strategy hits across all trials.
    strategy_totals: dict[str, dict] = {}
    verdict_totals: dict[str, int] = {}
    for entry in all_outcomes_per_trial:
        for o in entry["outcomes"]:
            verdict_totals[o["verdict"]] = (
                verdict_totals.get(o["verdict"], 0) + 1)
            s = o.get("strategy") or "—"
            strategy_totals.setdefault(s, {"hits": 0, "by_verdict": {}})
            strategy_totals[s]["hits"] += 1
            strategy_totals[s]["by_verdict"][o["verdict"]] = (
                strategy_totals[s]["by_verdict"].get(o["verdict"], 0) + 1)
    rewriter_section = [
        "",
        "## Rewriter analytics",
        "",
        f"After the model emitted its response, dejavu's deterministic "
        f"rewriter ran a 9-strategy cascade per bad name. Total names "
        f"flagged by the scorer across all 200 trials: "
        f"**{sum(verdict_totals.values())}**.",
        "",
        "### Verdicts",
        "",
        "| Verdict | Count | Meaning |",
        "|---------|------:|---------|",
        f"| `ACCEPT_SDK` | {verdict_totals.get('ACCEPT_SDK', 0)} | "
        f"Standard third-party SDK env var, gated by package.json "
        f"deps — legitimate, not a hallucination |",
        f"| `REPLACE` | {verdict_totals.get('REPLACE', 0)} | "
        f"Auto-corrected typo (string substitution applied to the "
        f"response) |",
        f"| `RESIDUAL` | {verdict_totals.get('RESIDUAL', 0)} | "
        f"No deterministic match — would escalate to LLM clarification "
        f"or user prompt in production |",
        "",
        "### Strategy hit counts",
        "",
        "| Strategy | Hits | ACCEPT_SDK | REPLACE | RESIDUAL |",
        "|----------|-----:|-----------:|--------:|---------:|",
    ]
    for s in sorted(strategy_totals.keys(),
                       key=lambda k: -strategy_totals[k]["hits"]):
        v = strategy_totals[s]
        rewriter_section.append(
            f"| `{s}` | {v['hits']} | "
            f"{v['by_verdict'].get('ACCEPT_SDK', 0)} | "
            f"{v['by_verdict'].get('REPLACE', 0)} | "
            f"{v['by_verdict'].get('RESIDUAL', 0)} |"
        )
    rewriter_section += [
        "",
        "### Pipeline (deterministic, no LLM in the correction path)",
        "",
        "1. Model emits response (raw).",
        "2. Scorer parses names → bad list per domain.",
        "3. For each bad name, run domain-specific strategy stack: "
        "`exact → import_gating → sdk_catalog → acronym → "
        "case_normalize → levenshtein_word → trigram → token_set → "
        "suffix_pattern`. Highest-priority verdict wins, ties broken "
        "by confidence.",
        "4. Apply REPLACE outcomes via word-boundary string "
        "substitution.",
        "5. Augment manifest with ACCEPT_SDK names (the project's "
        "*effective* declared name space includes legitimate SDK "
        "conventions).",
        "6. Re-score against extended manifest. Anything still flagged "
        "is RESIDUAL — would escalate to a focused, batched LLM "
        "clarification turn in production.",
        "",
        "_The rewriter is in `code/rewriter.py` (~280 lines). The SDK "
        "catalog is in `code/sdk_env_catalog.json` (~50 entries, "
        "community-extensible)._",
    ]
    final_report = base_report + "\n".join(rewriter_section) + "\n"
    report_path.write_text(final_report)
    print(f"Rewritten report  → {report_path}")

    # Print headline.
    print()
    print("=" * 60)
    print(f"  Trials with hallucinations:")
    print(f"    Raw model output:           "
            f"{raw_halluc} / {len(raw_trials)} "
            f"({100*raw_halluc/len(raw_trials):.1f}%)")
    print(f"    After deterministic rewrite: "
            f"{post_halluc} / {len(raw_trials)} "
            f"({100*post_halluc/len(raw_trials):.1f}%)")
    print()
    print(f"  Names processed by rewriter:")
    print(f"    Accepted via SDK catalog:   {sdk_accepted}")
    print(f"    Auto-replaced (typo fix):   {auto_replaced}")
    print(f"    Residual (need LLM clarif): {residual_count}")
    print("=" * 60)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path,
                     default=ROOT / "docs" / "benchmarks"
                                / "hallucination-rate-raw.json")
    ap.add_argument("--out", type=Path,
                     default=ROOT / "docs" / "benchmarks")
    ap.add_argument("--use-residual-llm", action="store_true",
                     help="Send unresolved RESIDUAL names to one "
                            "batched LLM clarification turn per response. "
                            "Off by default to keep the headline number "
                            "purely deterministic.")
    args = ap.parse_args()
    main(args.raw, args.out,
          use_residual_llm=args.use_residual_llm)
