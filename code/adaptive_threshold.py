#!/usr/bin/env python3
"""
claude-dejavu — adaptive threshold tuning (v0.3.10).

Per-project, per-channel trigram threshold tuning. The fixed default
0.4 / 0.5 thresholds work universally but are sub-optimal once a
project has a few hundred resolution outcomes. This module:

  1. Reads `correction_outcomes` rows for a (project, channel).
  2. For each candidate threshold value (0.30, 0.35, 0.40, ..., 0.80),
     simulates what would have happened: would the outcome have
     surfaced as a candidate? Was it accepted?
  3. Picks the threshold that maximizes precision_at_top * recall_at_top3.
  4. Writes the result to `resolver_thresholds`.

Channels:
  symbol   — symbol_indexer.resolve_symbol  (legacy 0.4 default)
  route    — route_indexer.resolve_route    (legacy 0.4)
  page     — page_indexer.check_new_page    (legacy 0.4)
  env      — env_indexer.resolve_env_var    (legacy 0.5)
  db       — db_indexer.resolve_db_field    (legacy 0.5)
  graphql  — graphql_indexer.resolve_*      (legacy 0.5)
  flag     — flag_indexer.resolve_flag      (legacy 0.5)

Tuning runs lazily — first time a resolver asks for the threshold OR
when a CLI explicitly requests it. Cached for 24h to avoid cold-path
recomputes inside the lint hot-path.

If the project has < 25 outcomes for a channel, we keep the default
(insufficient signal). Adaptive tuning kicks in once enough outcomes
accumulate.
"""
from __future__ import annotations

import time
from typing import Optional


# Default thresholds when no adaptive value is available.
_DEFAULT_THRESHOLDS = {
    "symbol": 0.40,
    "route": 0.40,
    "page": 0.40,
    "env": 0.50,
    "db": 0.50,
    "graphql": 0.50,
    "flag": 0.50,
}

# Minimum outcomes required before we'll override the default.
_MIN_OUTCOMES = 25

# Don't recompute inside 24h.
_TTL_SECONDS = 24 * 3600

# In-process memoization. Keyed by (project_slug, channel).
_cache: dict[tuple[str, str], tuple[float, float]] = {}  # → (threshold, ts)


def get_threshold(project_slug: str, channel: str, conn,
                   *, recompute: bool = False) -> float:
    """Return the adaptive threshold for (project, channel), or the
    channel default if there's not enough signal. Memoized in-process
    + persisted to resolver_thresholds.

    Set `recompute=True` to ignore both caches and re-tune from
    correction_outcomes."""
    default = _DEFAULT_THRESHOLDS.get(channel, 0.40)
    cache_key = (project_slug, channel)
    now = time.time()

    if not recompute:
        # In-process cache.
        cached = _cache.get(cache_key)
        if cached is not None and now - cached[1] < _TTL_SECONDS:
            return cached[0]
        # DB cache.
        with conn.cursor() as cur:
            cur.execute(
                """SELECT threshold, EXTRACT(EPOCH FROM last_tuned_at)
                   FROM resolver_thresholds
                   WHERE project_slug = %s AND channel = %s""",
                (project_slug, channel),
            )
            row = cur.fetchone()
        if row and (now - float(row[1])) < _TTL_SECONDS:
            t = float(row[0])
            _cache[cache_key] = (t, now)
            return t

    tuned = compute_optimal_threshold(project_slug, channel, conn)
    if tuned is None:
        # Not enough signal — record the default with 0 outcomes so the
        # cache doesn't keep recomputing.
        _persist(project_slug, channel, default, 0, None, None, conn)
        _cache[cache_key] = (default, now)
        return default
    threshold, observed, prec, recall = tuned
    _persist(project_slug, channel, threshold, observed, prec, recall, conn)
    _cache[cache_key] = (threshold, now)
    return threshold


def compute_optimal_threshold(project_slug: str, channel: str, conn
                               ) -> Optional[tuple[float, int, float, float]]:
    """Sweep threshold candidates against correction_outcomes and pick
    the value that maximizes the harmonic mean of:
      precision_at_top — top-1 candidate was accepted by user
      recall_at_top3   — correct match was somewhere in top-3

    Returns (threshold, observed, precision, recall) or None if
    insufficient signal."""
    with conn.cursor() as cur:
        # We need: trigram_sim of the top hit, edit_distance, the outcome
        # ('accepted' / 'rejected' / 'corrected'), and ideally the rank
        # at which the correct answer landed.
        #
        # The current correction_outcomes schema captures a single
        # outcome per (proposed_name, resolved_name). For the sweep we
        # treat:
        #   outcome='accepted' AND outcome_signal != 'no_candidates'
        #     → top_correct (precision wins)
        #   outcome='corrected' (user picked a different rank)
        #     → recall hit but not precision
        #   outcome='rejected' OR signal='no_candidates'
        #     → false positive at this threshold
        cur.execute(
            """SELECT outcome, outcome_signal, trigram_sim
               FROM correction_outcomes
               WHERE project_slug = %s
                 AND outcome_signal LIKE %s
               LIMIT 5000""",
            (project_slug, f"%{channel}%"),
        )
        rows = cur.fetchall()
    if len(rows) < _MIN_OUTCOMES:
        return None

    # Sweep.
    best = None
    for t in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
        true_pos = false_pos = false_neg = 0
        for outcome, _signal, sim in rows:
            sim = float(sim or 0)
            surfaced = sim >= t
            correct = outcome in ("accepted",)
            if surfaced and correct:
                true_pos += 1
            elif surfaced and not correct:
                false_pos += 1
            elif (not surfaced) and outcome == "corrected":
                false_neg += 1
        prec = true_pos / max(1, true_pos + false_pos)
        recall = true_pos / max(1, true_pos + false_neg)
        # Harmonic mean (F1).
        f1 = (2 * prec * recall) / max(1e-9, prec + recall)
        if best is None or f1 > best[0]:
            best = (f1, t, prec, recall)
    if best is None:
        return None
    return (best[1], len(rows), best[2], best[3])


def _persist(project_slug: str, channel: str, threshold: float,
              observed: int, prec: Optional[float], recall: Optional[float],
              conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO resolver_thresholds
                 (project_slug, channel, threshold, outcomes_observed,
                  precision_at_top, recall_at_top3, last_tuned_at)
               VALUES (%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (project_slug, channel)
                 DO UPDATE SET threshold = EXCLUDED.threshold,
                               outcomes_observed = EXCLUDED.outcomes_observed,
                               precision_at_top = EXCLUDED.precision_at_top,
                               recall_at_top3 = EXCLUDED.recall_at_top3,
                               last_tuned_at = now()""",
            (project_slug, channel, threshold, observed, prec, recall),
        )


def list_thresholds(conn, project_slug: Optional[str] = None) -> list[dict]:
    sql = ("SELECT project_slug, channel, threshold, outcomes_observed, "
           "       precision_at_top, recall_at_top3, last_tuned_at "
           "FROM resolver_thresholds")
    params: list = []
    if project_slug:
        sql += " WHERE project_slug = %s"
        params.append(project_slug)
    sql += " ORDER BY project_slug, channel"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
