import pytest

from .harness import Campaign, assess, install


RESULTS = pytest.StashKey[list]()
OWNERS = pytest.StashKey[list]()
ARTIFACTS = pytest.StashKey[list]()


@pytest.fixture
def campaign(monkeypatch, request):
    install(monkeypatch)
    campaign = Campaign(request.config.stash.setdefault(RESULTS, []), request.config.stash.setdefault(ARTIFACTS, []))
    request.config.stash.setdefault(OWNERS, []).append(campaign)
    yield campaign


def pytest_terminal_summary(terminalreporter):
    rows = terminalreporter.config.stash.get(RESULTS, [])
    if rows:
        terminalreporter.write_sep("-", "Offline concurrency isolation (controlled dispatch only)")
        summary = assess(rows)
        artifacts = terminalreporter.config.stash.get(ARTIFACTS, [])
        summary["artifact_collisions"] = len(artifacts) - len({path for path, _, _ in artifacts})
        summary["artifacts_published"] = len(artifacts)
        for key, value in summary.items():
            terminalreporter.write_line(f"{key}: {value}")
