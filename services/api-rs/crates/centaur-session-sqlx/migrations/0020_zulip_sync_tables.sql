create table if not exists zulip_sync_streams (
    realm text not null,
    stream_id bigint not null,
    stream_name text not null default '',
    description text not null default '',
    is_archived boolean not null default false,
    is_public boolean not null default false,
    is_web_public boolean not null default false,
    is_syncable boolean not null default false,
    raw_payload jsonb not null default '{}'::jsonb,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (realm, stream_id)
);

create index if not exists idx_zulip_sync_streams_syncable
    on zulip_sync_streams (is_syncable, realm, stream_name);

create table if not exists zulip_sync_topics (
    realm text not null,
    stream_id bigint not null,
    topic_name text not null,
    max_id bigint,
    message_count integer not null default 0,
    raw_payload jsonb not null default '{}'::jsonb,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (realm, stream_id, topic_name),
    foreign key (realm, stream_id)
        references zulip_sync_streams(realm, stream_id)
        on delete cascade
);

create index if not exists idx_zulip_sync_topics_updated
    on zulip_sync_topics (updated_at desc);

create table if not exists zulip_sync_users (
    realm text not null,
    user_id bigint not null,
    email text not null default '',
    full_name text not null default '',
    is_bot boolean not null default false,
    is_active boolean not null default true,
    raw_payload jsonb not null default '{}'::jsonb,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (realm, user_id)
);

create index if not exists idx_zulip_sync_users_full_name
    on zulip_sync_users (realm, full_name);

create table if not exists zulip_sync_runs (
    run_id text primary key,
    workflow_run_id text,
    mode text not null default 'incremental',
    status text not null,
    realm text not null default '',
    streams_requested jsonb not null default '[]'::jsonb,
    streams_synced jsonb not null default '[]'::jsonb,
    streams_skipped jsonb not null default '[]'::jsonb,
    streams_failed jsonb not null default '[]'::jsonb,
    topics_fetched integer not null default 0,
    messages_fetched integer not null default 0,
    messages_upserted integer not null default 0,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    error_text text not null default '',
    metadata jsonb not null default '{}'::jsonb
);

create index if not exists idx_zulip_sync_runs_started
    on zulip_sync_runs (started_at desc);

create table if not exists zulip_sync_messages (
    realm text not null,
    message_id bigint not null,
    stream_id bigint not null,
    topic_name text not null default '',
    occurred_at timestamptz,
    sender_id bigint,
    sender_email text not null default '',
    sender_full_name text not null default '',
    recipient_id bigint,
    message_type text not null default 'stream',
    content text not null default '',
    rendered_content text not null default '',
    subject text not null default '',
    permalink text not null default '',
    raw_payload jsonb not null default '{}'::jsonb,
    source_run_id text references zulip_sync_runs(run_id) on delete set null,
    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (realm, message_id),
    foreign key (realm, stream_id)
        references zulip_sync_streams(realm, stream_id)
        on delete cascade
);

create index if not exists idx_zulip_sync_messages_stream_topic
    on zulip_sync_messages (realm, stream_id, topic_name, message_id);

create index if not exists idx_zulip_sync_messages_occurred
    on zulip_sync_messages (occurred_at desc);

create index if not exists idx_zulip_sync_messages_sender
    on zulip_sync_messages (realm, sender_id, occurred_at desc);

create index if not exists idx_zulip_sync_messages_text
    on zulip_sync_messages
    using gin (to_tsvector('english', coalesce(content, '') || ' ' || coalesce(rendered_content, '')));

create table if not exists zulip_sync_checkpoints (
    realm text not null,
    stream_id bigint not null,
    watermark_message_id bigint,
    last_run_id text references zulip_sync_runs(run_id) on delete set null,
    last_success_at timestamptz,
    last_error text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (realm, stream_id),
    foreign key (realm, stream_id)
        references zulip_sync_streams(realm, stream_id)
        on delete cascade
);

create table if not exists zulip_sync_backfill_jobs (
    job_id bigserial primary key,
    job_key text not null unique,
    job_type text not null,
    payload_version integer not null default 1,
    realm text not null,
    stream_id bigint not null,
    topic_name text not null default '',
    status text not null default 'pending',
    payload_json jsonb not null default '{}'::jsonb,
    priority integer not null default 100,
    attempt_count integer not null default 0,
    last_run_id text references zulip_sync_runs(run_id) on delete set null,
    last_enqueued_at timestamptz not null default now(),
    last_started_at timestamptz,
    last_completed_at timestamptz,
    last_error text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    foreign key (realm, stream_id)
        references zulip_sync_streams(realm, stream_id)
        on delete cascade
);

create index if not exists idx_zulip_sync_backfill_jobs_status_priority
    on zulip_sync_backfill_jobs (status, priority, updated_at);

create index if not exists idx_zulip_sync_backfill_jobs_stream_status
    on zulip_sync_backfill_jobs (realm, stream_id, status);

create unique index if not exists uq_zulip_sync_stream_bootstrap_backfill
    on zulip_sync_backfill_jobs (realm, stream_id)
    where job_type = 'stream_bootstrap';
