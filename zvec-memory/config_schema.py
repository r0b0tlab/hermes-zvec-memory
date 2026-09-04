"""zvec-memory's declared config surface — rendered by the generic desktop panel.

Loaded by path (never via package import): this module may only import from
``plugins.memory.config_schema`` — the provider runtime must not load into
the web server.
"""

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_SELECT,
    KIND_TEXT,
    STORAGE_FLAT_JSON,
    ProviderConfigSchema,
    ProviderField,
    ProviderFieldOption,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="zvec-memory",
    label="Zvec Memory",
    storage=STORAGE_FLAT_JSON,
    docs_url="https://github.com/zvec-ai/zvec-grep",
    fields=(
        ProviderField(
            key="vault",
            label="Vault directory",
            kind=KIND_TEXT,
            description="Markdown memory vault (facts/, sessions/). Defaults to $HERMES_HOME/zvec-memory.",
            placeholder="$HERMES_HOME/zvec-memory",
            inline=True,
            group="Storage",
        ),
        ProviderField(
            key="embedding",
            label="Embedding model",
            kind=KIND_TEXT,
            default="local/potion-retrieval-32m",
            env_fallbacks=("ZVEC_GREP_EMBEDDING",),
            description="Embedding model for new indexes. Changing it requires a rebuild.",
            inline=True,
            group="Retrieval",
        ),
        ProviderField(
            key="recall_limit",
            label="Recall limit",
            kind=KIND_NUMBER,
            default="5",
            description="Default result count for prefetch and memory_search.",
            inline=True,
            group="Retrieval",
        ),
        ProviderField(
            key="context_chars",
            label="Context cap",
            kind=KIND_NUMBER,
            default="2000",
            description="Max chars of recall injected per turn. Truncates at word boundaries.",
            group="Retrieval",
        ),
        ProviderField(
            key="preview",
            label="Result preview",
            kind=KIND_SELECT,
            default="short",
            description="Source preview size in results. none keeps context smallest.",
            options=(
                ProviderFieldOption("none", "None"),
                ProviderFieldOption("short", "Short"),
                ProviderFieldOption("full", "Full"),
            ),
            group="Retrieval",
        ),
        ProviderField(
            key="auto_extract",
            label="Auto-extract",
            kind=KIND_BOOL,
            default="false",
            description="Extract preference/decision facts from user messages at session end.",
            inline=True,
            group="Writing",
        ),
        ProviderField(
            key="reindex_min_seconds",
            label="Reindex interval",
            kind=KIND_NUMBER,
            default="600",
            description="Minimum seconds between background index updates.",
            group="Writing",
        ),
    ),
)
