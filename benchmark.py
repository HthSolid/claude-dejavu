#!/usr/bin/env python3
"""
Benchmark candidate embedding models on this machine using real session data.

Outputs:
  - benchmark-result.md       (markdown table for humans)
  - benchmark-result.json     (raw measurements)
"""
import json
import os
import sys
import time
import statistics
from pathlib import Path

import psycopg2

# Output dir defaults to the script's parent. Override with CLAUDE_DEJAVU_BENCH_OUT.
OUT_DIR = Path(os.environ.get("CLAUDE_DEJAVU_BENCH_OUT") or Path(__file__).resolve().parent)

# Postgres connection. Override via env: PG_HOST, PG_PORT, PG_USER, PG_PASS, PG_DB.
PG_DSN = (
    f"host={os.environ.get('PG_HOST', 'localhost')} "
    f"port={os.environ.get('PG_PORT', '5432')} "
    f"user={os.environ.get('PG_USER', 'postgres')} "
    f"password={os.environ.get('PG_PASS', 'postgres')} "
    f"dbname={os.environ.get('PG_DB', 'claude_dejavu')}"
)

CANDIDATES = [
    # (label, hf_model_id, family, query_prefix, passage_prefix)
    # IMPORTANT: BGE/E5/Nomic require instruction prefixes for asymmetric retrieval.
    # Skipping them costs 30+ recall points.
    ("MiniLM-L6-v2",          "sentence-transformers/all-MiniLM-L6-v2", "eng",   "", ""),
    ("bge-small-en-v1.5",     "BAAI/bge-small-en-v1.5",                 "eng",   "Represent this sentence for searching relevant passages: ", ""),
    ("bge-base-en-v1.5",      "BAAI/bge-base-en-v1.5",                  "eng",   "Represent this sentence for searching relevant passages: ", ""),
    ("e5-small-multilingual", "intfloat/multilingual-e5-small",         "multi", "query: ", "passage: "),
    ("e5-base-multilingual",  "intfloat/multilingual-e5-base",          "multi", "query: ", "passage: "),
    ("nomic-embed-text-v1.5", "nomic-ai/nomic-embed-text-v1.5",         "eng",   "search_query: ", "search_document: "),
]

# Hand-curated (query, gold-turn-id) pairs. Each entry pins a specific past turn
# as the "correct answer" for a query paraphrase. Gold IDs MUST come from your
# own PG corpus — see the "Reproduce" section in docs/MODEL_BENCHMARK.md.
#
# The default below is a placeholder. Override with a JSON file path via
# CLAUDE_DEJAVU_EVAL_PAIRS=/path/to/eval.json. The file is a list of
# {"query": "...", "gold_turn_id": <int>} objects.
EVAL_PAIRS: list[tuple[str, int]] = []
_eval_file = os.environ.get("CLAUDE_DEJAVU_EVAL_PAIRS")
if _eval_file and Path(_eval_file).is_file():
    try:
        for entry in json.loads(Path(_eval_file).read_text()):
            EVAL_PAIRS.append((entry["query"], int(entry["gold_turn_id"])))
    except Exception as e:
        print(f"warning: could not load eval pairs from {_eval_file}: {e}", file=sys.stderr)


def load_corpus_and_qrels():
    """Pull a 5000-doc corpus + the gold ids for each query from PG."""
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    # 5000 longest user/asst/text turns as the corpus (good signal density)
    cur.execute("""
        SELECT id, content
        FROM turns
        WHERE role IN ('user','assistant') AND content_type='text' AND length(content) >= 80
        ORDER BY length(content) DESC
        LIMIT 5000
    """)
    corpus = cur.fetchall()  # list of (id, content)
    corpus_ids = [r[0] for r in corpus]
    corpus_texts = [r[1][:2000] for r in corpus]  # cap

    # Use hand-curated pairs and ensure their gold docs are in the corpus
    # (force-add them if they were truncated out).
    cset = set(corpus_ids)
    qrels = []
    extra_ids = []
    for q, gold_id in EVAL_PAIRS:
        if gold_id not in cset:
            cur.execute("SELECT id, content FROM turns WHERE id=%s", (gold_id,))
            r = cur.fetchone()
            if r:
                corpus_ids.append(r[0])
                corpus_texts.append(r[1][:2000])
                cset.add(gold_id)
                extra_ids.append(gold_id)
        qrels.append((q, gold_id))
    if extra_ids:
        print(f"  Force-added {len(extra_ids)} gold docs that fell outside the top-N corpus slice")
    conn.close()
    print(f"  Corpus: {len(corpus_ids)} docs. Eval pairs: {len(qrels)} (all gold IN corpus)")
    return corpus_ids, corpus_texts, qrels


def percentile(data, p):
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p
    f = int(k); c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def benchmark_model(label, model_id, family, corpus_ids, corpus_texts, qrels, q_prefix="", p_prefix=""):
    print(f"\n=== {label} ({model_id}) ===")
    import torch
    from sentence_transformers import SentenceTransformer
    import numpy as np

    # Cold-start: time from instantiation to first encode
    t_load = time.time()
    model = SentenceTransformer(model_id, device="cpu")
    load_dt = (time.time() - t_load) * 1000

    # Determine dim
    dim = model.get_sentence_embedding_dimension()

    short = "ok thanks let me think"
    long_ = "The cilium BGP integration was failing because the multus CNI plugin did not propagate the host network namespace correctly to the second container interface, causing routing tables to diverge. " * 4
    medium = "K3s bootstrap retry with exponential backoff for SSH command failures during cluster join."

    # Cold first-call (timed separately from steady state)
    t = time.time()
    _ = model.encode([short], normalize_embeddings=True)
    cold_first_ms = (time.time() - t) * 1000

    # Warm short
    short_lats = []
    for _ in range(20):
        t = time.time()
        _ = model.encode([short], normalize_embeddings=True)
        short_lats.append((time.time() - t) * 1000)

    # Warm long
    long_lats = []
    for _ in range(10):
        t = time.time()
        _ = model.encode([long_], normalize_embeddings=True)
        long_lats.append((time.time() - t) * 1000)

    # Batch 32 throughput
    batch_inputs = [short, medium, long_] * 11  # 33
    batch_inputs = batch_inputs[:32]
    t = time.time()
    _ = model.encode(batch_inputs, batch_size=32, normalize_embeddings=True)
    batch_dt = (time.time() - t) * 1000
    batch_throughput = 32 * 1000 / batch_dt

    # Embed corpus (with passage prefix for asymmetric models)
    print(f"  embedding {len(corpus_texts)} corpus docs (prefix={p_prefix!r})...")
    corpus_for_embed = [p_prefix + c for c in corpus_texts] if p_prefix else corpus_texts
    t = time.time()
    corpus_emb = model.encode(corpus_for_embed, batch_size=32, show_progress_bar=False, normalize_embeddings=True, convert_to_numpy=True)
    corpus_dt = time.time() - t
    print(f"  done in {corpus_dt:.1f}s ({len(corpus_texts)/corpus_dt:.1f} docs/s)")

    # Eval queries (with query prefix)
    queries = [q for q, _ in qrels]
    golds = [g for _, g in qrels]
    queries_for_embed = [q_prefix + q for q in queries] if q_prefix else queries
    q_emb = model.encode(queries_for_embed, batch_size=32, normalize_embeddings=True, convert_to_numpy=True)

    # Recall@1 / Recall@5 / MRR
    sims = q_emb @ corpus_emb.T  # cosine since normalized
    recall_at_1 = recall_at_5 = 0
    mrr = 0.0
    per_query = []
    for i, gold in enumerate(golds):
        order = np.argsort(-sims[i])  # desc
        gold_pos = -1
        for rank, idx in enumerate(order):
            if corpus_ids[idx] == gold:
                gold_pos = rank
                break
        if gold_pos == 0: recall_at_1 += 1
        if 0 <= gold_pos < 5: recall_at_5 += 1
        if gold_pos >= 0: mrr += 1.0 / (gold_pos + 1)
        per_query.append({"query": queries[i], "gold_rank": gold_pos})
    n = len(golds)
    recall_at_1_pct = recall_at_1 / n if n else 0
    recall_at_5_pct = recall_at_5 / n if n else 0
    mrr = mrr / n if n else 0

    # Memory: rough RSS via /proc
    rss_mb = 0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) // 1024
                    break
    except: pass

    # Free model
    del model, corpus_emb, q_emb, sims
    import gc; gc.collect()
    if torch.cuda.is_available(): torch.cuda.empty_cache()

    return {
        "label": label,
        "model_id": model_id,
        "family": family,
        "dim": dim,
        "load_ms": round(load_dt, 1),
        "cold_first_ms": round(cold_first_ms, 1),
        "short_p50_ms": round(percentile(short_lats, 0.5), 1),
        "short_p90_ms": round(percentile(short_lats, 0.9), 1),
        "long_p50_ms": round(percentile(long_lats, 0.5), 1),
        "batch32_throughput": round(batch_throughput, 1),
        "corpus_docs_per_sec": round(len(corpus_texts) / corpus_dt, 1),
        "rss_mb_peak": rss_mb,
        "recall_at_1": round(recall_at_1_pct, 3),
        "recall_at_5": round(recall_at_5_pct, 3),
        "mrr": round(mrr, 3),
        "n_eval": n,
        "per_query": per_query,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Loading eval set...")
    corpus_ids, corpus_texts, qrels = load_corpus_and_qrels()
    if len(qrels) < 5:
        print("WARN: too few valid eval queries, results will be noisy")

    results = []
    for label, mid, fam, qp, pp in CANDIDATES:
        try:
            r = benchmark_model(label, mid, fam, corpus_ids, corpus_texts, qrels, q_prefix=qp, p_prefix=pp)
            results.append(r)
            with open(OUT_DIR / "benchmark-result.json", "w") as f:
                json.dump({"qrels_size": len(qrels), "corpus_size": len(corpus_ids), "results": results}, f, indent=2)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
            results.append({"label": label, "model_id": mid, "error": f"{type(e).__name__}: {e}"})

    # Render markdown
    md = ["# claude-dejavu — Local Benchmark Results", "", f"Corpus: {len(corpus_ids)} turns • Eval queries with gold: {len(qrels)} • Hardware: CPU-only", "", "## Performance + Recall", "", "| Model | Dim | Family | Load (ms) | Short p50 (ms) | Long p50 (ms) | Batch-32 (texts/s) | Corpus (docs/s) | RSS (MB) | Recall@1 | Recall@5 | MRR |", "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        if "error" in r:
            md.append(f"| {r['label']} | — | — | ❌ {r['error']} | | | | | | | | |")
            continue
        md.append(
            f"| **{r['label']}** | {r['dim']} | {r['family']} | "
            f"{r['load_ms']:.0f} | {r['short_p50_ms']:.1f} | {r['long_p50_ms']:.1f} | "
            f"{r['batch32_throughput']:.0f} | {r['corpus_docs_per_sec']:.1f} | {r['rss_mb_peak']} | "
            f"**{r['recall_at_1']:.2f}** | {r['recall_at_5']:.2f} | {r['mrr']:.2f} |"
        )
    md.append("")
    md.append("## Recommended defaults")
    md.append("")
    # pick best by tier
    valid = [r for r in results if "recall_at_1" in r]
    if valid:
        light_pool = [r for r in valid if r["dim"] <= 384 and r["short_p50_ms"] < 50]
        normal_pool = [r for r in valid if r["dim"] <= 768 and r["short_p50_ms"] < 200]
        multi_pool = [r for r in valid if r["family"] == "multi"]
        def best(pool, key="recall_at_1"):
            return max(pool, key=lambda r: r[key]) if pool else None
        b_light = best(light_pool); b_norm = best(normal_pool); b_multi = best(multi_pool)
        md.append(f"- **light tier**: {b_light['label'] if b_light else '—'} (Recall@1={b_light['recall_at_1'] if b_light else 0})")
        md.append(f"- **normal tier (English)**: {b_norm['label'] if b_norm else '—'} (Recall@1={b_norm['recall_at_1'] if b_norm else 0})")
        md.append(f"- **multilingual tier**: {b_multi['label'] if b_multi else '—'} (Recall@1={b_multi['recall_at_1'] if b_multi else 0})")
    with open(OUT_DIR / "benchmark-result.md", "w") as f:
        f.write("\n".join(md))
    print(f"\nWrote {OUT_DIR/'benchmark-result.md'}")


if __name__ == "__main__":
    main()
