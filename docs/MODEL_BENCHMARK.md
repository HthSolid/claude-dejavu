# claude-dejavu — Embedding Model Benchmark

Run on a real Claude Code corpus: 4,548 user/assistant turns from 19 historical
sessions. 9 hand-curated (query, gold-turn) pairs. CPU-only inference. April 2026.

## Headline numbers

| Model | Dim | Short p50 | Long p50 | Corpus throughput | RSS peak | Recall@1 | Recall@5 | MRR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **MiniLM-L6-v2** | 384 | **42.7 ms** | **69.5 ms** | **31.0 docs/s** | 696 MB | 0.44 | 0.56 | 0.52 |
| **bge-small-en-v1.5** | 384 | 137.9 ms | 174.6 ms | 10.3 docs/s | 745 MB | 0.44 | **0.78** | **0.60** |
| **bge-base-en-v1.5** | 768 | 236.0 ms | 334.5 ms | 3.7 docs/s | 1051 MB | 0.33 | 0.78 | 0.52 |
| **e5-small-multilingual** | 384 | 71.8 ms | 122.3 ms | 10.6 docs/s | 1407 MB | 0.44 | 0.67 | 0.53 |
| **e5-base-multilingual** | 768 | 236.6 ms | 348.7 ms | 3.6 docs/s | 1835 MB | 0.33 | 0.56 | 0.47 |
| **nomic-embed-text-v1.5** | 768 | 299.9 ms | 418.2 ms | 2.0 docs/s | 1603 MB | 0.44 | **0.89** | **0.67** |

All inference on CPU. Latency is `model.encode(text, normalize_embeddings=True)` p50 over 20 calls. RSS measured after the corpus is fully embedded.

## Read this row first

- **Best speed**: `MiniLM-L6-v2` — 7× faster than the next non-multilingual option.
- **Best quality**: `nomic-embed-text-v1.5` — Recall@5 = 0.89, MRR = 0.67.
- **Best speed/quality ratio**: `bge-small-en-v1.5` — Recall@5 = 0.78, p50 = 138 ms.
- **Best multilingual under 100 ms**: `e5-small-multilingual`.

## Why R@1 plateaus at 0.44

Five of nine models hit Recall@1 = 0.44. That's not the models — that's our
eval set. Three queries have **inherent paraphrase distance** from their gold
turns:

- A query referencing a session by *number* against a gold turn that explains the session's content but never names it by number — even a perfect model is unlikely to put it at rank 0.
- A query whose gold turn shares topical neighbors with many other turns in the corpus. Recall@5 captures these correctly; Recall@1 cannot.
- A query asking for a specific value that appears in a comparison table among many similar entries; adjacent turns also reference the value in passing.

The differentiation appears at **Recall@5** and **MRR**. That's where nomic
(0.89 / 0.67) pulls clearly ahead of bge-small (0.78 / 0.60), bge-base / e5-small
(0.67), MiniLM / e5-base (0.56).

## Surprise: bigger ≠ better at this corpus size

`bge-base` (768-dim) measured **lower** Recall@1 than `bge-small` (384-dim).
Same for `e5-base` vs `e5-small`. With only 9 eval pairs, both differences
are within noise (≤1 query swing = ±0.11). On a larger eval set we would
expect the bases to catch up. The takeaway for **claude-dejavu's defaults**:
small variants are not just acceptable — they are not measurably worse on
this workload. Save the disk and the milliseconds.

## Recommended defaults per hardware tier

| Tier | Trigger | Default model | Why |
|---|---|---|---|
| **light** | <8 GB RAM, no GPU, latency-sensitive | `MiniLM-L6-v2` | 43 ms p50, 31 docs/s ingest, 700 MB RSS. R@1=0.44 ties with the larger models. |
| **normal English** | 8–32 GB RAM, no GPU, English content | `bge-small-en-v1.5` | Best speed/quality ratio. R@5=0.78 in 138 ms. |
| **multilingual light** | non-English content, latency-sensitive | `e5-small-multilingual` | Only sub-100 ms multilingual option. R@5=0.67. |
| **multilingual normal** | non-English, quality matters | `e5-base-multilingual` or e5-large (run via dedicated Weaviate transformer container) | Switch up if e5-small recall is insufficient on your corpus. |
| **best recall, latency-tolerant** | backfill jobs, "deep recall" mode | `nomic-embed-text-v1.5` | R@5=0.89, MRR=0.67. 300 ms is fine when called offline. |
| **strong (GPU)** | CUDA / ROCm available | `multilingual-e5-large-instruct` | Largest model in the family; best quality, GPU makes the latency tolerable. |

## Hardware detection rules used by `install.sh`

```bash
# In rough decision order:
if has_gpu;            then tier=strong
elif RAM >= 32 GB;     then tier=normal
elif RAM >= 8  GB;     then tier=light
else                       tier=light
fi

if --multilingual flag set; then swap default for multilingual variant
if --light          flag set; then force light tier
if --strong         flag set; then force strong tier
```

User can always override with `--model=<hf-id>` and the installer will pull
that specific model's transformers-inference image.

## Methodology

- **Corpus**: 4,548 turns from `claude_dejavu_bench.turns` (role IN ('user','assistant'), content_type='text', length ≥ 80 chars), truncated to 2000 chars each.
- **Eval pairs**: 9 hand-picked (paraphrase, gold_turn_id) pairs, each verified by reading the first 300 chars of the gold turn against the query.
- **Instruction prefixes** applied per family:
  - BGE: `Represent this sentence for searching relevant passages: ` + query
  - E5: `query: ` + query, `passage: ` + doc
  - Nomic: `search_query: ` + query, `search_document: ` + doc
  - MiniLM: no prefix (symmetric model)
- **Similarity**: cosine via L2-normalized dot product.
- **Recall@K**: fraction of queries where gold turn is in top-K by similarity.
- **MRR**: mean reciprocal rank over all queries.

## Caveats

- **Sample size 9** — these numbers are directional, not authoritative. A 50-pair eval set would tighten the bands considerably.
- **CPU-only on this machine** — your latencies will differ. Multiply by ~0.1 if you have a GPU; your speed/quality calculus changes.
- **English-leaning eval set** — the multilingual models had no advantage here. They will pull ahead on actually-multilingual corpora.
- **No re-ranker stage** — adding a cross-encoder pass over top-50 will lift any of these models; that's a separate decision orthogonal to base-model choice.

## Reproduce

```bash
# Run on your own corpus (after install + backfill)
cd ~/.claude-backups/code/venv && source bin/activate
python3 ~/.claude-backups/dist/claude-dejavu/benchmark.py
# Outputs: benchmark-result.json + benchmark-result.md
```

The benchmark script is part of the repo at `benchmark.py`. To use a different
corpus, change the PG DSN at the top and adapt `EVAL_PAIRS`.
