# Zulip ETL

Zulip ETL is disabled unless the API service has `ZULIP_ETL_ENABLED=true`.
Production deployments should enable it deliberately after choosing the Zulip
bot credential, stream scope, exclusion patterns, and data boundary they want
agents to use.

Zulip ETL mirrors the Slack ETL pattern. It keeps an indexed, queryable copy of
public Zulip stream history in Postgres for agent context and operator workflows.
It runs scheduled no-delivery workflows:

| Workflow | Default cadence | Role |
|----------|-----------------|------|
| `zulip_sync` | 1 hour | Lists public streams, refreshes users/topics, syncs recent messages, advances per-stream checkpoints, and enqueues backfill jobs. |
| `zulip_backfill` | 10 minutes | Claims queued backfill jobs and drains Zulip message pages. |
| `company_context_documents` | 4 hours | Projects changed Zulip rows into `company_context_documents` for retrieval. |

The ETL path is separate from Zulipbot delivery. Zulipbot handles live tagged
turns and same-topic context; Zulip ETL reads stream history with a bot token and
writes durable rows into Postgres.

## Configuration

| Environment variable | Default | Effect |
|----------------------|---------|--------|
| `ZULIP_ETL_ENABLED` | `false` | Enables `zulip_sync`, `zulip_backfill`, and document projection. |
| `ZULIP_ETL_SITE` | falls back to `ZULIP_SITE` | Zulip organization URL. |
| `ZULIP_ETL_EMAIL` | falls back to `ZULIP_BOT_EMAIL` | Bot email used for ETL API calls. |
| `ZULIP_ETL_API_KEY` | falls back to `ZULIP_API_KEY` | Bot API key used for ETL API calls. |
| `ZULIP_ETL_REALM` | inferred from hosted Zulip Cloud URLs | Realm label stored with synced rows and documents; set this explicitly for custom/self-hosted domains so it matches Zulipbot thread keys. |
| `ZULIP_SYNC_INTERVAL_SECONDS` | `3600` | How often to run incremental Zulip sync. |
| `ZULIP_BACKFILL_ENABLED` | `true` | Enables the backfill worker schedule. |
| `ZULIP_BACKFILL_INTERVAL_SECONDS` | `600` | How often to drain queued backfill jobs. |
| `ZULIP_BACKFILL_STREAM_BATCH_LIMIT` | `50` | Maximum backfill jobs claimed per run. |
| `ZULIP_BACKFILL_STREAM_PAGES_PER_JOB` | `5` | Maximum Zulip message pages drained before a continuation job is queued. |
| `ZULIP_SYNC_BACKFILL_LOOKBACK_DAYS` | `30` | Historical window seeded for first-time stream backfills. |
| `ZULIP_ETL_EXCLUDED_STREAM_PATTERNS` | empty | Comma-separated stream-name globs to skip. |
| `COMPANY_CONTEXT_DOCUMENTS_ENABLED` | `true` | Enables projection from Zulip sync rows into company context documents. |
| `COMPANY_CONTEXT_DOCUMENTS_INTERVAL_SECONDS` | `14400` | How often to project changed rows into documents. |

The ETL bot can only read history visible to that Zulip identity.
`ZULIP_ETL_REALM` must match the realm segment in Zulipbot thread keys so scoped
`company_context` reads can see the synced rows for their stream.

## Data model

| Table | Contents |
|-------|----------|
| `zulip_sync_streams` | Public streams visible to the ETL bot and whether they are currently syncable. |
| `zulip_sync_topics` | Topic metadata per stream. |
| `zulip_sync_users` | Zulip user display metadata used when rendering documents. |
| `zulip_sync_runs` | One row per incremental or backfill workflow run. |
| `zulip_sync_messages` | Stream messages keyed by `(realm, message_id)`. |
| `zulip_sync_checkpoints` | Per-stream watermarks and last error state. |
| `zulip_sync_backfill_jobs` | Deferred stream-history continuation jobs. |
| `company_context_documents` | Derived stream-day and topic documents for retrieval. |

`company_context` uses a scoped `COMPANY_CONTEXT_DSN` pg_dsn secret. The proxy
sets Postgres GUCs from the current chat principal, including
`centaur.zulip_realm` and `centaur.zulip_stream_id`; row-level-security policies
then restrict Zulip message and document reads to that stream unless an admin
stream is configured.
