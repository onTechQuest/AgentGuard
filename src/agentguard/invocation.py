"""Exact invocation handoff, independent of evaluation artifact finalization."""
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import json
import re
import sys
from inspect import unwrap
from uuid import uuid4

_active = ContextVar("evaluation_invocation", default=None)


def error_type(error):
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", name) else "UNKNOWN"


class Invocation:
    def __init__(self, root, suite, mode, release):
        invocation_id = uuid4().hex
        self.path = Path(root) / "reports/continuous_evaluation/invocations" / invocation_id / "receipt.json"
        self.path.parent.mkdir(parents=True, exist_ok=False)
        self.document = dict(invocation_schema_version=1, invocation_id=invocation_id,
            created_at=datetime.now(timezone.utc).isoformat(), suite=suite, requested_mode=mode,
            requested_release_qualification=release, process_state="CREATED", process_exit_code=None,
            evaluation_run_id=None, evaluation_artifact_path=None,
            linked_release_run_id=None, linked_release_artifact_path=None,
            comparison_id=None, baseline_id=None, setup_error_type=None, finalization_error_type=None)
        self.save()

    def save(self, **changes):
        self.document.update(changes)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.document, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def link(self, run):
        prefix = "linked_release" if run.manifest.to_dict()["suite"] == "structural" else "evaluation"
        self.save(**{prefix + "_run_id": run.manifest.run_id, prefix + "_artifact_path": str(run.path.resolve())})


def current_invocation():
    return _active.get()


def setup_failure(error):
    receipt = current_invocation()
    if receipt and not receipt.document["evaluation_run_id"] and not receipt.document["linked_release_run_id"]:
        receipt.save(setup_error_type=error_type(error))


def finalization_failure(error):
    if current_invocation():
        current_invocation().save(finalization_error_type=error_type(error))


def invocation_entry(*, suite, mode="live"):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if current_invocation() is not None:
                return function(*args, **kwargs)
            namespace = unwrap(function).__globals__
            root = kwargs.get("project_root") or namespace.get("PROJECT_ROOT") or namespace.get("ROOT")
            argv = kwargs.get("argv", args[0] if args else None)
            argv = list(sys.argv[1:] if argv is None else argv)
            requested_suite = suite
            for index, argument in enumerate(argv):
                candidate = argv[index + 1] if argument == "--suite" and index + 1 < len(argv) else argument.partition("--suite=")[2]
                if candidate in {"smoke", "full", "performance", "reliability", "structural"}:
                    requested_suite = candidate
            receipt = Invocation(root, requested_suite, mode, "--release-qualification" in argv)
            token = _active.set(receipt)
            print(f"Invocation receipt: {receipt.path}")
            receipt.save(process_state="RUNNING")
            try:
                result = function(*args, **kwargs)
            except BaseException as error:
                cancelled = isinstance(error, KeyboardInterrupt) or type(error).__name__ == "CancelledError"
                code = (error.code if type(error.code) is int else 1) if isinstance(error, SystemExit) else 130 if cancelled else 1
                setup_failure(error)
                state = "CANCELLED" if cancelled else "COMPLETED" if code == 0 else (
                    "FAILED" if receipt.document["evaluation_run_id"] or receipt.document["linked_release_run_id"] else "SETUP_FAILED")
                receipt.save(process_state=state, process_exit_code=code)
                raise
            else:
                code = result if type(result) is int else 0
                state = "COMPLETED" if code == 0 else "FAILED" if (
                    receipt.document["evaluation_run_id"] or receipt.document["linked_release_run_id"]) else "SETUP_FAILED"
                receipt.save(process_state=state, process_exit_code=code)
                return result
            finally:
                _active.reset(token)
        return wrapped
    return decorate
