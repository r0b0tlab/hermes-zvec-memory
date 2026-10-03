"""Standalone test-owned peer: real provider, rejected scheduling or cold recall."""
import importlib
import json
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'tests'))
from test_provider import ZvecMemoryProvider, _mod

action, raw_config, receipt = sys.argv[1:]
config = json.loads(raw_config)
denials = []
if action == 'store-rejected':
    workers = importlib.import_module(_mod.__name__ + '.workers')
    def reject(self, *args, **kwargs):
        denials.append(self._thread.name)
        return False
    workers.Worker.submit = reject
p = ZvecMemoryProvider(config=config)
try:
    p.initialize('independent-peer', hermes_home=os.environ['HERMES_HOME'])
    if action == 'store-rejected':
        response = json.loads(p._handle_store({'content': 'The synthetic telescope needs an indigo parachute.'}))
        assert response['status'] == 'stored', response
        assert (p._vault / '.index-refresh.json').exists()
        assert p._recall_token() is None
        value = {'response': response, 'denials': denials, 'refresh': (p._vault / '.index-refresh.json').read_text()}
        assert denials
    elif action == 'cold-read':
        assert p._index_worker.drain(30)
        assert p._disk_worker.drain(10)
        response = p.prefetch('What emergency equipment does the synthetic telescope need?')
        assert 'indigo parachute' in response
        assert 'facts/' in response
        value = {'response': response, 'generation': p._index_generation(), 'token': p._recall_token(),
                 'inbox_pending': p._mirror_inbox.pending(), 'map': p._mirror_state()}
        assert value['token'] is not None and value['inbox_pending'] == 0
    else:
        raise ValueError('unsupported peer action')
finally:
    p.shutdown()
    assert not p._index_worker._thread.is_alive()
    assert not p._disk_worker._thread.is_alive()
Path(receipt).write_text(json.dumps(value, indent=2))
