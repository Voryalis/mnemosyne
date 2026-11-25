# Opt-in Zotero maintenance (experimental)

The Java MCP server remains read-only. This standalone Python 3.10+ utility uses the Zotero Web API with a separate write-enabled key. Never commit credentials, backups or private research data.

Create a restricted group-write key at https://www.zotero.org/settings/keys. When `ZOTERO_API_KEY` is unset, the utility prompts for the key with hidden input. On a POSIX terminal, it displays one bullet per character and accepts typing or paste. Press Enter to submit, Backspace to correct a character, or Ctrl+C or Ctrl+D to cancel.

## Network retries

Set `--timeout SECONDS` and `--max-retries N` before the subcommand to change the defaults of 30 seconds per request and four additional read attempts. Reads retry transient connection failures, TLS handshake timeouts, HTTP 429, and HTTP 5xx with exponential backoff and jitter. The client honors Zotero's `Backoff` and `Retry-After` headers. Writes are sent once. If an acknowledgement is lost, the CLI checks server state before it can continue.

## Preview a consolidation

```sh
python3 scripts/zotero_maintenance.py --group GROUP_ID merge MASTER01 DUPLIC01
```

Replace the example IDs with actual Zotero keys. The first key is the master. Review every metadata conflict before proceeding. The script fills empty master fields, preserves master values on conflicts, unions tags and collections, and reparents existing child notes and attachments rather than copying them. It checks attachment child keys after reparenting. Duplicate parents are retained unless --trash is explicitly requested.

```sh
python3 scripts/zotero_maintenance.py --group GROUP_ID --apply --backup /private/path/audit.json merge MASTER01 DUPLIC01 --trash
```

The command requires typed confirmation and creates a new private metadata snapshot before writes. It is not an atomic transaction: an API failure can leave partially applied changes. Stop and review the server state and backup before retrying. The backup is JSON metadata, not a binary attachment backup; independently back up Zotero storage first. The script does not implement Zotero Desktop native merge relations or citation redirects. For records cited in existing documents, prefer native Zotero merging. Existing relations on duplicate parents cause refusal.

## Update a note

```sh
python3 scripts/zotero_maintenance.py --group GROUP_ID note NOTEKEY1 /private/path/note.html
python3 scripts/zotero_maintenance.py --group GROUP_ID --apply --backup /private/path/note-backup.json note NOTEKEY1 /private/path/note.html
```

Review HTML and its evidence before applying. This replaces the note body only. The MCP duplicate detector can supply bibliographic candidates.

## Deduplicate child notes

The preview groups notes by their exact stored HTML and parent reference. It compares tags, collections, relations, and other editable fields, ignoring note keys, versions, and timestamps for duplicate detection. It retains the oldest copy, using the item key to break ties. Distinct note content and standalone notes are preserved. It skips groups with conflicting metadata, existing relationships, child items, or recognized incoming Zotero item links in the visible library. References from other libraries and external documents are outside the scan.

For Market Research Analysis (group `6757728`), use the following commands from the repository on the feature branch.

```sh
mkdir -p -m 700 "$HOME/.local/share/mnemosyne"
python3 scripts/zotero_maintenance.py --group 6757728 --timeout 60 --max-retries 5 \
  deduplicate-notes --expect 19 30 \
  --save-plan "$HOME/.local/share/mnemosyne/group-6757728-note-plan.json"
```

The preview prints the retained and redundant keys. Compare them to your validated list before applying. `--expect 19 30` saves the plan only if the scan finds 19 duplicate-note groups and 30 redundant notes. If counts differ, run a preview without `--save-plan` and `--expect` to investigate. Optionally list known parent keys immediately after `deduplicate-notes` to narrow discovery. The saved plan contains private note content.

```sh
python3 scripts/zotero_maintenance.py --group 6757728 --timeout 60 --max-retries 5 \
  --apply --backup "$HOME/.local/share/mnemosyne/group-6757728-note-backup.json" \
  deduplicate-notes --from-plan "$HOME/.local/share/mnemosyne/group-6757728-note-plan.json"
```

Type `APPLY 6757728` after reviewing the keys. The CLI revalidates the saved plan, then creates an exclusive private JSON backup containing every retained and redundant note snapshot before the first write. Each trash operation has library and item version checks. The API applies individual changes, and the CLI verifies the trash state with batched reads. Use a new path for each plan or backup. Keep the files private and leave their contents intact.

### Recover after a timeout or partial application

If the backup was created, reconcile it in dry-run mode first.

```sh
python3 scripts/zotero_maintenance.py --group 6757728 --timeout 60 --max-retries 5 \
  deduplicate-notes --resume "$HOME/.local/share/mnemosyne/group-6757728-note-backup.json"
```

To continue with the unchanged pending keys, rerun with `--apply` before the subcommand and confirm again.

```sh
python3 scripts/zotero_maintenance.py --group 6757728 --timeout 60 --max-retries 5 \
  --apply deduplicate-notes \
  --resume "$HOME/.local/share/mnemosyne/group-6757728-note-backup.json"
```

Resume keeps the original backup and skips notes already verified in the trash. If a retained copy, redundant note, or relationship has changed, stop and review the current records and snapshot. If the run ended before backup creation, no trash request was sent. Use the reviewed plan again, or preview a fresh plan if records changed. To undo a completed removal, restore the affected notes from Zotero Trash using the keys in the backup.

### Validation on group 6757728

On 25 November 2026, the preview found 19 duplicate-note groups with 30 redundant notes. The first apply stopped after an unacknowledged write. A read-only recovery check found all 30 notes still pending, and applying from the original backup verified all 30 in Zotero Trash.

This experimental utility requires testing with disposable group records before use on production research. It does not add write tools to ChatGPT; deployment of a write-enabled MCP interface would be separate.

See [Archive AI-use evidence in Zotero](zotero-ai-evidence.md) for the separate import workflow.

API reference: https://www.zotero.org/support/dev/web_api/v3/write_requests
