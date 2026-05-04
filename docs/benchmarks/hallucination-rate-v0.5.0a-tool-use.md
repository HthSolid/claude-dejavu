# claude-dejavu Hallucination Rate Benchmark — v0.5.0a Tool-Use Mode

**Headline:** claude-dejavu **tool-use mode** reduces Claude's hallucination rate by **85.4%** (rate 51.5% → 7.5%, n = 200 trials, baseline from v0.3.12).

_Model:_ `claude-sonnet-4-6` · _Trials per cell:_ 10 · _Prompts:_ 20 · _Wall-clock:_ 2872.5s · _Mock:_ False

## Methodology

1. Built a synthetic project with a fixed schema: 5 Prisma models, 12 env vars, 6 HTTP routes, 4 GraphQL types, 5 feature flags.
2. 20 prompts spanning every grounding domain — pure DB, pure env, pure route, pure GraphQL, pure flag, plus multi-domain combos.
3. For each prompt, ran 10 trials in tool-use mode:
    - **TOOL_USE:** plain Claude with the `dejavu_check_name(name, kind?, parent?)` tool registered. The model decides when to call it; tool returns OK / TYPO (with the right name) / UNKNOWN against the synthetic manifest.
    - Baseline (OFF rate) is the published v0.3.12 number — not re-measured.
4. Scored each response deterministically by parsing every grounded name (env var, model.field, URL, gql field, flag key) and looking it up against the schema. Strict matching — any name not in the schema counts as a hallucination.

## Result

|                                  | Hallucination rate |
|----------------------------------|-------------------:|
| **OFF** (baseline v0.3.12)    | 51.5% |
| **TOOL_USE** (dejavu_check_name) | 7.5% |
| **Absolute reduction**           | 44.0 pp |
| **Relative reduction**           | **85.4%** |

## Per-domain breakdown

Hallucinations per reference, by grounding domain:

| Domain | TOOL_USE refs | TOOL_USE halluc. | TOOL_USE rate |
|--------|--------------:|-----------------:|--------------:|
| db      |            95 |                1 |          1.1% |
| env     |           107 |               14 |         13.1% |
| route   |             0 |                0 |          0.0% |
| graphql |           129 |                0 |          0.0% |
| flag    |            27 |                0 |          0.0% |

## Per-prompt breakdown (tool_use vs baseline)

| Prompt | n | TOOL_USE rate |
|--------|--:|--------------:|
| `p01-prisma-find-by-email` | 10 |   0.0% |
| `p03-prisma-published-posts` | 10 |   0.0% |
| `p04-prisma-update-user-role` | 10 |   0.0% |
| `p05-env-stripe-init` | 10 |   0.0% |
| `p06-env-redis-connect` | 10 |   0.0% |
| `p08-route-fetch-posts` | 10 |   0.0% |
| `p09-route-create-user-client` | 10 |   0.0% |
| `p11-gql-query-user` | 10 |   0.0% |
| `p12-gql-mutation-create-user` | 10 |   0.0% |
| `p13-gql-query-posts` | 10 |   0.0% |
| `p15-flag-audit-rollout` | 10 |   0.0% |
| `p16-multi-prisma-env` | 10 |   0.0% |
| `p17-multi-route-flag` | 10 |   0.0% |
| `p19-multi-env-route` | 10 |   0.0% |
| `p20-all-domains` | 10 |   0.0% |
| `p02-prisma-create-user` | 10 |  10.0% |
| `p10-route-update-user` | 10 |  10.0% |
| `p18-multi-prisma-gql` | 10 |  10.0% |
| `p07-env-aws-config` | 10 |  30.0% |
| `p14-flag-checkout-gate` | 10 |  90.0% |

## Statistics

- TOOL_USE trials: **200**, of which **15** contained ≥1 hallucinated identifier (7.5%).
- Baseline (OFF) rate: **51.5%** — published v0.3.12 number (n=400, McNemar p=9.6e-27, see `hallucination-rate-v0.3.12.md`). Not re-measured for this run.
- McNemar / paired CI not applicable: no paired OFF trials in this run. The reduction is a one-sample comparison against the published v0.3.12 OFF rate.

## Reproducibility

```bash
ANTHROPIC_API_KEY=sk-ant-… python tests/bench_hallucination.py \
    --only=tool_use --n=10 --seed=42 --output=docs/benchmarks/
```

Raw trial data is at `docs/benchmarks/hallucination-rate-raw.json` (every prompt, every trial, every response). The synthetic project fixture is regenerated from `tests/bench_hallucination.py::build_fixture` each run — no manual setup required.
