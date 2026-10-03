#!/usr/bin/env node
/**
 * SPDX-License-Identifier: Apache-2.0
 * Adapted/modified for this plugin from zvec-ai/zvec-grep 0.2.2
 * dist/daemon/runtime.js (https://github.com/zvec-ai/zvec-grep).
 * See managed-server-LICENSE.txt for the upstream Apache-2.0 license.
 * Changes: version/source admission, watcher constructor injection, restricted
 * foreground options, mandatory authentication and independent cleanup.
 *
 * Private version-bound adapter for the shipped 0.2.2 daemon modules, which
 * are not package-exported public API. Reviewed source digests fail closed
 * before importing them. A future engine release needs a new qualification.
 * Lifecycle derives from 0.2.2 daemon/runtime.js; only backend construction
 * changes. No installed SDK edits; upstream auth, instance lock, MCP codecs,
 * HTTP transport and warm model/runtime pools remain in use.
 */
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { dirname, isAbsolute, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { managedBackend } from './managed-policy.mjs';

const PIN = Object.freeze({
    'dist/daemon/backend.js': '8a45e014759235f8b333b7cdb2feb02ed3ba3b5dba0f3326748a0c041353b522',
    'dist/daemon/backend.d.ts': '0e838cf46121220a569da12baa2aa35f41411347150d794e845daf9be4b9a4d7',
    'dist/daemon/watch-manager.d.ts': '9197a71bf6d66d19cf7c40c6b11a508fe821b5108e119c9a918720e82d0893a7',
    'dist/daemon/runtime.js': 'e656114554e99341dc5aa47140b03d0322101546aec56343ccd3c0032e76b486',
    'dist/daemon/config.js': '8524b1f607c15e82971312dde5ecc5db2f1b5e43f9ba3041b2437bb85c70039d',
    'dist/daemon/http-server.js': 'be175c4809fd1e36a295ec77aa575ff887bb53b8617cd29ef5ce0dd2a36ed6f7',
    'dist/daemon/server-controller.js': '8a7b1bc87e01add060ef9520ab30640a69a4b99ec0e6d0ca7c9974c3a9a4a86b',
    'dist/daemon/logger.js': '475d1dd10c6a540996e2010ad54d3a8e0382223e1c9263f39c66b09c8ac881bc',
    'dist/mcp/request-state.js': 'e82c8752866f6700d3453b18e619d1259914baed7981d68283bd033d7dbf50f2',
    'dist/mcp/toolset.js': 'c8d46985e8bb4a86f35505a865bb92207d94a2ac9639904ec7e017f3e230dfc7',
    'dist/cli/args.js': '0437f0411a8b3a87fe254a10eb561a9d30ab75c754f268ec3e5aca7e05eef3ff',
});

async function main() {
    const entry = process.argv[2];
    if (!entry || !isAbsolute(entry)) throw new Error('Managed server requires absolute engine entry');
    const pkg = resolve(dirname(entry), '../..');
    const metadata = JSON.parse(await readFile(resolve(pkg, 'package.json'), 'utf8'));
    if (metadata.name !== '@zvec/zvec-grep' || metadata.version !== '0.2.2') {
        throw new Error('Managed server requires pinned engine 0.2.2');
    }
    for (const [file, digest] of Object.entries(PIN)) {
        const bytes = await readFile(resolve(pkg, file));
        if (createHash('sha256').update(bytes).digest('hex') !== digest) {
            throw new Error('Managed server source mismatch: ' + file);
        }
    }
    const args = process.argv.slice(3);
    // Only the foreground service contract and explicitly implemented options
    // are supported. Do not silently ignore upstream runtime/model overrides.
    if (args[0] !== 'server' || args[1] !== 'run') {
        throw new Error('Managed server supports foreground server run only');
    }
    const allowed = new Set(['--listen', '--token-file', '--home', '--mcp-toolset']);
    for (let i = 2; i < args.length; i += 2) {
        if (!allowed.has(args[i]) || args[i + 1] === undefined) {
            throw new Error('Unsupported managed server option');
        }
    }
    const load = (file) => import(pathToFileURL(resolve(pkg, file)).href);
    const { parseArgs } = await load('dist/cli/args.js');
    const parsed = parseArgs(args);
    if (parsed.command !== 'server' || parsed.options.serverAction !== 'run' ||
        parsed.options.serverStdio || parsed.positionals.length) {
        throw new Error('Managed server supports foreground server run only');
    }
    const { DaemonBackend } = await load('dist/daemon/backend.js');
    const { configuredListenAddress, configuredWatcherIdleTimeoutMs, resolveServerToken } = await load('dist/daemon/config.js');
    const { DaemonHttpServer } = await load('dist/daemon/http-server.js');
    const { DaemonInstanceLock } = await load('dist/daemon/server-controller.js');
    const { createDaemonLogger } = await load('dist/daemon/logger.js');
    const { MCP_TOOLSET_ENV, resolveMcpToolset } = await load('dist/mcp/toolset.js');
    const { loadOrCreateMcpRequestStateKey, PersistentRemoteEmbeddingRequestStateReplayGuard } = await load('dist/mcp/request-state.js');
    const options = parsed.options;
    const home = options.home ?? process.env.ZVEC_GREP_HOME;
    const mcpToolset = resolveMcpToolset(options.mcpToolset, process.env[MCP_TOOLSET_ENV]);
    const listen = configuredListenAddress(options.listen);
    const host = listen.host.includes(':') ? `[${listen.host}]` : listen.host;
    const displayAddress = `http://${host}:${listen.port}/mcp`;
    // Unlike upstream, authentication is mandatory for this managed topology.
    const auth = await resolveServerToken({ tokenFile: options.serverTokenFile, home });
    if (!auth.token) throw new Error('Managed server requires a bearer token file');
    const instanceLock = await DaemonInstanceLock.acquire(home, displayAddress, mcpToolset);
    let logger, backend, httpServer;
    let stopRequested = false;
    let notifyStop;
    const stopped = new Promise((done) => { notifyStop = done; });
    const stop = () => { stopRequested = true; notifyStop(); };
    // Register before startup so TERM while starting remains a cleanup request.
    process.once('SIGINT', stop);
    process.once('SIGTERM', stop);
    try {
        logger = createDaemonLogger(home);
        const requestStateKey = await loadOrCreateMcpRequestStateKey(home);
        backend = managedBackend(DaemonBackend, { version: '0.2.2',
            runtimeIdleTtlMs: configuredWatcherIdleTimeoutMs(), logger });
        httpServer = new DaemonHttpServer({ ...listen, token: auth.token, version: '0.2.2', mcpToolset,
            backend, requestStateKey,
            requestStateReplayGuard: new PersistentRemoteEmbeddingRequestStateReplayGuard(home),
            logger, onShutdown: stop });
        if (stopRequested) return;
        await httpServer.start();
        if (stopRequested) return;
        await instanceLock.markReady();
        logger.event('server.ready', { host: listen.host, port: listen.port, pid: process.pid,
                                      managed_policy: 'plugin-publication-v1', filesystem_watcher: false });
        console.log('Managed zvec-grep server listening on ' + displayAddress);
        await stopped;
    } finally {
        process.off('SIGINT', stop);
        process.off('SIGTERM', stop);
        // Each cleanup is attempted even if another fails; failure stays visible.
        const errors = [];
        for (const cleanup of [() => backend?.close(), () => httpServer?.close(),
                               () => instanceLock.release(), () => logger?.flush()]) {
            try { await cleanup(); } catch (error) { errors.push(error); }
        }
        if (errors.length) throw new AggregateError(errors, 'Managed server cleanup failed');
    }
}

main().catch((error) => {
    // No token/config content on the public diagnostic surface.
    console.error(error instanceof SyntaxError ? 'Invalid managed server metadata' : error.message);
    process.exitCode = 1;
});
