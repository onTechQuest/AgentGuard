# Milestone 12: correctness, production usage and latency qualification

Correctness coverage, production-cost reporting and repeated latency
qualification now have explicit populations and decisions. Production prompts,
models, tools, datasets, retry behavior and YAML thresholds are unchanged.

## CLI and CI

```powershell
# Normal PR correctness/safety smoke (also the default with no arguments)
python scripts/run_agentguard_eval.py --suite smoke

# Complete dataset coverage, without repeated performance sampling
python scripts/run_agentguard_eval.py --suite full

# Eight functional smoke scenarios, five executions each; hard latency gate
python scripts/run_agentguard_eval.py --suite performance

# Same architecture, larger benchmark and a distinct generated artifact
python scripts/run_agentguard_eval.py --suite performance --repetitions 25 --report reports/performance_200.json
```

The existing PR workflow already selects smoke, so no workflow change was needed.
No expensive scheduled job was added. Performance qualification is explicitly
invokable for dedicated/manual automation. `--repetitions` and `--report` are
performance-only options; `full` remains dataset coverage. Performance is a CLI
mode, not a new dataset tier.

## Correctness smoke/full decisions

The normal smoke currently has eight functional and eight safety production
executions. Every selected scenario still executes once, with its captured record
reused for evaluation. All existing deterministic, semantic and safety gates
remain hard-blocking. The functional production token gate remains
`average_tokens_per_run <= 1500` from YAML.

Latency is recorded and displayed separately for each production population:

- execution count and latency observation count;
- average and maximum latency;
- observed nearest-rank P95, explicitly report-only and unqualified;
- count above the YAML latency threshold and every exceeding scenario/duration;
- average production tokens per execution and number of available token samples.

The `p95_latency_ms` entry appears as **REPORT ONLY**, with the unchanged threshold
and an explanation directing qualification to performance mode. It is not shown
as PASS. Eight-sample P95 remains the maximum; no observation is removed or
recalculated with a different percentile convention. Correctness smoke/full can
pass despite a latency spike, while any other failing configured gate still
produces a nonzero exit code.

## Scorecard and accounting compatibility

`AgentGuardScorecard` adds `functional_production_usage` and
`safety_production_usage`, each a `ProductionUsage` summary with:

- `execution_count`;
- `average_latency_ms`, `maximum_latency_ms`, `p95_latency_ms`;
- `token_observation_count`, `average_tokens_per_execution`;
- `latency_observations`, preserving scenario IDs and individual durations.

Exceedances are selected against the YAML threshold at reporting time rather
than embedding a duplicate threshold in aggregation. Missing token values remain
unavailable, not zero. Empty populations show unavailable averages/maxima.

Legacy scorecard fields `average_latency_ms`, `p95_latency_ms` and
`average_tokens_per_run` retain their functional-only meaning. Existing constructor
arguments remain valid, and `build_scorecard` accepts optional `safety_records`.
Additional populated fields naturally participate in dataclass equality.

`evaluate_quality_gate` retains strict enforcement by default for existing
callers. The correctness CLI explicitly selects `latency_mode="report_only"`.
Only that gate entry gets `passed=None, enforced=False`; all other comparisons
and unavailable-metric failures are unchanged.

Production usage comes exclusively from EvaluationRecord, covering router,
support-agent turns and tools. Evaluator objects are not usage inputs. Separate
semantic-judge/safety-classifier cost reporting can be added later without
changing these populations; no judge-usage capture is required or fabricated now.

## Performance qualification

The performance mode delegates to the existing latency diagnostic using
`--qualify`, so there is one repeated-execution implementation. It loads only
functional smoke scenarios and invokes no DeepEval/safety judges. The default
is five passes, producing forty observations. Fewer than five repetitions or
fewer than forty planned observations are rejected before execution.

Qualification verifies every expected `(scenario_id, repetition)` exactly once.
Missing, duplicate, failed, incomplete or invalid observations fail qualification.
Every attempt remains recorded, including first requests and slow observations.
No retry-until-pass, warmup exclusion, trimming or scenario-level averaging occurs.

The hard comparison is the existing YAML `p95_latency_ms <= 7500`, using the same
nearest-rank calculation as the scorecard: `ceil(95*N/100)`. The console reports
N, mean, P50, P90, P95, P99, min, max, failures, and exceedance count/rate. It lists
each exceedance. For forty successful observations P95 selects rank 38, while
P99 and maximum retain the slowest observation. A failed execution blocks
qualification even if the computed percentile would pass.

Forty is the initial qualification policy, not a guarantee of a precise
population-tail estimate. Increase repetitions for 100–200 observations as needed
(13 passes produce 104, and 25 produce 200 with eight scenarios).

The default generated artifact is `reports/performance_qualification.json`.
An existing output is never silently overwritten; use `--report` for a fresh path.
The JSON preserves observations, distributions, exceedances and the qualification
decision. Reports remain gitignored. PASS exits 0; failed qualification exits 1.
Standalone `scripts/audit_latency.py` without `--qualify` remains diagnostic-only.

## Validation and changed files

`python -m pytest tests -q`: **1,165 passed**. Tests cover eight-sample smoke
reporting, functional/safety/judge isolation, token-gate preservation, unchanged
non-latency gates, forty-observation hard gating, exact boundaries, retained
outliers, invalid/failed/incomplete/duplicate sampling, 200-observation scaling,
and CLI delegation with no live calls. No live performance or full evaluation
was run for this implementation.

Changed files:

- `src/agentguard/performance.py` (new)
- `src/agentguard/scorecard.py`
- `src/agentguard/quality_gate.py`
- `scripts/run_agentguard_eval.py`
- `scripts/audit_latency.py`
- `tests/test_performance_qualification.py` (new)
- `tests/test_scorecard.py`
- `tests/test_quality_gate.py`
- `tests/test_run_agentguard_eval.py`
- `docs/PERFORMANCE_QUALIFICATION.md` (new)
- `docs/LATENCY_MEASUREMENT_AUDIT.md` (historical-status note)
