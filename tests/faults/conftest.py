import pytest

from .harness import install
from .scorecard import summarize


_RESULTS = pytest.StashKey[list]()


@pytest.fixture
def faults(monkeypatch):
    install(monkeypatch)


@pytest.fixture
def fault_results(request):
    return request.config.stash.setdefault(_RESULTS, [])


def pytest_terminal_summary(terminalreporter):
    rows = terminalreporter.config.stash.get(_RESULTS, [])
    if rows:
        terminalreporter.write_sep("-", "Offline fault matrix (executed cases only)")
        for key, value in summarize(rows).items():
            terminalreporter.write_line(f"{key}: {value}")
