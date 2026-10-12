import pytest


@pytest.fixture(autouse=True)
def simq_home(tmp_path, monkeypatch):
    """Every test gets its own empty ~/.simq."""
    home = tmp_path / "simq-home"
    monkeypatch.setenv("SIMQ_HOME", str(home))
    return home
