# Catalog optimization measurement — 2026-10-17

Copied vault: `/home/r0b0tmagic/hermes-workspace/projects/zvec-memory-catalog-opt-20261017/.test-tools/reindex-vault`

- Markdown files copied: 45 (`facts/` and `sessions/` only; no `.zvec-grep`)
- Command: `/home/r0b0tmagic/.local/share/hermes-zvec-memory/zg-default index`
- Exit: 0
- elapsed_seconds: 14.439
- Branch taken: keep the 600-second debounce. Do not force session reindex.
- Note: the first index downloaded `local/potion-code-16m-v2` (32 MiB) before completion. This is not a warm production-model reindex. It is still above the 5-second force-refresh budget, so session appends stay on `_maybe_reindex()` without `force=True`.

Live service after the measurement: ActiveState=active, MainPID=3555174.
