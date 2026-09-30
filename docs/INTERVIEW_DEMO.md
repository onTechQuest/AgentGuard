# Ten-minute offline interview demo

Run from the repository root in Windows PowerShell with the virtual environment
and dependencies already installed. This demo reads committed **synthetic**
evidence. It calls no provider, judge, business tool or evaluation runner, and
needs no `OPENAI_API_KEY`. Install dependencies before the interview; do not spend
demo time running the full test suite.

## Prepare once before the interview

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

## 0–1 minute: the problem

```powershell
Get-Content -Encoding UTF8 README.md -TotalCount 17
```

Explain: “A plausible answer can still use the wrong tool, wrong arguments, leak
data, contradict a tool result, or miss its deadline. AgentGuard combines explicit
contracts and semantic evidence so a team can explain both a decision and its
limitations.”

## 1–2 minutes: architecture

```powershell
Get-Content -Encoding UTF8 docs/ARCHITECTURE.md -TotalCount 28
```

Show the rendered [README diagram](../README.md#how-it-fits-together). Point out
execution, deterministic/semantic/safety evaluation, evidence, gates, and separate
descriptive comparisons. The registry does not silently control release decisions.

## 2–4 minutes: inspect evaluation evidence

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

## 4–5 minutes: quality gates and CI

```powershell
$success.aggregate_results.quality_result | ConvertTo-Json -Depth 5
$candidate.aggregate_results.quality_result | ConvertTo-Json -Depth 5
Get-Content -Encoding UTF8 .github/workflows/agentguard.yml
```

Expected: illustrative PASS versus FAIL; the candidate functional-accuracy check
retains its actual value, operator and threshold. Gate requirements are not
recomputed by this demo. The actual CI workflow runs offline tests, then a
credentialed live smoke/release command; a nonzero required-gate result blocks the
job. Do not trigger that live step during the interview. The M16 fixture's retained
threshold is historical synthetic evidence, not a replacement for current YAML.

## 5–6 minutes: retained failure evidence

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

## 6–7 minutes: correlated observability

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

## 7–8 minutes: model/prompt regression

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

## 8–9 minutes: lineage and governance

```powershell
$manifest = Get-Content -Encoding UTF8 -Raw examples/portfolio/evaluations/17000000000000000000000000000001/manifest.json | ConvertFrom-Json
$manifest | Select-Object run_id,execution_mode,scenario_set_fingerprint
$manifest.fingerprints | Format-List
$success.source_integrity | ConvertTo-Json -Depth 4
Get-Content -Encoding UTF8 docs/CONTINUOUS_EVALUATION_BASELINES.md -TotalCount 29
```

Expected: `offline_fixture`, versioned fingerprints and a synthetic MATCH source
record. Explain immutable manifests, terminal completion evidence, validated
digests and explicit promotion of reviewed eligible runs. Do not promote these
fixtures or treat their MATCH record as attestation of the current checkout.

## 9–10 minutes: enterprise direction

```powershell
Get-Content -Encoding UTF8 docs/ENTERPRISE_ARCHITECTURE_V2.md -TotalCount 43
```

Show that document rendered in GitHub or a Mermaid-capable Markdown preview.
Green is implemented; blue/dashed is external or future. AgentGuard would publish
evidence through adapters while existing platforms own workflows. RAG belongs in
a future governed KnowledgeProvider; MCP could support a future authorized
ToolProvider. Neither is implemented. Finish with the honest boundary: a tested
reference framework, not claimed production adoption or an experiment platform.
