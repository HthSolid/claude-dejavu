# claude-dejavu Hallucination Rate Benchmark

**Headline:** claude-dejavu reduces Claude's hallucination rate by **89.3%** (95% CI: 82.7–95.1%, McNemar p = 9.593e-27, n = 400 trials).

_Model:_ `claude-sonnet-4-6` · _Trials per cell:_ 10 · _Prompts:_ 20 · _Wall-clock:_ 2931.6s · _Mock:_ False

## Methodology

1. Built a synthetic project with a fixed schema: 5 Prisma models, 12 env vars, 6 HTTP routes, 4 GraphQL types, 5 feature flags.
2. 20 prompts spanning every grounding domain — pure DB, pure env, pure route, pure GraphQL, pure flag, plus multi-domain combos.
3. For each prompt, ran 10 trials × 2 conditions:
    - **OFF:** plain Claude, no system prompt context
    - **ON:** system prompt enriched with the schema digest
      (the same ground truth dejavu's PreToolUse hook injects)
4. Scored each response deterministically by parsing every grounded name (env var, model.field, URL, gql field, flag key) and looking it up against the schema. Strict matching — any name not in the schema counts as a hallucination.

## Result

|                       | Hallucination rate |
|-----------------------|-------------------:|
| **OFF** (no dejavu)   | 51.5% |
| **ON** (with dejavu)  | 5.5% |
| **Absolute reduction**| 46.0 pp |
| **Relative reduction**| **89.3%** |

## Per-domain breakdown

Hallucinations per reference, by grounding domain:

| Domain | OFF refs | OFF halluc. | OFF rate | ON refs | ON halluc. | ON rate |
|--------|---------:|------------:|---------:|--------:|-----------:|--------:|
| db      |      186 |          13 |    7.0% |     191 |          1 |    0.5% |
| env     |      187 |         114 |   61.0% |     131 |         10 |    7.6% |
| route   |        0 |           0 |    0.0% |       0 |          0 |    0.0% |
| graphql |       95 |          11 |   11.6% |     180 |          0 |    0.0% |
| flag    |       19 |           0 |    0.0% |      22 |          0 |    0.0% |

## Per-prompt breakdown (sorted by reduction)

| Prompt | n | OFF rate | ON rate | Reduction |
|--------|--:|---------:|--------:|----------:|
| `p13-gql-query-posts` | 10 | 100.0% |   0.0% | +100.0 pp |
| `p18-multi-prisma-gql` | 10 | 100.0% |   0.0% | +100.0 pp |
| `p06-env-redis-connect` | 10 |  90.0% |   0.0% | +90.0 pp |
| `p07-env-aws-config` | 10 |  90.0% |   0.0% | +90.0 pp |
| `p16-multi-prisma-env` | 10 |  90.0% |   0.0% | +90.0 pp |
| `p08-route-fetch-posts` | 10 |  70.0% |   0.0% | +70.0 pp |
| `p04-prisma-update-user-role` | 10 |  60.0% |   0.0% | +60.0 pp |
| `p10-route-update-user` | 10 |  60.0% |   0.0% | +60.0 pp |
| `p05-env-stripe-init` | 10 |  50.0% |   0.0% | +50.0 pp |
| `p09-route-create-user-client` | 10 |  50.0% |   0.0% | +50.0 pp |
| `p20-all-domains` | 10 |  50.0% |   0.0% | +50.0 pp |
| `p01-prisma-find-by-email` | 10 |  40.0% |   0.0% | +40.0 pp |
| `p17-multi-route-flag` | 10 |  40.0% |   0.0% | +40.0 pp |
| `p03-prisma-published-posts` | 10 |  30.0% |  10.0% | +20.0 pp |
| `p02-prisma-create-user` | 10 |  10.0% |   0.0% | +10.0 pp |
| `p11-gql-query-user` | 10 |  10.0% |   0.0% | +10.0 pp |
| `p12-gql-mutation-create-user` | 10 |   0.0% |   0.0% |  +0.0 pp |
| `p15-flag-audit-rollout` | 10 |   0.0% |   0.0% |  +0.0 pp |
| `p19-multi-env-route` | 10 |   0.0% |   0.0% |  +0.0 pp |
| `p14-flag-checkout-gate` | 10 |  90.0% | 100.0% | -10.0 pp |

## Statistics

- Trials where OFF hallucinated and ON did not (b): **93**
- Trials where ON hallucinated and OFF did not (c): **1**
- McNemar exact two-sided p-value: **9.593e-27**
- Bootstrap 95% CI on relative reduction: **82.7% – 95.1%**

## Reproducibility

```bash
ANTHROPIC_API_KEY=sk-ant-… python tests/bench_hallucination.py \
    --n=10 --seed=42 --output=docs/benchmarks/
```

Raw trial data is at `docs/benchmarks/hallucination-rate-raw.json` (every prompt, every trial, every response). The synthetic project fixture is regenerated from `tests/bench_hallucination.py::build_fixture` each run — no manual setup required.
