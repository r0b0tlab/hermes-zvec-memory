# Catalog submission — requirement check (2026-09-11)

What the Hermes plugin catalog requires of an entry, where each rule is
published, and the evidence that this repository satisfies it. Re-run the
commands below after any new pin.

Sources (all authoritative, all read for this check):

| Source | What it governs |
| --- | --- |
| `plugin-catalog/README.md` in hermes-agent | Admission policy (6 rules) + entry schema |
| `website/docs/user-guide/features/plugin-catalog.md` (published at `/docs/user-guide/features/plugin-catalog`) | Public submission checklist (5 conditions) |
| `.github/workflows/plugin-catalog-ci.yml` in hermes-agent | The two admission gates that must be green on the PR |
| `hermes_cli/plugin_validate.py` (`hermes plugins validate`) | Manifest schema + declared-vs-registered capability probe |
| `scripts/validate_plugin_catalog.py` in hermes-agent | Standalone structural validator the CI runs first |

## Status

| # | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| 1 | **Owner-submitted** — PR author owns or maintains the plugin repo | ✅ | `gh api orgs/r0b0tlab/memberships/am423` → `role: admin`, `state: active` |
| 2 | **Public repository**, publicly cloneable, `https://` URL | ✅ | `gh api repos/r0b0tlab/hermes-zvec-memory` → `private: false`, `license: MIT`; a fresh full clone was performed during this check |
| 3 | **Released** — real releases/tags, not just a branch tip | ✅ | annotated tag `v0.2.0` + published GitHub release, `git rev-list -n1 v0.2.0` = `36fdba7…` |
| 4 | **Passing validation** — structural schema + pinned-source `hermes plugins validate` | ✅ | both gates run locally at the pin, zero errors, zero warnings (commands below) |
| 5 | **Pinned to settled code** — the pinned SHA is at least 2 weeks old | ⏳ **not yet** | pin `36fdba7…` was committed 2026-09-11T10:49:09-05:00 → eligible from **2026-09-25T10:49-05:00** |
| 6 | **Exact 40-hex SHA pin**, mandatory | ✅ | `sha: 36fdba76e1693ac8f9da14e0f0f88438aba43757` |
| 7 | **Human-merged gate** — entry + every pin bump land via reviewed PR | ✅ (process) | submission is a PR to `plugin-catalog/`; no self-serve path exists |
| 8 | **Declared capabilities match reality** at the pinned commit | ✅ | at the pin: `get_tool_schemas()` → `memory_search`, `memory_store`; `plugin.yaml` hooks → `on_session_end`, `on_memory_write`; nothing required from the environment |
| 9 | Entry schema fields valid | ✅ | `name` matches `[a-z0-9_-]{1,64}`; `repo` https; `tier: community`; `description`/`maintainer` non-empty; `platforms: []` (= all); capability lists are strings |
| 10 | Not on the kill list (`plugin-catalog/removed.yaml`) | ✅ | `removed.yaml` currently has 0 entries; no name/repo match |
| 11 | Plugin passes the manifest checks the CI gate runs | ✅ | `hermes plugins validate` at the pin: manifest parses; `name`/`version`/`description` present; `requires_env` all UPPER_SNAKE; capability probe runs `register()` in isolation; no built-in tool collisions |

### Deliberate omissions

- `subdir` is **not** optional for us: the plugin lives in `zvec-memory/` of this
  repository, and the CI looks for `plugin.yaml` at `<repo>/<subdir>`. It is set.
- `requires_hermes` is omitted. No entry in the catalog declares it, our
  manifest declares none, and asserting a floor we have not tested against
  would either exclude working hosts or silently gate the plugin (the manifest
  field is load-blocking). `hermes plugins validate` reports "not declared" as a
  pass.
- No `capabilities:` block in `plugin.yaml`. The manifest field takes ids from
  the host's capability registry (`tools.override`, `llm.*`, `gateway.platform_actions`)
  and this plugin uses none of them; the catalog entry's `capabilities:` block is
  a different shape and is what reviewers read.
- `hermes-zvec-memory` (not `zvec-memory`) as the catalog key: it matches the
  repository name. The install directory does **not** come from this key — the
  installer uses the manifest `name`, so a catalog install still lands in
  `$HERMES_HOME/plugins/zvec-memory/` and `memory.provider: zvec-memory` resolves.

## How this was verified

```sh
# Gate 1 — structural (identical to the CI's first job)
python3 scripts/validate_plugin_catalog.py plugin-catalog/          # in hermes-agent
# with this entry staged into a copy of plugin-catalog/: "OK: 11 file(s) valid"

# Gate 2 — pinned-source (identical to the CI's second job)
git clone https://github.com/r0b0tlab/hermes-zvec-memory /tmp/x
git -C /tmp/x checkout --detach 36fdba76e1693ac8f9da14e0f0f88438aba43757
hermes plugins validate /tmp/x/zvec-memory        # "Validation passed." (10/10 checks, no warnings)
```

## Submitting (on or after 2026-09-25)

1. Fork `NousResearch/hermes-agent`, branch from `main`.
2. Add `plugin-catalog/hermes-zvec-memory.yaml` with the exact contents of this
   repository's `plugin-catalog-entry.yaml`.
3. Open the PR titled **"plugin-catalog: add hermes-zvec-memory"** with a body
   stating: what the plugin does, that the author (`am423`) owns
   `r0b0tlab/hermes-zvec-memory`, that the pin is tag `v0.2.0`
   (`git rev-list -n1 v0.2.0`), that the pinned commit is ≥ 2 weeks old, which
   capabilities are declared and how they were verified at the pin, and that
   both admission gates pass locally (paste the `hermes plugins validate` output).
4. Expect a maintainer to read the pinned commit range; the diff is the review
   surface. Pin bumps are new PRs with the same evidence.

Anything that changes the plugin's registered tools or hooks changes this
entry's `capabilities:` block in the same PR — capability creep is treated as a
security issue by the admission policy.
