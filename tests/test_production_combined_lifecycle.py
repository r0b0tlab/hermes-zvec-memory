"""A fault after acquired native instance ownership must release that lock.

The process-local loader injects a synchronous logger construction failure;
no installed or input SDK bytes are edited. This is boundary fault coverage,
not a claim that the real logger normally fails with this error.
"""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.integration
def test_managed_logger_construction_failure_releases_actual_instance_lock(tmp_path):
    plugin = Path(__file__).resolve().parents[1] / 'zvec-memory'
    entry = Path(os.environ['ZVEC_TEST_BIN']).resolve()
    home = tmp_path / 'state'
    token = tmp_path / 'token'
    token.write_text('synthetic-test-token-with-enough-entropy')
    token.chmod(0o600)
    loader = tmp_path / 'logger-fault.mjs'
    loader.write_text(r"""
import {registerHooks} from 'node:module';
import {existsSync} from 'node:fs';
registerHooks({load(url, context, next) {
  const result = next(url, context);
  if (url.endsWith('/dist/daemon/logger.js')) {
    result.source = "import {existsSync} from 'node:fs';\n" + String(result.source).replace('export function createDaemonLogger(home) {',
      `export function createDaemonLogger(home) {
        if (!existsSync(home + '/daemon/instance.lock')) throw Error('fault missed acquisition');
        throw Error('witnessed logger failure after acquisition');`);
  }
  return result;
}});
""")
    proc = subprocess.run(['/usr/bin/node', '--import', str(loader), str(plugin / 'managed-server.mjs'),
                           str(entry), 'server', 'run', '--home', str(home),
                           '--listen', '127.0.0.1:17999', '--token-file', str(token)],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 1
    assert 'witnessed logger failure after acquisition' in proc.stderr
    assert not (home / 'daemon/instance.lock').exists(), 'acquired instance ownership leaked on early failure'
    assert not (home / 'daemon/mcp-request-state.key').exists()
