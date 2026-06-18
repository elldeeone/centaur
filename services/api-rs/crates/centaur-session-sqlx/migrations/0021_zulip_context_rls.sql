do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'centaur_slack_reader') then
        create role centaur_slack_reader nologin;
    end if;

    if not exists (select 1 from pg_roles where rolname = 'centaur_slack_admin') then
        create role centaur_slack_admin nologin;
    end if;

    execute format('grant centaur_slack_reader to %I', current_user);
end
$$;

grant usage on schema public to centaur_slack_reader, centaur_slack_admin;

grant select on zulip_sync_streams to centaur_slack_admin;
grant select on zulip_sync_topics to centaur_slack_admin;
grant select on zulip_sync_users to centaur_slack_admin;
grant select on zulip_sync_messages to centaur_slack_reader, centaur_slack_admin;
grant select on company_context_documents to centaur_slack_reader, centaur_slack_admin;

create table if not exists slack_context_rls_admin_channels (
    channel_id text primary key,
    created_at timestamptz not null default now()
);

create table if not exists zulip_context_rls_admin_streams (
    realm text not null,
    stream_id bigint not null,
    created_at timestamptz not null default now(),
    primary key (realm, stream_id)
);

grant select on slack_context_rls_admin_channels to centaur_slack_reader, centaur_slack_admin;
grant select on zulip_context_rls_admin_streams to centaur_slack_reader, centaur_slack_admin;

alter table zulip_sync_messages enable row level security;
alter table company_context_documents enable row level security;

drop policy if exists centaur_zulip_messages_admin_select on zulip_sync_messages;
create policy centaur_zulip_messages_admin_select
    on zulip_sync_messages
    for select
    to centaur_slack_admin
    using (true);

drop policy if exists centaur_zulip_messages_reader_select on zulip_sync_messages;
create policy centaur_zulip_messages_reader_select
    on zulip_sync_messages
    for select
    to centaur_slack_reader
    using (
        (
            stream_id::text = nullif(current_setting('centaur.zulip_stream_id', true), '')
            and realm = nullif(current_setting('centaur.zulip_realm', true), '')
        )
        or exists (
            select 1
            from zulip_context_rls_admin_streams admins
            where admins.realm = nullif(current_setting('centaur.zulip_realm', true), '')
              and admins.stream_id::text = nullif(current_setting('centaur.zulip_stream_id', true), '')
        )
    );

drop policy if exists centaur_context_docs_reader_select on company_context_documents;
create policy centaur_context_docs_reader_select
    on company_context_documents
    for select
    to centaur_slack_reader
    using (
        source not in ('slack', 'zulip')
        or (
            source = 'slack'
            and (
                metadata ->> 'channel_id' = nullif(current_setting('centaur.slack_channel_id', true), '')
                or exists (
                    select 1
                    from slack_context_rls_admin_channels admins
                    where admins.channel_id = nullif(current_setting('centaur.slack_channel_id', true), '')
                )
            )
        )
        or (
            source = 'zulip'
            and (
                (
                    metadata ->> 'stream_id' = nullif(current_setting('centaur.zulip_stream_id', true), '')
                    and metadata ->> 'realm' = nullif(current_setting('centaur.zulip_realm', true), '')
                )
                or exists (
                    select 1
                    from zulip_context_rls_admin_streams admins
                    where admins.realm = nullif(current_setting('centaur.zulip_realm', true), '')
                      and admins.stream_id::text = nullif(current_setting('centaur.zulip_stream_id', true), '')
                )
            )
        )
    );
