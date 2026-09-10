import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def offline_engine(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return
    # Never launch native/model work in the offline lane, even if zg exists.
    from test_provider import ZvecMemoryProvider
    monkeypatch.setattr(ZvecMemoryProvider, "_run_zg", lambda *a, **k: (0, "", ""))
