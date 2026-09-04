# hermes-zvec-memory

Local-first memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent),
backed by [zvec-grep](https://github.com/zvec-ai/zvec-grep) (`zg`): hybrid
BM25 + vector search with RRF fusion over a Markdown vault. No cloud, no
account, no data leaves the machine (unless you explicitly grant a remote
embedding model).

## How it works

- Vault layout under `$HERMES_HOME/zvec-memory/` (profile-scoped):
  `facts/` (durable facts) + `sessions/` (per-day turn logs), indexed by `zg`
  into `<vault>/.zvec-grep/`.
- Each turn, `prefetch` runs one `zg query --preview short --limit 5` (skipped
  for trivial prompts) and injects compact `path:line` results.
- Each turn, `sync_turn` appends to the daily session log in the background;
  reindexing is debounced (default 600 s), never per-turn.
- Tools: `memory_search` (hybrid/fts/vector + globs + limit) and
  `memory_store` (append a fact, background reindex).
- Built-in `memory`-tool writes are mirrored into the vault; optional
  session-end extraction harvests preferences/decisions (compaction handoffs
  excluded). `hermes backup` includes the vault via `backup_paths()`.

## Install

Prerequisites: Node.js 22+, then:

```bash
npm install -g @zvec/zvec-grep
```

Install the provider (files only — does not change your active provider):

```bash
cp -r zvec-memory "${HERMES_HOME:-~/.hermes}/plugins/zvec-memory"
hermes memory status   # zvec-memory appears with availability
```

Activate (switches recall for all future sessions — one provider at a time):

```bash
hermes memory setup    # pick zvec-memory
```

For faster repeat recall, keep the shared daemon up (shares loaded models,
background refresh):

```bash
zg server on
zg server status --check-ready
```

First run builds the vault index in the background
(`local/potion-retrieval-32m` downloads on first use and stays cached under
`~/.zvec-grep/models`). Check it with:

```bash
zg status "${HERMES_HOME:-~/.hermes}/zvec-memory" --check-ready
```

## Configure

In `$HERMES_HOME/config.yaml`:

```yaml
plugins:
  zvec-memory:
    vault: $HERMES_HOME/zvec-memory
    embedding: local/potion-retrieval-32m   # code-heavy vaults: local/potion-code-16m-v2
    recall_limit: 5
    context_chars: 2000
    preview: short                          # none | short | full
    auto_extract: false
    reindex_min_seconds: 600
```

Changing `embedding` (or a remote endpoint) requires an explicit rebuild:

```bash
zg index --rebuild --embedding local/jina-embeddings-v2-base-code <vault>
```

Remote embeddings (Qwen) send vault/query text off-machine and need both a
credential and an explicit grant — never automatic:

```bash
zg config provider set qwen --api-key "$DASHSCOPE_API_KEY"
zg auth grant --capability embedding --scope workspace <vault>
```

## Verify

```bash
hermes memory status
zg status <vault> --check-ready
```

Then in a fresh session ask something stored in the vault and confirm
`memory_search` returns it with a `path:line` citation.

## Measure recall

`scripts/measure_recall.py` seeds an isolated vault with 20 known facts,
rebuilds the index, and runs two query sets (10 paraphrased worst-case
queries + 10 same-vocabulary realistic queries) through `memory_search`:

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/measure_recall.py
~/.hermes/hermes-agent/venv/bin/python scripts/measure_recall.py \
  --embedding local/embeddinggemma-300m
```

Reference numbers (20 facts, `local/potion-retrieval-32m`, 2026-09-04):

| set | hybrid hit@5 | fts hit@5 | p50 |
| --- | --- | --- | --- |
| same-vocabulary (realistic) | 10/10, all visible under the 2000-char cap | 10/10 | 0.25 s |
| paraphrased (adversarial) | 6/10 | 4/10 | 0.28 s |
| paraphrased, `embeddinggemma-300m` | 6/10 (different misses, same count) | 4/10 | 0.29 s, p95 4.3 s |

The bigger model did not move paraphrase recall but cost 125 s to build vs
3 s — so `potion-retrieval-32m` stays the default; paraphrase-heavy recall
is a retrieval-tuning limit, not a model-size limit, at this vault scale.

Plus: warmed-cache prefetch is ~0.000 s vs ~0.27 s cold (~265x) — that is
what `queue_prefetch` buys every turn. If your vault needs better
paraphrase-heavy recall, the levers are query-side (hybrid groups, `--fuse`,
globs) and content-side (richer fact wording), not a bigger embedding —
see `docs/04-pipeline.md` upstream.

## Run tests

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q
```

(Hermes source must be importable: run from the hermes-agent checkout or set
`HERMES_AGENT_DIR`. Tests that need `zg` skip when it is absent.)

## Layout

```text
zvec-memory/
  __init__.py        provider (MemoryProvider ABC + register(ctx))
  plugin.yaml        name/version/description/hooks
  config_schema.py   declarative desktop config panel
tests/
  test_provider.py
```

## Credits

Recall engine: [zvec-ai/zvec-grep](https://github.com/zvec-ai/zvec-grep)
(Apache-2.0). Provider pattern follows Hermes's bundled `holographic` memory
provider. MIT licensed (see LICENSE).
