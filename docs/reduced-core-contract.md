# Reduced v0.3.0 core contract

This user-approved reduced contract supersedes older full-release requirements.
Actual pinned-source core/CLI/native execution and scoped independent review
are recorded in [validation](release-0.3.0-validation.md). Publication requires
final integration acceptance and exact remote readback; production deployment is
excluded. Historical failed or unexecuted obligations are not relabeled PASS.

## Supported target and retained behavior

One pinned Linux/Hermes source tuple, Node 26.8.1, zvec-grep 0.2.2, and
`local/potion-retrieval-32m`. Final validation must record the exact Hermes
commit and source identities; the old Node >=22 admission floor is not a
broader supported-platform claim.

Keep `memory_search`, `memory_store`, CLI setup/status/doctor/reindex, Markdown
source durability, indexing, persistence/restart, ordinary recovery and durable
notification replay. Managed setup remains default-profile-only, as before.
CLI setup verifies an existing local engine and complete launcher/unit/manifest
artifacts, writes canonical private JSON, then publishes host activation. Setup
does not fetch dependencies or start/restart a production service.

## Explicitly unsupported entries

| Entry | Reduced-core behavior |
| --- | --- |
| `hermes zvec-memory migrate-config` | Exit 2; explicit unsupported action; no home/settings/engine lookup or write |
| `hermes zvec-memory backup` / `restore` | Never implemented; argparse rejects before dispatch |
| `migrate_legacy_mirrors(entries)` | `NotImplementedError` before vault access |
| `backup_paths()` | `NotImplementedError`, no advertised coherent provider snapshot |
| Declared desktop/F9 config | `CONFIG_SCHEMA = None`; no provider panel |
| Legacy generic desktop config | Empty `get_config_schema()`; `save_config()` refuses before publication |
| Different, remote, null, empty or false embedding | `ValueError` before provider state, managed layout or processes |
| Different engine version request | `ValueError` before managed layout, npm or runtime processes |

Missing embedding retains the supported default. Supplied provider settings,
engine blocks and host `plugins.zvec-memory` setup blocks are validated; setup
also validates the authoritative native settings before artifacts/activation.
CLI reindex refuses an unsupported readable selection before health processes
or a durable rebuild request. Existing malformed-config/unknown-vault and
known-vault unhealthy-state handling remains distinct.

The pinned host's CLI `memory_setup._post_setup_hook` invokes the provider hook
before the generic schema prompt/writer; therefore disabling desktop schemas
and `save_config` does **not** retire CLI setup. The existing provider hook and
engine's verified JSON/activation writer are retained. Abstract backup API
compatibility is retained through an explicit refusal, which the host external
path collector catches. The host's unrelated generic profile archive may still
include files below HERMES_HOME: that is not a qualified provider backup or
restore. Do not use it as a coherent vault recovery recipe.

Desktop/F9 activation is outside this contract. Removing provider configuration
surfaces is not a claim that every host-level generic provider toggle is
intercepted. No caller detection or host/core changes are introduced.

Automatic migration, backup/restore, multiwriter capacity, model switching,
broader platform support and the bespoke cold-bootstrap attestation campaign
are deferred. Historical tests remain immutable; final acceptance must give
specific dispositions and retain shared CLI/recovery/privacy coverage rather
than excluding whole mixed-scope files indiscriminately.

## Stationary vault is mandatory

The vault and **all ancestor directories must remain stationary while active**.
Stop and drain the owning provider before relocating a directory. Atomic safety
against a noncooperating external directory rename after a final userspace check
is not guaranteed. Historical counterexamples are preserved, not described as
fixed. Ordinary path/symlink refusal, privacy, source/journal integrity, stale
fact suppression and owned-child cleanup remain mandatory. No general safety
waiver is authorized.

The historical native 8x200 campaign did not converge to idle by its deadline.
This release makes no large-burst/multiwriter capacity claim and does not reuse
that failure as acceptance. A fresh final-source ordinary lifecycle and scoped
soak remain required.
