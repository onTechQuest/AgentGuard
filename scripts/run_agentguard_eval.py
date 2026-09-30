"""Run AgentGuard end to end: python scripts/run_agentguard_eval.py --suite smoke."""

import argparse
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agentguard.evaluation_record import execute_scenario
from src.agentguard.failure_evidence import retain_failure
from src.agentguard.datasets import DatasetValidationError, RISK_LEVELS, SUITES, TEST_INTENTS, load_datasets
from src.agentguard.quality_gate import evaluate_quality_gate, load_quality_gate_config
from src.agentguard.scorecard import build_scorecard
from src.agentguard.scoring import evaluate_record
from src.agentguard.semantic_evaluator import evaluate_semantics
from src.agentguard.safety_evaluator import safety_evaluate_record
from src.agentguard.safety_incidents import SafetyIncidentRecorder
from src.agentguard.lineage import lineage_entry, start_run
from src.agentguard.invocation import invocation_entry, setup_failure
from src.agentguard.correctness_execution import load_correctness_policy


GATE_LABELS = {
    "functional_accuracy": "Functional Accuracy",
    "tool_accuracy": "Tool Accuracy",
    "argument_accuracy": "Argument Accuracy",
    "p95_latency_ms": "P95 Latency",
    "average_tokens_per_run": "Functional Production Token Usage",
    "failed_scenarios": "Failed Scenarios",
    "answer_relevancy": "Answer Relevancy",
    "correctness": "Correctness",
    "hallucination_score": "Hallucination Score",
    "semantic_pass_rate": "Semantic Pass Rate",
    "safety_pass_rate": "Safety Pass Rate",
    "prompt_injection_failures": "Prompt Injection Failures",
    "unsupported_action_failures": "Unsupported Action Failures",
    "data_protection_failures": "Data Protection Failures",
    "tool_policy_failures": "Tool Policy Failures",
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate AgentGuard and apply release quality gates.")
    parser.add_argument(
        "--suite", choices=(*SUITES, "performance", "reliability"), default="smoke",
        help="smoke/full: correctness; performance: latency qualification; reliability: explicit candidate/shadow qualification",
    )
    parser.add_argument("--repetitions", type=int, help="Qualification repetitions (performance default 5; reliability uses configuration)")
    parser.add_argument("--report", type=Path, help="Qualification JSON report path")
    parser.add_argument("--release-qualification", action="store_true", help="Enforce the 16 structural gates using fresh offline qualification evidence")
    parser.add_argument("--structural-evidence", type=Path, help="Current-revision offline production transport evidence")
    parser.add_argument("--release-report", type=Path, help="New release bundle output path (never overwritten)")
    parser.add_argument("--qualification-config", type=Path, help="Reliability only: explicit candidate configuration JSON")
    parser.add_argument("--execution-candidate", help="Reliability only: select the named execution candidate")
    parser.add_argument("--execute-retries", action="store_true", help="Reliability only: explicitly enable the selected retry candidate")
    parser.add_argument("--enforce-candidate-budget", action="store_true",
                        help="Reliability only: explicitly enforce experimental request/stage budgets; no retries")
    args = parser.parse_args(argv)
    if args.release_qualification and args.suite not in SUITES:
        parser.error("Release qualification requires smoke/full correctness mode")
    if not args.release_qualification and (args.structural_evidence or args.release_report):
        parser.error("Structural evidence/output require --release-qualification")
    if args.suite not in {"performance", "reliability"} and (args.repetitions is not None or args.report is not None):
        parser.error("--repetitions and --report require a qualification suite")
    if args.suite == "performance" and args.repetitions is not None and args.repetitions < 5:
        parser.error("Performance qualification requires at least five repetitions")
    if args.suite != "reliability" and (args.qualification_config or args.execution_candidate or args.execute_retries or args.enforce_candidate_budget):
        parser.error("Reliability policy options require --suite reliability")
    if args.enforce_candidate_budget and args.execute_retries:
        parser.error("Candidate budget qualification cannot enable retries")
    if args.suite == "reliability":
        if args.qualification_config is None:
            parser.error("Reliability requires --qualification-config")
        if args.repetitions is not None and args.repetitions < 1:
            parser.error("Reliability repetitions must be positive")
    return args


def print_production_usage(label, usage, threshold):
    print(f"\n{label} PRODUCTION USAGE")
    print(f"Execution Count: {usage.execution_count}")
    print(f"Latency Observations: {len(usage.latency_observations)}")
    for name, value in (("Average Latency", usage.average_latency_ms), ("Maximum Latency", usage.maximum_latency_ms)):
        print(f"{name}: {value:.0f} ms" if value is not None else f"{name}: unavailable")
    tokens = usage.average_tokens_per_execution
    print(f"Average Production Tokens/Execution: {tokens:g}" if tokens is not None else "Average Production Tokens/Execution: unavailable")
    print(f"Token Observations: {usage.token_observation_count}")
    if usage.p95_latency_ms is not None:
        print(f"Observed P95 Latency: {usage.p95_latency_ms:.0f} ms "
              "(report-only; correctness sample is insufficient for release latency qualification)")
    exceeded = usage.exceeding(threshold)
    print(f"Observations > {threshold:g} ms: {len(exceeded)}")
    for observation in exceeded:
        print(f"  {observation.scenario_id}: {observation.latency_ms:.0f} ms")


@invocation_entry(suite="smoke")
@lineage_entry
def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    print("AGENTGUARD EVALUATION", flush=True)
    print(f"Evaluation Suite: {args.suite.upper()}", flush=True)
    if args.suite == "reliability":
        from dataclasses import replace
        from src.agentguard.reliability_policy import load_qualification_config
        from src.agentguard.reliability import qualify, print_summary
        try:
            config = load_qualification_config(args.qualification_config)
            config = replace(config, repetitions=args.repetitions if args.repetitions is not None else config.repetitions,
                             execution_candidate=args.execution_candidate or config.execution_candidate)
            datasets = load_datasets(PROJECT_ROOT / "evals/datasets", suite=config.dataset_suite)
            report = qualify(config, datasets, project_root=PROJECT_ROOT,
                             output=args.report or PROJECT_ROOT / "reports/reliability_qualification.json",
                             execute_retries=args.execute_retries,
                             enforce_candidate_budget=args.enforce_candidate_budget)
        except Exception as error:
            setup_failure(error)
            # Configuration/provider error messages can contain sensitive input.
            print(f"Reliability qualification incomplete ({type(error).__name__}). Check configuration and report destination.")
            return 1
        print_summary(report)
        return 0  # Measurement completion, not a quality-gate or policy approval.
    if args.suite == "performance":
        from scripts.audit_latency import main as performance_main
        return performance_main([
            "--qualify", "--repetitions", str(args.repetitions or 5),
            "--output", str(args.report or PROJECT_ROOT / "reports/performance_qualification.json"),
        ], project_root=PROJECT_ROOT)
    try:
        datasets = load_datasets(PROJECT_ROOT / "evals/datasets", suite=args.suite)
        scenarios, safety_scenarios = datasets.functional, datasets.safety
        config = load_quality_gate_config(PROJECT_ROOT / "config/quality-gates.yaml")
        execution_policy = load_correctness_policy()
        for scenario in scenarios:
            if not isinstance(scenario["expected_output"], str):
                raise ValueError("expected_output must be a string")
    except Exception as error:
        setup_failure(error)
        print(f"Evaluation setup failed ({type(error).__name__}).")
        if isinstance(error, DatasetValidationError):
            print(str(error))
        print("Check both datasets, functional expected_output fields, and quality gate YAML.\nFINAL DECISION: FAIL")
        return 1

    lineage = start_run(PROJECT_ROOT, suite=args.suite, functional=scenarios, safety=safety_scenarios,
        effective_runtime_policy=execution_policy.snapshot(), execution_profile=execution_policy.profile_identity())
    lineage.execution_policy = execution_policy
    records_and_scores = []
    semantic_scores = []
    for scenario in scenarios:
        stage = "execution"
        record = None
        try:
            record = execute_scenario(scenario)
            lineage.capture(scenario, record=record)
            if record.execution_error is not None:
                retain_failure(scenario, record=record, stage=stage)
                print(f"\nScenario {scenario['id']}: {record.execution_error}.")
                print("Evaluation incomplete.\nFINAL DECISION: FAIL")
                return 1
            stage = "deterministic evaluation"
            score = evaluate_record(scenario, record)
            lineage.capture(scenario, deterministic=score)
            stage = "semantic evaluation"
            semantic_score = evaluate_semantics(record, scenario["expected_output"])
            lineage.capture(scenario, semantic=semantic_score)
        except BaseException as error:
            retain_failure(scenario, error, record=record, stage=stage)
            if not isinstance(error, Exception):
                raise
            # API exception messages can include request or credential details.
            print(f"\nScenario {scenario['id']}: {stage} failed ({type(error).__name__}).")
            print("Evaluation incomplete.\nFINAL DECISION: FAIL")
            return 1
        records_and_scores.append((record, score))
        semantic_scores.append(semantic_score)

    safety_scores = []
    safety_records = []
    incidents = SafetyIncidentRecorder(PROJECT_ROOT)
    for scenario in safety_scenarios:
        stage = "execution"
        record = None
        try:
            record = execute_scenario(scenario)
            lineage.capture(scenario, record=record)
            if record.execution_error is not None:
                retain_failure(scenario, record=record, stage=stage)
                incidents.retain(scenario, record, stage=stage)
                print(f"\nScenario {scenario['id']}: {record.execution_error}.")
                print("Evaluation incomplete.\nFINAL DECISION: FAIL")
                return 1
            safety_records.append(record)
            stage = "safety evaluation"
            score = safety_evaluate_record(scenario, record)
            lineage.capture(scenario, safety=score)
            safety_scores.append(score)
            incidents.retain(scenario, record, score)
        except BaseException as error:
            retain_failure(scenario, error, record=record, stage=stage)
            if not isinstance(error, Exception):
                raise
            incidents.retain(scenario, record, error=error, stage=stage)
            print(f"\nScenario {scenario['id']}: {stage} failed ({type(error).__name__}).")
            print("Evaluation incomplete.\nFINAL DECISION: FAIL")
            return 1

    scorecard = build_scorecard(
        records_and_scores, semantic_scores=semantic_scores, safety_scores=safety_scores, safety_records=safety_records,
    )
    gate = evaluate_quality_gate(scorecard, config, latency_mode="report_only")
    from dataclasses import asdict
    lineage.aggregate.update(quality_result=asdict(gate))
    lineage.evaluation_complete = True

    executed = [*scenarios, *safety_scenarios]
    print("\nCOVERAGE")
    print(f"Scenarios Executed: {len(executed)}")
    print(f"Functional: {len(scenarios)}")
    print(f"Safety: {len(safety_scenarios)}")
    print("Scenarios by Category:")
    for category, count in sorted(Counter(scenario["category"] for scenario in executed).items()):
        print(f"  {category}: {count}")
    print("Scenarios by Risk Level:")
    risks = Counter(scenario["risk"] for scenario in executed)
    for risk in RISK_LEVELS:
        print(f"  {risk}: {risks[risk]}")
    print("Scenarios by Test Intent:")
    intents = Counter(scenario["test_intent"] for scenario in executed)
    for intent in TEST_INTENTS:
        print(f"  {intent}: {intents[intent]}")

    print("\nSCENARIOS")
    print(f"Total: {scorecard.total_scenarios}")
    print(f"Passed: {scorecard.passed_scenarios}")
    print(f"Failed: {scorecard.failed_scenarios}")
    print("\nDETERMINISTIC QUALITY")
    print(f"Functional Accuracy: {scorecard.functional_accuracy:.1%}")
    print(f"Tool Accuracy: {scorecard.tool_accuracy:.1%}")
    print(f"Argument Accuracy: {scorecard.argument_accuracy:.1%}")
    print("\nSEMANTIC QUALITY")
    for label, value in (
        ("Average Answer Relevancy", scorecard.average_answer_relevancy),
        ("Average Correctness", scorecard.average_correctness),
        ("Average Hallucination/Faithfulness", scorecard.average_hallucination_score),
    ):
        print(f"{label}: {value:.3f}" if value is not None else f"{label}: unavailable")
    rate = scorecard.semantic_pass_rate
    print(f"Semantic Pass Rate: {rate:.1%}" if rate is not None else "Semantic Pass Rate: unavailable")
    print("\nSAFETY QUALITY")
    safety_rate = scorecard.safety_pass_rate
    print(f"Safety Pass Rate: {safety_rate:.1%}" if safety_rate is not None else "Safety Pass Rate: unavailable")
    print(f"Prompt Injection Failures: {scorecard.prompt_injection_failures}")
    print(f"Unsupported Action Failures: {scorecard.unsupported_action_failures}")
    print(f"Data Protection Failures: {scorecard.data_protection_failures}")
    print(f"Tool Policy Failures: {scorecard.tool_policy_failures}")
    print("\nPERFORMANCE")
    print("Correctness workload observations; use --suite performance for latency qualification.")
    latency_threshold = config["quality_gates"]["p95_latency_ms"]["maximum"]
    print_production_usage("FUNCTIONAL", scorecard.functional_production_usage, latency_threshold)
    print_production_usage("SAFETY", scorecard.safety_production_usage, latency_threshold)

    printed_diagnostics = False
    for (_, score), semantic in zip(records_and_scores, semantic_scores):
        reasons = list(score.failures)
        for label, passed, reason in (
            ("Answer Relevancy", semantic.answer_relevancy_pass, semantic.answer_relevancy_reason),
            ("Correctness", semantic.correctness_pass, semantic.correctness_reason),
            ("Hallucination/Faithfulness", semantic.hallucination_pass, semantic.hallucination_reason),
        ):
            if passed is not True:
                status = "SKIPPED" if passed is None else "FAIL"
                reasons.append(f"{label} {status}: {reason or 'No reason provided.'}")
        if reasons:
            if not printed_diagnostics:
                print("\nSCENARIO DIAGNOSTICS")
                printed_diagnostics = True
            print(score.scenario_id)
            for failure in reasons:
                print(f"  - {failure}")

    for score in safety_scores:
        if not score.passed or score.prompt_injection_disagreement:
            if not printed_diagnostics:
                print("\nSCENARIO DIAGNOSTICS")
                printed_diagnostics = True
            print(f"{score.scenario_id} (safety)")
            for failure in score.failures:
                print(f"  - {failure}")
            if score.prompt_injection_disagreement:
                print(f"  - {score.prompt_injection_diagnostic}")

    print("\nQUALITY GATES")
    for check in gate.checks:
        if check.get("enforced") is False:
            print(f"{GATE_LABELS[check['metric']]} REPORT ONLY: {check['reason']} "
                  f"Threshold remains {check['comparison']} {check['threshold']:g} ms.")
            continue
        status = "PASS" if check["passed"] else "FAIL"
        print(f"{GATE_LABELS[check['metric']]} {status}")
    for failure in gate.failures:
        print(f"  - {failure}")
    if args.release_qualification:
        from scripts.qualify_release import assemble_release
        report = assemble_release(PROJECT_ROOT,
            structural_path=args.structural_evidence or PROJECT_ROOT/'reports/release_13e4/transport.json',
            quality_result=gate, deployment={'active': False, 'source': 'sequential smoke/full runner'},
            records=[*(record for record, _ in records_and_scores), *safety_records],
            slo_path=PROJECT_ROOT/'reports/slo_13e2/scorecard_final.json',
            cost_path=PROJECT_ROOT/'reports/cost_13e1/baseline.json',output=args.release_report,
            evaluation_lineage=lineage.reference)
        print(f"FINAL DECISION: {report['decision']}")
        return report['exit_code']
    print(f"\nFINAL DECISION: {'PASS' if gate.passed else 'FAIL'}")
    return 0 if gate.passed else 1


if __name__ == "__main__":
    sys.exit(main())
