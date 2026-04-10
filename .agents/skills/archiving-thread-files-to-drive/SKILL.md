---
name: archiving-thread-files-to-drive
description: "Scans a Slack thread, creates a Google Drive folder when needed, and uploads every attached file there. Use when asked to save closing docs, archive a Slack thread, or move Slack thread attachments into Drive."
---

# Archiving Thread Files To Drive

Archives every file attached anywhere in a Slack thread into Google Drive.

## When To Use It

Use this skill when the user gives you:
- A Slack thread URL or a `channel_id` + `thread_ts`
- A Google Drive destination
- A request like "save the closing docs", "archive this thread", or "put all files from this thread in Drive"

## What It Does

The bundled script:
- Parses a Slack thread URL
- Reads the root message and every reply in the thread
- Collects attached files from every message
- Deduplicates by Slack file ID
- Creates a new Drive folder when given a parent folder and folder name
- Uploads each file into the destination Drive folder
- Prints a JSON manifest with the source Slack file IDs and resulting Drive links

## Inputs

Provide one of these destination modes:
- Existing Drive folder: `--folder-id <drive_folder_id>`
- New subfolder under a parent: `--parent-folder-id <parent_id> --folder-name "<new folder name>"`

Thread input:
- Preferred: `--thread-url https://paradigm-ops.slack.com/archives/C.../p...`
- Optional direct inputs: `--channel-id C... --thread-ts 1774886843.605449`

## Primary Command

Run:

```bash
python3 .agents/skills/archiving-thread-files-to-drive/scripts/archive_thread_files_to_drive.py \
  --thread-url "https://paradigm-ops.slack.com/archives/C043LNPTMS8/p1774886843605449" \
  --parent-folder-id "1KNgF74dK6RT8z409haJYyeMttGrs0jQz" \
  --folder-name "True Anomaly - Series D Financing"
```

Use `--dry-run` first if you want to inspect the file manifest before uploading.

## Notes

- The script uses Centaur's existing `slack` and `gsuite` tools through the local API.
- It uploads the original Slack filenames to Drive.
- It continues through per-file failures and reports them in the final manifest.
- If the thread has no files, it exits without creating a destination folder.
