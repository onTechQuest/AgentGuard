# Milestone 13C.4F: controlled deadline qualification

This document describes the 13C.4F qualification harness. In 13C.5, Candidate R
was promoted into the [v1 production policy](PRODUCTION_RELIABILITY_V1.md).
Normal callers now use those limits by default. Reliability qualification remains
descriptive through an explicit unbounded diagnostic policy unless candidate
enforcement is selected. L/R experimental configurations are unchanged.

## Activation and ownership

The reliability CLI requires `--enforce-candidate-budget` to apply the selected
candidate's `qualification_budget_policy`. This flag is rejected outside
`--suite reliability`, and cannot be combined with `--execute-retries`.
Enforcement requires a complete stage policy and a disabled retry policy with
`max_attempts=1` and `shared_extra_attempts_per_request=0`.
The separate baseline configuration is unchanged.

`QualificationBudgetPolicy` is now a compatibility name for the production-owned
`StageBudgetPolicy`; qualification converts it to `RuntimeReliabilityPolicy`.
The outer request
wrapper creates its request budget before observation starts, and the existing
`request_execution.stage()` boundaries perform admission and acceptance. Request
and stage scope use context variables, not environment variables or shared global
mutable policy. An already shorter caller budget remains authoritative. Existing
direct finite-budget APIs and their cancellation semantics remain supported.

## Hierarchical budget semantics

| Candidate | Request deadline | Router cap | Recovery minimum | Synthesis cap/reserve |
| --- | ---: | ---: | ---: | ---: |
| L | 10,000 ms | 4,000 ms | 2,000 ms | 3,000 ms |
| R | 20,000 ms | 13,000 ms | 3,000 ms | 6,000 ms |

Stage values are maxima and admission requirements, **not additive guarantees**.
R does not require a 22-second request deadline.

- Router: allocate `min(request remaining, router cap)` from stage entry.
  Preparation and the single model attempt share this bound. Repeated dispatch
  admission checks do not reset the stage deadline.
- Recovery: before constructing its planner, require remaining request time to
  cover `recovery minimum + synthesis reserve`. An admitted recovery child ends
  at `request deadline - synthesis reserve`; the minimum is not a recovery
  timeout. For R, 8 seconds remaining denies recovery (needs 9); an early router
  can admit it. Recovery is a separate logical model call, never a retry.
- Required operations: existing deterministic ordering and request admission
  remain unchanged. No generic tool timeout or retry is introduced.
- Synthesis: after required operations finish, allocate
  `min(request remaining, synthesis cap)`, requiring positive remaining time.
  This is a maximum allowance, not a promise to admit the entire configured cap.
  Recovery reserves the full cap to protect downstream opportunity; intervening
  local work still consumes the original budget.
- Final acceptance: the original request deadline also covers work after
  synthesis. No child can extend it. At an exact deadline, the budget is exhausted.

The synchronous executor checks results when work returns; it does not preempt a
provider request or enforce a wall-clock termination bound. A late router result
cannot continue into policy/tool/synthesis. A late synthesis cannot become an
accepted response or fallback. Completed attempts retain their returned usage;
completed tools retain their actual completion state. Cancellation requested and
cancellation observed are distinct and are not inferred from rejection.

## Reporting and replay

Enforced reports contain candidate/configured/effective policies, stage admission
and allocated allowances, rejection component, late completion and abandoned
result flags, completed/incomplete operation counts, recovery remaining budget,
minimum and reserve requirements, usage of rejected requests and its completeness,
and latency to observed termination. Router/synthesis cap exceedances are distinct
from request exhaustion. No token-saving or provider-cancellation claim is made.

The offline helper reuses actual `stage()` admission/acceptance with a virtual
clock and no production executor. Archived reports lack exact local-stage starts:
router is anchored at zero, recovery/synthesis use saved attempt offsets plus
full component durations, and aggregate operation time is placed before synthesis.
This explicit reconstruction checks equivalent timing inputs; it cannot prove
unrecorded boundary timing or predict changed provider behavior.

```powershell
python scripts/replay_candidate_deadlines.py --qualification-config config/reliability-deadline-candidates.json --reports reports/reliability_baseline.json reports/reliability_13c4b_pass1.json reports/reliability_13c4b_pass2.json reports/reliability_13c4d_tail_probe.json --output reports/reliability_13c4f_replay.json
```

That offline replay accepted 130/136 observations under L and 136/136 under R.
L's six earlier router rejections match 13C.4E. Retrospectively, two of those
observations also exceed the L synthesis and request thresholds; their replay
stops at the router and does not execute downstream stages. The single saved
recovery is admitted under both candidates. All populations remain separated by
source report and workload/recovery group, including the selected tail probe.
N=1 recovery is not a general reliability qualification.

Outputs require a fresh JSON destination in gitignored `reports/`. Existing
reports, planning manifests and baseline configuration are never overwritten.

## Commands for later controlled live qualification

These commands make real production calls. They were **not executed**. They
select the existing smoke population, one repetition, no judges and no retries.
Use fresh report destinations if either file already exists.

Candidate L:

```powershell
python scripts/run_agentguard_eval.py --suite reliability --qualification-config config/reliability-deadline-candidates.json --execution-candidate candidate_L --enforce-candidate-budget --report reports/reliability_13c4f_candidate_L.json
```

Candidate R:

```powershell
python scripts/run_agentguard_eval.py --suite reliability --qualification-config config/reliability-deadline-candidates.json --execution-candidate candidate_R --enforce-candidate-budget --report reports/reliability_13c4f_candidate_R.json
```

Exit zero means reporting completed, not that all requests succeeded or that a
candidate is production-ready. Rejected requests remain reported. Omitting the
enforcement flag runs unlimited descriptive measurement.
