# Muesli → Centaur transcript sync

[Muesli](https://github.com/pHequals7/muesli) is a local-first macOS dictation
and meeting recorder. After each meeting, Muesli can fire a configurable shell
hook with the meeting ID. This integration ships a one-line hook that pushes
the transcript into Centaur via a single durable workflow run, where it is
stored in Postgres and (optionally) announced to Slack.

The data flow:

```
╭──────────────╮  stdin {id:42}   ╭─────────────────╮  POST /workflows/runs   ╭──────────────────────╮
│  Muesli.app  │─────────────────▶│  muesli-push.sh │────────────────────────▶│ muesli_meeting_      │
│ (hook fires) │                  │   (hook)        │  {transcript, notes,    │ ingest workflow      │
╰──────────────╯                  ╰────────┬────────╯   meta}                 ╰──────────┬───────────╯
                                           │ muesli-cli meetings get 42                 │
                                           ▼                              persist + Slack notify
                                  ╭─────────────────╮
                                  │  ~/Library/.../ │
                                  │   muesli.db     │
                                  ╰─────────────────╯
```

The hook is the **only thing each user installs**. It calls one Centaur
endpoint (`POST /workflows/runs`) with one workflow name. The API key it uses
is scoped to that workflow alone — see "Workflow-scoped API keys" below — so
the same key may be safely distributed to every laptop.

## 1. Mint a workflow-scoped API key

From a host with operator access (e.g. inside the API pod):

```bash
kubectl exec -n centaur deploy/centaur-centaur-api -- curl -s -X POST \
    http://localhost:8000/admin/api-keys \
    -H 'Content-Type: application/json' \
    -d '{
          "name": "muesli-hook",
          "scopes": ["workflows:muesli_meeting_ingest"],
          "created_by": "ops"
        }'
```

The response includes the plaintext key (shown once) like:

```json
{ "key": "aiv2_…", "scopes": ["workflows:muesli_meeting_ingest"] }
```

This key can do **exactly one thing**: enqueue runs of
`muesli_meeting_ingest`. It cannot read other threads, call tools, spawn
agents, list other workflow runs, or hit the admin API. Treat it as
distributable but still rate-limit at the ingress.

## 2. Install the hook on the user's Mac

```bash
# As the user
mkdir -p "$HOME/Library/Application Support/Muesli"
curl -fsSL \
    https://raw.githubusercontent.com/paradigmxyz/centaur/main/contrib/scripts/muesli-push.sh \
    -o "$HOME/Library/Application Support/Muesli/centaur-push.sh"
chmod +x "$HOME/Library/Application Support/Muesli/centaur-push.sh"
```

Set environment so the hook can authenticate (e.g. in `~/.zshrc`):

```bash
export CENTAUR_API_URL=https://centaur.example.com
export CENTAUR_API_KEY=aiv2_xxx_paste_from_step_1
# Optional:
export MUESLI_HOST="$(hostname -s)"             # label that ends up in metadata
export MUESLI_CLI=/Applications/Muesli.app/Contents/MacOS/muesli-cli
```

In Muesli: **Settings → Meeting Hook → Executable** → point at
`~/Library/Application Support/Muesli/centaur-push.sh`. Enable the hook.

## 3. (Optional) Wire up Slack notifications

If the workflow input includes `slack_channel`, the workflow posts a summary
to that channel via the in-process `slack` tool (`ctx.post_to_slack`). No
webhook URL needed — uses the existing Slack bot token already configured for
Centaur.

To enable, set `MUESLI_SLACK_CHANNEL` on the user's Mac and the hook will
forward it through:

```bash
export MUESLI_SLACK_CHANNEL=muesli-transcripts
```

If `slack_channel` is omitted, persistence still happens; the Slack step
records `{"sent": false, "reason": "no_slack_channel"}` in the checkpoint.

## 4. Verify end-to-end

Record a short test meeting in Muesli, stop it, and tail the hook log:

```bash
tail -f "$HOME/Library/Logs/centaur-muesli-push.log"
```

You should see a line like `ok http=200 run_id=...`. Inspect the run:

```bash
kubectl exec -n centaur deploy/centaur-centaur-api -- curl -s \
    "http://localhost:8000/workflows/runs/<run_id>" | jq
```

And confirm the row landed:

```sql
SELECT id, host, meeting_id, title, length(raw_transcript), notes_state, ingested_at
FROM muesli_meetings
ORDER BY ingested_at DESC
LIMIT 5;
```

## Workflow-scoped API keys

The `workflows:<name>` scope (added alongside this integration) lets operators
mint keys that may invoke a single named workflow and nothing else. It mirrors
the existing `tools:<name>` pattern.

| Scope                                  | Permits                                                       |
| -------------------------------------- | ------------------------------------------------------------- |
| `workflows:muesli_meeting_ingest`      | `POST /workflows/runs` for that workflow + read its own runs  |
| `workflows:*` / `workflows`            | Any workflow                                                  |
| `agent:execute`                        | Any workflow (legacy, kept for backwards compatibility)       |
| `admin` / `*`                          | Everything                                                    |

Narrow keys may also call `GET /workflows/runs?workflow_name=...` (filter is
required) and `GET/POST /workflows/runs/{id}{,/cancel,/checkpoints,/children}`
when the run's `workflow_name` matches the scope. They cannot dispatch
workflow events (`POST /workflows/events`) — use a broader operator key for
that.

## Schema

Stored rows live in [`muesli_meetings`](../../services/api/db/migrations/032_add_muesli_meetings.sql):

| Column            | Notes                                                       |
| ----------------- | ----------------------------------------------------------- |
| `host`            | `MUESLI_HOST` from the originating laptop                   |
| `meeting_id`      | Muesli's local primary key — unique per host                |
| `raw_transcript`  | Speaker-labeled verbatim merge from `MuesliCore`            |
| `formatted_notes` | LLM-summarized notes, if Muesli's summary step ran          |
| `notes_state`     | `missing` / `raw_transcript_fallback` / `structured_notes`  |
| `metadata`        | JSONB — calendar id, folder, template name, etc.            |
| `workflow_run_id` | UUID of the durable run that ingested this meeting          |

`UNIQUE (host, meeting_id)` plus `trigger_key=muesli:<host>:<id>` on the
workflow call makes the whole pipeline idempotent — re-running the hook (or
Muesli replaying it on retry) will upsert in place rather than duplicating.
