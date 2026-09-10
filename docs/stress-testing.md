# Stress and soak testing the zvec-memory provider

This harness answers one question: what does this provider actually sustain, and where does it
fail? Every run uses synthetic data in disposable vaults. Nothing in this document touches a
user vault, and no lane is allowed to reach the production service.

Measured results live in `docs/stress-results.md`. This file is the reproduction guide.

## Safety model

- All artifacts stay under `.test-tools/` (private: `stress-runs/`, `soak-runs/`, `campaign/`,
  `thread-runtimes/`). Exported shares stay under `stress-results/` (gitignored).
- Each case runs as its own transient user unit in `app.slice` with
  `MemoryMax=2G`, `CPUQuota=200%`, `TasksMax=<budget>`, `KillMode=control-group` and a
  `RuntimeMaxSec` deadline. The unit owns escaped native grandchildren.
- Subprocess environments are constructed, never copied: `HOME`, `HERMES_HOME`, XDG roots and
  `ZVEC_GREP_HOME` all point inside the run directory. No credentials, no remote embedding
  endpoint, no production token.
- Every case first verifies the production baseline and refuses to run if it changed:
  `memory.provider`, the service `MainPID`/`ActiveState`, the vault `config.json`, the engine
  wrapper, the systemd unit, and the installed plugin files. The parsed `memory`/`plugins`
  config sections are compared instead of the whole `config.yaml`, because the Hermes CLI
  rewrites unrelated keys (onboarding, `_config_version`) on its own.

## Layout

| Path | Role |
|---|---|
| `scripts/run_campaign.py` | serial controller; receipts, manifest, production gate, `--tasks` |
| `scripts/stress_memory.py` | finite lanes: `regression`, `native-regression`, `load` |
| `scripts/soak_memory.py` | same-handle long-lived soak over a run-owned daemon |
| `scripts/report_stress.py` | privacy-allowlisted aggregate export |
| `scripts/prepare_zg_thread_runtime.py` | private engine copy with requested thread pools |
| `.test-tools/campaign/planned.json` | case definitions (private) |
| `.test-tools/campaign/manifest.json` | every attempt plus the export entries (private) |

## Resource profiles

| Profile | Memory | CPU | Tasks | Use |
|---|---|---|---|---|
| `256` | 2 GiB | 200 % | 256 | the measured native profile; controller default |
| `128` | 2 GiB | 200 % | 128 | historical default; too small for the native engine |

`run_campaign.py` defaults to `--tasks 256`. A native zg process tree needs roughly 150
concurrent tasks during warm churn. Below that the kernel refuses thread creation, the native
layer aborts (`terminate called without an active exception`), and SQLite/RocksDB report
`Resource temporarily unavailable`. That is a harness ceiling, not a provider defect: the
production unit itself runs with `TasksMax=18232`.

## Running the lanes

```sh
export HERMES_AGENT_DIR=$HOME/.hermes/hermes-agent

# finite lanes
.venv/bin/python scripts/run_campaign.py --variant fix --tasks 256 --case native-repeat-2
.venv/bin/python scripts/run_campaign.py --variant fix --tasks 256 --case native-repeat-10
.venv/bin/python scripts/run_campaign.py --variant fix --tasks 256 --case offline-8x200-drain

# soak lanes (bounded proof first, then the full run)
.venv/bin/python scripts/run_campaign.py --variant fix --tasks 256 --case daemon-proof-5m
.venv/bin/python scripts/run_campaign.py --variant fix --tasks 256 --case daemon-soak-30m-restart

# a prepared thread-limited engine, for the pool experiment
.venv/bin/python scripts/run_campaign.py --variant fix --tasks 256 \
  --case daemon-smoke-threadruntime
```

Run one lane at a time. `--list` prints the known labels; an unknown label or a task budget
outside 64..4096 exits 2 before anything runs. `run_campaign.py` stops on the first failure and
keeps the attempt.

## Receipts and reading them

Each attempt writes `.test-tools/campaign/<unit>.json` and appends the same object to
`manifest.json.runs`, plus a privacy-allowlisted entry to `manifest.json.reports`:

```jsonc
{
  "label": "native-repeat-10", "unit": "hermes-zvec-stress-…", "exit_code": 0,
  "limits": {"memory_bytes": 2147483648, "cpu_quota_percent": 200, "tasks": 256, "runtime_seconds": 1800},
  "elapsed_seconds": 403.6, "cleanup_verified": true, "production_unchanged": true,
  "harness_passed": true, "passed": true, "report": ".test-tools/stress-runs/stress-…/report.json",
  "cgroup": {"pids_peak": 150, "pids_events": {"max": 0}, "memory_peak_bytes": …, "memory_events": {"oom_kill": 0}}
}
```

`passed` is true only when the child exited 0, the inner report passed, the cgroup is empty and
production is unchanged. A lane that exits 124 hit its deadline; `pids_events.max > 0` means the
task ceiling was hit; `memory_events.oom_kill > 0` marks an OOM.

An attempt may also be recorded with no report at all (`failure_category: timeout|child_exit|oom`).
Those entries are exportable evidence; never delete them.

## Exporting a shareable aggregate

```sh
.venv/bin/python scripts/report_stress.py \
  --manifest .test-tools/campaign/manifest.json \
  --machine .test-tools/campaign/machine.json \
  --output-dir stress-results/campaign-final --zip
```

The export drops hostnames, ports, absolute paths, exception text and unknown fields, renames
scenarios to ordinal IDs, and pools raw samples only. Percentiles are nearest-rank over raw
samples, never pooled quantiles. Soak quantiles stay per attempt. A successful receipt whose
schema is not recognized raises instead of passing.

## Decision rules

- Change one axis per experiment. If the task budget changes, nothing else does.
- Never raise `MemoryMax`/`CPUQuota` to make a lane green, and never relax a lane assertion
  without the raw error contract as evidence.
- A clean 30-minute run is evidence for that profile on this machine, not proof that a defect is
  absent.
- Anything that changes production state must be followed by
  `run_campaign.py --snapshot-production --reason "<why>"`, which preserves the previous
  revision as `production-before-rev<N-1>.json`.
