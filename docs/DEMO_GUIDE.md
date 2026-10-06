# Offline demonstration guide

Run from the repository root in Windows PowerShell with the virtual environment
and dependencies already installed. The inspection commands read committed
**synthetic** evidence without calling a provider, judge, business tool or
evaluation runner. The separate test section exercises capture/scoring with
controlled doubles and local tools. Neither route needs `OPENAI_API_KEY`.
Install dependencies before starting; full-suite qualification is a separate step.

## Prepare the fixtures

```powershell
New-Item -ItemType Directory -Path reports/evaluations -Force | Out-Null
Copy-Item -Path tests/fixtures/model_prompt_runs/16000000000000000000000000000001,tests/fixtures/model_prompt_runs/16000000000000000000000000000002,examples/portfolio/evaluations/17000000000000000000000000000001,examples/portfolio/evaluations/17000000000000000000000000000002 -Destination reports/evaluations -Recurse
$success = Get-Content -Encoding UTF8 -Raw examples/portfolio/evaluations/17000000000000000000000000000001/results.json | ConvertFrom-Json
$deadline = Get-Content -Encoding UTF8 -Raw examples/portfolio/evaluations/17000000000000000000000000000002/results.json | ConvertFrom-Json
$candidate = Get-Content -Encoding UTF8 -Raw tests/fixtures/model_prompt_runs/16000000000000000000000000000002/results.json | ConvertFrom-Json
```

Use only these reserved fixture IDs. On repeat setup, verify existing copies
against the committed examples before copying. `reports/` stays gitignored.
The success fixture contains selected illustrative passing checks; it is not a
complete structural-release bundle. The comparison pair intentionally contains
failed quality decisions. None of these files proves live provider quality,
current-source qualification, or eligibility for baseline promotion.

## The problem

A plausible answer can still use the wrong tool, wrong arguments, leak
data, contradict a tool result, or miss its deadline. AgentGuard combines explicit
contracts and semantic evidence so a team can explain both a decision and its
limitations.

## Architecture

The [README diagram](../README.md#how-it-fits-together) shows
execution, deterministic/semantic/safety evaluation, evidence, gates, and separate
descriptive comparisons. The registry does not silently control release decisions.

## Verify capture and deterministic evaluation offline

Public fixture projections omit raw input, response and tool payloads. They show
retained results, not a newly executed `EvaluationRecord`. Existing tests exercise
actual capture/scoring with controlled doubles:

```powershell
$env:PYTHON_DOTENV_DISABLED = "1"
$env:DEEPEVAL_TELEMETRY_OPT_OUT = "YES"
$env:OPENAI_AGENTS_DISABLE_TRACING = "1"
.venv/Scripts/python.exe -m pytest tests/test_evaluation_record.py tests/test_scoring.py tests/test_execution_plan.py tests/test_safety_evaluator.py tests/test_portfolio_demo.py -q
```

Coverage includes required/missing calls, arguments, allowed-tool constraints,
required work before synthesis, final-response checks, safety and sanitized demo
inspection. Model/judge boundaries are doubled; no credentials are required.
The generic scorer does not enforce call order or inspect private reasoning.

## Inspect evaluation evidence

```powershell
$success.observations | Select-Object scenario_id,population,completed
$success.observations[0].deterministic | Format-List
$success.observations[0].semantic | ConvertTo-Json -Depth 5
$success.observations | Where-Object population -eq safety | Select-Object scenario_id,safety | ConvertTo-Json -Depth 6
$candidate.observations | Where-Object population -eq safety | Select-Object scenario_id,safety | ConvertTo-Json -Depth 6
```

Expected: the successful fixture has completed observations, true functional/tool/
argument/grounding checks and retained semantic scores. The comparison candidate
has `safety.final_pass=false`. These are fixture outcomes, not newly executed
judgments. A null safety sub-check is unavailable evidence, not an inferred pass.

## Quality gates and CI

```powershell
$success.aggregate_results.quality_result | ConvertTo-Json -Depth 5
$candidate.aggregate_results.quality_result | ConvertTo-Json -Depth 5
Get-Content -Encoding UTF8 .github/workflows/agentguard.yml
```

Expected: illustrative PASS versus FAIL; the candidate functional-accuracy check
retains its actual value, operator and threshold. Gate requirements are not
recomputed by this demo. The CI workflow runs offline tests. Manual opt-in
additionally runs credentialed live smoke/release qualification; a nonzero required-gate result blocks that job.
Live qualification is outside this offline demo. The M16 fixture's retained
threshold is historical synthetic evidence, not a replacement for current YAML.

## Retained failure evidence

```powershell
$deadline | Select-Object completion_state,executed_scenario_count,completed_scenario_count
$deadline.executions[0].failure_evidence | ConvertTo-Json -Depth 6
```

Expected: `INCOMPLETE`, one execution and zero completed scenarios;
`RequestDeadlineExceeded`, `DEADLINE_EXHAUSTED`, component `synthesis`, request ID,
19,000 ms illustrative budget/latency, abandoned result, unknown remote outcome,
zero retries, partial usage and retained stage timings. This shorter synthetic
budget does not change either production or correctness policy. No exception
message, prompt, response or customer payload is retained.

## Correlated observability

```powershell
.venv/Scripts/python.exe scripts/inspect_observability.py --run-id 17000000000000000000000000000001 --scenario-id order_status_001
.venv/Scripts/python.exe scripts/inspect_observability.py --run-id 17000000000000000000000000000002 --scenario-id order_status_001 --json
$trace = Get-Content -Encoding UTF8 -Raw reports/observability/17000000000000000000000000000002/demo-deadline-request.json | ConvertFrom-Json
$trace.events | Where-Object event_type -eq SPAN | Select-Object name,status,request_id,trace_id,span_id,parent_span_id
```

Expected deadline tree: request ERROR, router OK, synthesis ERROR, evaluation
UNAVAILABLE. Retained aggregate tool timing cannot establish the tool name or
status: `agentguard.tool.UNKNOWN` remains UNAVAILABLE. Successful legacy-summary
components also retain unknown statuses. Correlation comes from recorded request,
run and scenario identity; these fixtures contain no invocation receipt, so
`invocation_id` is null. No missing evidence is reconstructed.

## Model/prompt regression

```powershell
.venv/Scripts/python.exe scripts/compare_model_prompt_runs.py --baseline-run 16000000000000000000000000000001 --candidate-run 16000000000000000000000000000002 --baseline-label fixture-v1 --candidate-label fixture-v2
```

Expected: `MODEL_AND_PROMPT_CHANGE`; quality, semantic and safety COMPARABLE;
operational PARTIALLY_COMPARABLE because some measurements are missing.
Functional accuracy falls from 2/3 to 1/3; correctness from 0.9 to 0.7; safety
pass rate from 1 to 0. Latency improves from 100 to 80 ms. Tokens fall from 12 to
8, but their registry direction remains INFORMATIONAL, so no directional winner
is declared. Display labels do not establish identity. New and unchanged failures
are listed per comparable check/repetition.

## Lineage and governance

```powershell
$manifest = Get-Content -Encoding UTF8 -Raw examples/portfolio/evaluations/17000000000000000000000000000001/manifest.json | ConvertFrom-Json
$manifest | Select-Object run_id,execution_mode,scenario_set_fingerprint
$manifest.fingerprints | Format-List
$success.source_integrity | ConvertTo-Json -Depth 4
Get-Content -Encoding UTF8 docs/CONTINUOUS_EVALUATION_BASELINES.md -TotalCount 29
```

Expected: `offline_fixture`, versioned fingerprints and a synthetic MATCH source
record. These illustrate immutable manifests, terminal completion evidence, validated
digests and explicit promotion of reviewed eligible runs. Do not promote these
fixtures or treat their MATCH record as attestation of the current checkout.

## Future integration boundaries

The [future architecture](ENTERPRISE_ARCHITECTURE_V2.md) distinguishes implemented
components (green) from external or proposed components (blue/dashed). AgentGuard would publish
evidence through adapters while existing platforms own workflows. RAG belongs in
a future governed KnowledgeProvider; MCP could support a future authorized
ToolProvider. Neither is implemented. These fixtures do not establish production
adoption or an experiment-management platform.

## Optional live evaluation

See [live commands](../README.md#optional-live-evaluation). Live agent execution
and semantic/safety classifiers may incur provider calls and costs. Supply keys
through the process environment or configured CI secret; never include a key in
a command example or committed artifact. Synthetic semantic scores above are not
current live-judge qualification.
