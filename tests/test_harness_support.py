"""Host provenance is actual selected content, not a borrowed checkout SHA."""
import importlib.util
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = ("agent/memory_provider.py", "agent/memory_manager.py",
         "agent/inline_tool_executors.py", "tools/memory_tool_store.py",
         "plugins/memory/__init__.py")


def support():
    path = ROOT / "scripts/harness_support.py"
    assert path.is_file(), "missing explicit installed-snapshot provenance"
    spec = importlib.util.spec_from_file_location("harness_support_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_host(path):
    for name in FILES:
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# synthetic contract\n")
    return path


def test_installed_snapshot_identity_is_content_bound(tmp_path):
    module = support()
    root = fixture_host(tmp_path / "host")
    first = module.host_provenance(root)
    assert first["kind"] == "installed_snapshot"
    assert first == module.host_provenance(root)
    assert "git_sha" not in first
    assert set(first["files"]) == set(FILES)
    (root / FILES[0]).write_text("# changed contract\n")
    assert first["identity"] != module.host_provenance(root)["identity"]


def test_incomplete_snapshot_refuses(tmp_path):
    module = support()
    root = fixture_host(tmp_path / "host")
    (root / FILES[-1]).unlink()
    with pytest.raises(ValueError, match="missing host contract"):
        module.host_provenance(root)


def test_real_git_host_keeps_revision_and_dirty_state(tmp_path):
    module = support()
    root = fixture_host(tmp_path / "host")
    for command in (["init", "-q"], ["add", "."],
                    ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(root), *command], check=True,
                       capture_output=True, timeout=10)
    value = module.host_provenance(root)
    assert value["kind"] == "git" and len(value["git_sha"]) == 40
    assert value["dirty"] is False
    (root / FILES[0]).write_text("changed\n")
    assert module.host_provenance(root)["dirty"] is True


def fixture_product(path, marker="old"):
    package = path / "zvec-memory"
    package.mkdir(parents=True)
    (package/"__init__.py").write_text(f"MARKER = {marker!r}\n")
    (package/"plugin.yaml").write_text("name: synthetic-fixture\n")
    (package/"README.md").write_text("synthetic shipped documentation\n")
    (path/"scripts").mkdir()
    (path/"scripts/probe.py").write_text("# synthetic harness\n")
    (path/"tests").mkdir()
    (path/"tests/test_probe.py").write_text("# synthetic test oracle\n")
    return path


def test_selected_product_and_harness_have_independent_content_identity(tmp_path, monkeypatch):
    m = support()
    candidate = fixture_product(tmp_path/"candidate", "candidate")
    frozen = fixture_product(tmp_path/"frozen", "baseline")
    monkeypatch.setenv("ZVEC_TEST_PROVIDER_ROOT", str(frozen))
    assert m.provider_source(candidate) == frozen
    assert m.provider_source(candidate, candidate) == candidate
    old = m.product_identity(frozen)
    harness = m.harness_identity(candidate)
    assert "git_sha" not in old and len(old["content_sha256"]) == 64
    assert set(old["files"]) == {"zvec-memory/__init__.py", "zvec-memory/plugin.yaml", "zvec-memory/README.md"}
    (frozen/"zvec-memory/README.md").write_text("changed shipped file\n")
    assert old["identity"] != m.product_identity(frozen)["identity"]
    assert harness == m.harness_identity(candidate)
    (candidate/"scripts/probe.py").write_text("changed instrument\n")
    assert harness["identity"] != m.harness_identity(candidate)["identity"]


def test_selected_product_refuses_escaping_shipped_symlink(tmp_path):
    m = support()
    root = fixture_product(tmp_path/"product")
    outside = tmp_path/"unrelated"
    outside.write_text("unrelated sentinel")
    (root/"zvec-memory/aliased.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        m.product_identity(root)
    assert outside.read_text() == "unrelated sentinel"


def test_product_git_metadata_is_real_and_scoped_to_product(tmp_path):
    m = support()
    root = fixture_product(tmp_path/"repo")
    for args in (["init","-q"],["add","."],["-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","-qm","fixture"]):
        subprocess.run(["git","-C",str(root),*args],check=True,capture_output=True,timeout=10)
    initial = m.product_identity(root)
    assert initial["kind"] == "git" and len(initial["git_sha"]) == 40
    assert initial["dirty"] is False
    (root/"scripts/probe.py").write_text("changed harness only\n")
    assert m.product_identity(root) == initial
    assert m.harness_identity(root)["dirty"] is True
    (root/"zvec-memory/__init__.py").write_text("changed product\n")
    assert m.product_identity(root)["dirty"] is True
    assert m.product_identity(root)["git_sha"] == initial["git_sha"]


@pytest.mark.parametrize("script", ["stress_memory", "soak_memory", "measure_recall"])
def test_workload_imports_selected_product_not_candidate(tmp_path, monkeypatch, script):
    import sys
    candidate = fixture_product(tmp_path/"candidate", "candidate")
    frozen = fixture_product(tmp_path/"frozen", "baseline")
    for root, marker in ((candidate,"candidate"),(frozen,"baseline")):
        (root/"zvec-memory/__init__.py").write_text(
            f"import os\nclass ZvecMemoryProvider:\n marker={marker!r}\n home=os.environ['HERMES_HOME']\n def __init__(self,config=None): pass\n def initialize(self,*a,**k): self.initialized=True\n def shutdown(self): pass\n")
    spec = importlib.util.spec_from_file_location("selected_"+script, ROOT/"scripts"/(script+".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "REPO_ROOT" if script == "measure_recall" else "ROOT", candidate)
    monkeypatch.delitem(sys.modules, "soak_provider", raising=False)
    for number, (root, marker) in enumerate(((frozen,"baseline"),(candidate,"candidate"),(frozen,"baseline"))):
        monkeypatch.setenv("ZVEC_TEST_PROVIDER_ROOT", str(root))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / f"home-{number}"))
        if script == "stress_memory": obj = m.provider(tmp_path, "offline", "fixture")
        elif script == "soak_memory": obj = m.provider_factory(tmp_path, 1)
        else: obj = m.load_provider()()
        try:
            assert obj.marker == marker
            assert obj.home == str(tmp_path / f"home-{number}")
            if script != "measure_recall": assert obj.initialized
        finally: obj.shutdown()


@pytest.mark.parametrize("script", ["stress_memory", "soak_memory", "measure_recall"])
def test_coordinator_records_and_forwards_selected_product(tmp_path, monkeypatch, capsys, script):
    from types import SimpleNamespace
    import json
    frozen = fixture_product(tmp_path/"frozen", "baseline")
    host = fixture_host(tmp_path/"host")
    spec = importlib.util.spec_from_file_location("coordinate_"+script, ROOT/"scripts"/(script+".py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    monkeypatch.setattr(m, "HERMES_AGENT_DIR" if script == "measure_recall" else "HOST", host)
    calls = []
    if script == "stress_memory":
        monkeypatch.setattr(m,"RUNS",tmp_path/"runs")
        def load(run, args, report):
            assert m.environment(run,m.ROOT,args.provider_source)["ZVEC_TEST_PROVIDER_ROOT"] == str(frozen)
            assert report["product_identity"] == m.product_identity(frozen)
            assert report["harness_identity"] == m.harness_identity(ROOT)
            calls.append("work")
            report["workers"]=[{"samples":[{"seconds":0,"phase":name} for name in ("add","replace","remove")]}]
        monkeypatch.setattr(m,"load_campaign",load)
        assert m.main(["--lane","load","--records","1","--provider-source",str(frozen)]) == 0
        record = json.loads(Path(json.loads(capsys.readouterr().out)["report"]).read_text())
    elif script == "soak_memory":
        monkeypatch.setattr(m,"RUNS",tmp_path/"runs")
        package = tmp_path/"engine"
        entry = package/"dist/cli/index.js"
        entry.parent.mkdir(parents=True);entry.write_text("// never executed")
        (package/"package.json").write_text('{"version":"0.2.2"}')
        monkeypatch.setattr(m,"runtime_command",lambda *a: ([str(entry)],{"kind":"fixture"}))
        def supervise(run, command, env, *args):
            assert env["ZVEC_TEST_PROVIDER_ROOT"] == str(frozen)
            calls.append("work")
            return {"passed":True,"errors":[]}
        monkeypatch.setattr(m,"supervise",supervise)
        assert m.main(["--duration","1","--provider-source",str(frozen)]) == 0
        record = json.loads(Path(json.loads(capsys.readouterr().out)["report"]).read_text())
    else:
        real = m.subprocess.run
        def run(command, **kwargs):
            if command[0] in {"node","zg"}: return SimpleNamespace(returncode=0,stdout="fixture",stderr="")
            return real(command,**kwargs)
        monkeypatch.setattr(m.subprocess,"run",run)
        def stop():
            assert m.os.environ["ZVEC_TEST_PROVIDER_ROOT"] == str(frozen)
            calls.append("work")
            raise RuntimeError("stop at native boundary")
        monkeypatch.setattr(m,"load_provider",stop)
        output = tmp_path/"benchmark.json"
        assert m.main(["--output",str(output),"--provider-source",str(frozen)]) == 1
        record = json.loads(output.read_text())["provenance"]
    assert calls == ["work"]
    assert record["product_identity"] == m.product_identity(frozen)
    assert record["harness_identity"] == m.harness_identity(ROOT)
    assert record["host_provenance"]["kind"] == "installed_snapshot"
    assert "plugin_sha" not in record, "a non-Git product must not borrow the instrument's Git revision"
