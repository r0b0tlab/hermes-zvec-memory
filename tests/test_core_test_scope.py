"""The release scope is explicit, source-bound, and does not hide mixed files."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location('core_test_scope', ROOT / 'scripts/core_test_scope.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest():
    return json.loads((ROOT / 'tests/reduced_core_scope.json').read_text())


def test_core_scope_retains_exact_original_obligations_and_sources():
    module = _load()
    data = _manifest()
    counts = module.validate(ROOT, data)
    assert counts == {'successor-required': 120, 'deferred': 57, 'asset-replay': 2}
    assert len(data['cases']) == len({x['original_nodeid'] for x in data['cases']}) == 179
    for row in data['cases']:
        path = ROOT / row['source_binding']['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['source_binding']['sha256']


def test_core_scope_emits_exact_deselections_not_file_exclusions(capsys):
    module = _load()
    data = _manifest()
    args = module.pytest_arguments(ROOT, data)
    assert args[:3] == ['tests', '-m', 'not integration']
    assert not any(x.startswith('--ignore') for x in args)
    excluded = [args[i + 1] for i, value in enumerate(args) if value == '--deselect']
    assert len(excluded) == len(set(excluded)) == 177
    assert 'tests/test_provider.py::test_cold_backup_resolves_profile_paths[${HERMES_HOME}/external]' in excluded
    for case in data['cases']:
        assert (case['original_nodeid'] in excluded) == (case['disposition'] != 'asset-replay')
    assert module.main([]) == 0
    assert json.loads(capsys.readouterr().out) == args


@pytest.mark.parametrize('damage', ['duplicate', 'missing', 'source-hash', 'unknown', 'missing-successor', 'parameter-nodeid'])
def test_core_scope_fails_closed_on_incomplete_or_unbound_selection(damage):
    module = _load()
    data = copy.deepcopy(_manifest())
    if damage == 'duplicate':
        data['cases'][1]['original_nodeid'] = data['cases'][0]['original_nodeid']
    elif damage == 'missing':
        data['cases'].pop()
    elif damage == 'source-hash':
        data['cases'][0]['source_binding']['sha256'] = '0' * 64
    elif damage == 'unknown':
        data['cases'][0]['disposition'] = 'silently-ignored'
    elif damage == 'parameter-nodeid':
        row = next(x for x in data['cases'] if '${HERMES_HOME}/external' in x['original_nodeid'])
        row['original_nodeid'] = row['original_nodeid'].replace('${HERMES_HOME}/external', 'different-literal-parameter')
    else:
        row = next(x for x in data['cases'] if x['disposition'] == 'successor-required')
        row['successor_mapping'] = []
    with pytest.raises(ValueError):
        module.validate(ROOT, data)
