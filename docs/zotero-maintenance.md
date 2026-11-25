# Opt-in Zotero maintenance (experimental)

The Java MCP server remains read-only. This standalone Python 3.10+ utility uses the Zotero Web API with a separate write-enabled key. Never commit credentials, backups or private research data.

Create a restricted group-write key at https://www.zotero.org/settings/keys. When `ZOTERO_API_KEY` is unset, the utility prompts for the key with hidden input. On a POSIX terminal, it displays one bullet per character and accepts typing or paste. Press Enter to submit, Backspace to correct a character, or Ctrl+C or Ctrl+D to cancel.

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

Review HTML and its evidence before applying. This replaces the note body only. Note creation and automatic duplicate detection are not implemented by this utility; the existing MCP duplicate detector can supply candidates.

This experimental utility requires testing with disposable group records before use on production research. It does not add write tools to ChatGPT; deployment of a write-enabled MCP interface would be separate.

API reference: https://www.zotero.org/support/dev/web_api/v3/write_requests
