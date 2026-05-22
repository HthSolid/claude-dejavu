"""Top-level test runner for the infra-free suite.

Runs every standalone test file and aggregates pass/fail counts.
Used as the canonical pre-publish regression check; doesn't need
Postgres or Weaviate. For full E2E coverage including the indexer
and MCP stdio paths, run `python tests/smoke.py` separately (needs
PG + Weaviate up)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SUITES = [
    ("strategies", "test_strategies.py"),
    ("helpers + cascade", "test_helpers.py"),
    ("error_log", "test_error_log.py"),
    ("runtime_trap", "test_runtime_trap.py"),
    ("scraper", "test_scraper.py"),
    ("v05 followups (integration)", "test_v05_followups.py"),
    ("ingester robustness (NUL bytes / encoding / boundary)",
     "test_ingester_robustness.py"),
    ("install.py helpers (v0.5.0e — docker compose project name, "
       "INSTALL_STATUS, venv resolver, schema migrations)",
     "test_install.py"),
    ("health (storage integrity + auto-repair)",
     "test_health.py"),
    ("pg_rescue (v0.8.3 — TOAST scan / patch / rebuild / reindex)",
     "test_pg_rescue.py"),
    ("weaviate_rescue (v0.8.3 — scan / quarantine / redo-class)",
     "test_weaviate_rescue.py"),
    ("session_copy_pg (v0.8.3 — pg_dump / pg_restore staging)",
     "test_session_copy_pg.py"),
    ("session_copy_weaviate (v0.8.3 — volume tarball staging)",
     "test_session_copy_weaviate.py"),
    ("changelog_distill (v0.8.4 G1.b — release-line distiller)",
     "test_changelog_distill.py"),
    ("what-shipped (v0.8.4 G1.a — CLI release summary)",
     "test_what_shipped.py"),
    ("session_start_digest hook (v0.8.4 G1.c — cross-project release digest)",
     "test_hooks_session_start_digest.py"),
    ("ingester retry-vectorize (v0.8.4 G4 — parallel sharding)",
     "test_ingester_retry_vectorize.py"),
    ("JIT recall hook (v0.8.5 G2 — UserPromptSubmit opt-in)",
     "test_jit_recall_hook.py"),
    ("distill_pipeline (v0.8.6 — Context Economy phase 3 "
       "anchor preservation)",
     "test_distill_pipeline.py"),
    ("ingester gist wiring (v0.8.6 — distill_pipeline + "
       "backfill-gists)",
     "test_ingester_gist.py"),
    ("search response shape (v0.8.6 — gist-first + --full flag "
       "+ anchor parsing)",
     "test_search_returns_gist.py"),
    ("ingester gate (v0.8.7 — Context Economy phase 4 trash gate)",
     "test_ingester_gate.py"),
    ("query_embed (v0.8.7 — caller-side query embedding + "
       "per-process cache)",
     "test_query_embed.py"),
    ("search RRF (v0.8.7 — retrieve_rrf fusion of BM25 + nearVector)",
     "test_search_rrf.py"),
    ("bridge signature parity (v0.8.7 audit — stub MUST match real wheel "
       "call shape for every primitive)",
     "test_bridge_signature_parity.py"),
    ("embed_cache PG (v0.8.8 — Context Economy phase 5 cross-session "
       "persistence + lazy eviction)",
     "test_embed_cache_pg.py"),
    ("topic extraction (v0.8.9 — Context Economy phase 6 ai_title-based "
       "topic labels)",
     "test_topic_extraction.py"),
    ("topic_reweight wiring (v0.8.9 — Context Economy phase 6 opt-in "
       "topic-aware re-ranking, CLI + JIT)",
     "test_topic_reweight.py"),
    ("v0.8.10 MCP tools (Fix 1 obs tokenization + Fix 2 sum query "
       "param + Fix 3 cache lock + connect_timeout)",
     "test_v0810_mcp_tools.py"),
    ("ingester WV_CLASS override (v0.8.12 — env / config.env precedence "
       "for bench-class isolation)",
     "test_ingester_wv_class_override.py"),
    ("dejavu_summary digest mode (v0.8.14 — limit=1/no-query gate, "
       "topics cap, stratified sample, env override)",
     "test_v0814_digest_mode.py"),
    ("recall-surface tools (v0.8.16 — dejavu_global_search + "
       "session_export + status + JIT identifier extractor)",
     "test_v0816_recall_surface.py"),
    ("PreCompact hook (v0.8.17 — pre-compaction recovery systemMessage "
       "so compacted-away turns are recoverable via "
       "dejavu_session_export)",
     "test_v0817_pre_compact_hook.py"),
    ("recall-completeness bundle (v0.8.18 — IPv6 + opaque-token "
       "thresholds + global_search ordering + session_export cap + "
       "WV count + PreCompact polish + JIT substring + find_sessions)",
     "test_v0818_recall_completeness.py"),
    ("next-gen bundle (v0.9.0 — Voyage/Cohere/BGE reranker cascade "
       "+ PreCompact LLM synthesis of recovery memo)",
     "test_v0900_next_gen.py"),
    ("local vector index + per-project reranker (v0.9.1 — pure-numpy "
       "in-process index, .dejavu-rerank file overrides)",
     "test_v0901_local_vector_index.py"),
    ("HNSW backend + incremental sync (v0.9.2 — hnswlib companion to "
       "TurnVectorIndex, Stop-hook auto-sync)",
     "test_v0902_hnsw_backend.py"),
    ("local vector index auto-enable (v0.9.3 — flips opt-in gate to "
       "auto-on; env=false becomes explicit kill switch)",
     "test_v0903_vector_index_auto_on.py"),
    ("background bootstrap + HNSW presets (v0.9.4 — daemon-thread "
       "bootstrap on MCP startup; CLAUDE_DEJAVU_HNSW_PRESET=high-recall"
       " | balanced | low-memory)",
     "test_v0904_bg_bootstrap_and_preset.py"),
    ("per-project vector index isolation (v0.9.5 — slug subdirs "
       "under <data_root>/vector_index/, legacy auto-migration, "
       "--slug CLI flag)",
     "test_v0905_per_project_index.py"),
    ("recall feedback loop (v0.9.6 — implicit + explicit relevance "
       "signals, popularity boost in dejavu_search)",
     "test_v0906_feedback_loop.py"),
    ("learned ranker (v0.10.0 — 7-feature LR with champion/challenger "
       "shadow mode + arbiter at 60% win-rate over 1000 trials)",
     "test_v0100_learned_ranker.py"),
]


def main() -> int:
    total_pass = 0
    total_fail = 0
    crashed: list[str] = []
    print("=" * 60)
    print("claude-dejavu — pre-publish regression suite")
    print("=" * 60)

    for label, fname in SUITES:
        print(f"\n┌─ {label}")
        print(f"│  ({fname})")
        print(f"└─ ", end="", flush=True)
        r = subprocess.run([sys.executable, str(ROOT / fname)],
                              capture_output=True, text=True)
        # Parse the "PASS: N     FAIL: M" footer
        out = r.stdout
        last_line = ""
        for line in reversed(out.splitlines()):
            if line.startswith("PASS:"):
                last_line = line
                break
        if not last_line:
            print(f"\033[31mCRASHED\033[0m (rc={r.returncode})")
            print(out[-500:])
            print(r.stderr[-500:])
            crashed.append(fname)
            continue
        # Extract numbers
        try:
            parts = last_line.replace(":", " ").split()
            p = int(parts[1]); f = int(parts[3])
        except (IndexError, ValueError):
            p = f = -1
        total_pass += max(0, p)
        total_fail += max(0, f)
        if r.returncode == 0:
            print(f"\033[32mOK\033[0m  ({p} pass)")
        else:
            print(f"\033[31mFAIL\033[0m  ({p} pass, {f} fail)")
            # Print the failure lines for context
            for ln in out.splitlines()[-10:]:
                print(f"   {ln}")

    print()
    print("=" * 60)
    print(f"TOTAL: {total_pass} pass, {total_fail} fail, "
            f"{len(crashed)} crashed suite(s)")
    if crashed:
        print(f"  crashed: {', '.join(crashed)}")
    print("=" * 60)
    return 0 if (total_fail == 0 and not crashed) else 1


if __name__ == "__main__":
    sys.exit(main())
