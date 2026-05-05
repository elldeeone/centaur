---
name: managing-db-migrations
description: "Manages Centaur Postgres migrations with the repo's dbmate wrapper. Use when asked to create, extend, apply, roll back, or inspect migrations; continue prior schema work from the current thread or a prior PR; preserve an existing table/layout contract; or work on files under services/api/db/migrations. Triggers on: db migration, database migration, migration follow-up, overlay migration, continue prior schema, extend existing tables, dbmate, schema_migrations, migrate up, migrate rollback."
---

# Managing DB Migrations

Manage Centaur database migrations through `./scripts/dbmate`, the core `services/api/db/migrations` set, and the overlay migration set when `--set overlay` is part of the task.

## Use This Skill When

- The user asks to create a new database migration.
- The user asks to apply, roll back, or inspect migrations.
- The user asks to continue earlier migration work, extend a prior PR's schema, or preserve an established table/layout contract.
- The task explicitly targets an overlay migration set.
- The task touches `services/api/db/migrations` or `schema_migrations`.
- The task mentions `dbmate` or migration status.

## Default Workflow

1. If the task explicitly continues earlier migration work, references a prior PR, draft, or schema, or asks to extend an existing overlay schema, read the relevant earlier thread context before drafting SQL or a PR. Restate the schema contract already established in the thread and treat it as binding unless the user explicitly changes it.
2. Inspect the existing SQL files in the relevant migration set to understand the numbering and recent schema changes. For follow-up overlay work, also inspect the prior schema surface the new migration is extending.
3. For a new migration, run `./scripts/dbmate new <short_name>` for core work or `./scripts/dbmate --set overlay new <short_name>` for overlay work.
4. Before writing SQL for follow-up overlay work, compare the planned tables, columns, indexes, and naming against the previously established schema surface. Call out any intentional divergence before coding instead of silently drifting to a new shape.
5. Edit the generated SQL file and keep both `-- migrate:up` and `-- migrate:down` sections.
6. For runtime checks, prefer `./scripts/dbmate status`, `./scripts/dbmate up`, or `./scripts/dbmate rollback` instead of invoking `dbmate` directly. Use `--set overlay` when the user wants to inspect or apply the overlay migration set in isolation. Without `--set`, commands such as `status` and `up` run every available migration set, while `rollback` targets the core set.
7. If the change affects runtime behavior, run the highest-value local verification the environment supports and report any blocked checks clearly.

## Rules

- Prefer adding a new migration over editing an old one. Only rewrite an existing migration when the user explicitly asks or the migration is clearly unreleased in the current branch.
- When a migration task is a continuation of earlier thread work, preserve the previously established schema contract unless the user explicitly changes it.
- Do not infer a prior schema contract for a brand-new migration or an operational command such as `status`, `up`, or `rollback` unless the user explicitly references earlier schema work.
- Do not renumber existing migration files.
- Keep migration names short, lowercase, and descriptive; the wrapper normalizes them to the repo format.
- Use `./scripts/dbmate --set overlay` when the task is explicitly working in the overlay migration set or when the earlier migration work being continued is in the overlay set.
- For overlay migrations that extend an earlier schema or PR, compare the planned schema surface against the earlier tables before coding and surface any intentional divergence.
- Make SQL idempotent where that is cheap and clear, especially for `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `DROP ... IF EXISTS`, and index creation.
- If a rollback cannot be automated safely, leave a brief comment in the `down` section explaining that instead of faking a destructive rollback.

## Wrapper Behavior

- `./scripts/dbmate new add_agent_leases` creates the next numbered file in `services/api/db/migrations`.
- `./scripts/dbmate --set overlay new add_crm_tables` creates the next numbered overlay migration file when the overlay migration directory exists.
- `./scripts/dbmate status` runs `dbmate` inside the `api` container.
- Commands such as `status` and `up` run all available migration sets by default; pass `--set core` or `--set overlay` to operate on one set only.
- `./scripts/dbmate rollback` targets the core migration set by default; use `./scripts/dbmate --set overlay rollback` only when intentionally rolling back overlay migrations.
- If `DATABASE_URL` is not set in the host shell, the wrapper reads it from the running `api` container.
- The repo mounts `services/api/db` into `/app/db`, so new migration files are visible to the `api` container without rebuilding the image.

## Common Commands

```bash
./scripts/dbmate new add_agent_leases
./scripts/dbmate --set overlay new add_crm_tables
./scripts/dbmate status
./scripts/dbmate --set overlay status
./scripts/dbmate up
./scripts/dbmate rollback
./scripts/dbmate --set overlay rollback
```

## If Docker Is Unavailable

If Docker or the local stack is unavailable, still do the highest-value part of the task:

- create or edit the migration file,
- validate obvious SQL and file-shape issues,
- explain that live `status` or `up` verification is blocked by the missing runtime.
