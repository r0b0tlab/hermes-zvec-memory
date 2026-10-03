/**
 * Plugin-managed publication policy, qualified ONLY for zvec-grep 0.2.2.
 * backend.d.ts explicitly supplies watchManagerFactory; backend.js uses only
 * start/close/flushPending/refreshPaths on its result. No native WatchManager
 * is constructed, no fs watch is registered, and no timers/callbacks dispatch
 * changes. This is constructor injection, not an SDK/global filesystem patch.
 * Upstream's watcherActive flag describes the installed sentinel, NOT an OS
 * watcher; managedPolicy reports the actual policy separately.
 *
 * Native index/drop remain authenticated manual operations. The plugin's
 * _index_job is the supported publisher; same-UID token holders/operators can
 * still deliberately index outside that protocol. This is not a sandbox.
 */
export const MANAGED_POLICY = 'plugin-publication-v1';

export function publicationOnlyWatcher() {
    return Object.freeze({
        start() {},
        async close() {},
        async flushPending() {},
        async refreshPaths() {},
    });
}

export function managedBackend(DaemonBackend, options) {
    const backend = new DaemonBackend({ ...options, watchManagerFactory: publicationOnlyWatcher });
    // Off suppresses only one upstream branch; the injection suppresses all
    // watcher branches. Reject wait/background search policies as well, so an
    // authenticated query cannot autonomously submit a reconciliation job.
    const search = backend.search.bind(backend);
    backend.search = (input, callOptions) => {
        if (input.autoUpdate !== false || input.freshness !== 'eventual') {
            throw new Error('Managed publication requires query refresh off');
        }
        return search(input, callOptions);
    };
    const status = backend.serverStatus.bind(backend);
    backend.serverStatus = async () => ({ ...await status(), managedPolicy: MANAGED_POLICY,
                                          filesystemWatcher: false });
    return backend;
}
