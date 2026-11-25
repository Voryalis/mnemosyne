# Archive AI-use evidence in Zotero

`scripts/zotero_ai_evidence.py` is an opt-in CLI for group `6757728`. Its destination is the existing **AI-use logs** collection (`3ALQQNFN`). On apply it reuses or creates **ChatGPT Interaction Records**, **Codex Interaction Records**, **Maintenance Evidence**, and **Compliance Review** under that collection. It creates an index note in Compliance Review that links each verified stored file to its AI-use activity.

## Find and prepare sources

Run discovery on the machine where Codex saved its local sessions. Filenames and recorded dates are candidate evidence for review. A keyword match alone is insufficient to establish relevance.

```sh
python3 scripts/zotero_ai_evidence.py discover \
  --match MNEMOSYNE --match Zotero --match 'Anhui Huiliao'
```

Discovery checks `sessions` and `archived_sessions` under `CODEX_HOME` or `~/.codex`. Repeat `--root` to supply other directories. The CLI prints candidate paths and dates without printing message text.

Use a fresh staging directory outside the checkout, with a separate subdirectory per source:

```sh
mkdir -p -m 700 "$HOME/.local/share/mnemosyne/evidence"
python3 scripts/zotero_ai_evidence.py prepare codex /path/to/rollout-SESSION.jsonl \
  "$HOME/.local/share/mnemosyne/evidence/codex-session"

python3 scripts/zotero_ai_evidence.py prepare chatgpt /path/to/conversations.json \
  "$HOME/.local/share/mnemosyne/evidence/chatgpt-session" --conversation-id CONVERSATION_ID

python3 scripts/zotero_ai_evidence.py prepare maintenance /path/to/execution.log \
  "$HOME/.local/share/mnemosyne/evidence/maintenance-log"
```

For a ChatGPT export ZIP, extract `conversations.json` privately and select one conversation by its exported ID. The renderer uses the active conversation branch and visible user and assistant text. Codex rendering extracts recorded model identifiers, CLI version, session date, and user and assistant text from the rollout. Fields absent from the export appear as unavailable. The renderers omit tool calls, tool output, hidden messages, and non-text attachments. Source files can be up to 128 MiB, and each approved text attachment can be up to 10 MiB.

For a maintenance JSON backup or compliance record, use `prepare maintenance` or `prepare compliance`. For Git evidence, make a text file from reviewed commit IDs and repository commit links, then prepare it as maintenance evidence. You can get an exact ID with `git rev-parse COMMIT`.

Each new source directory is private and contains an unchanged `original.private` and a separate readable `review.txt`. Keep originals and staging files outside Git. The redactor removes common credential patterns and some personal identifiers. Review every line of each draft, including transcript excerpts, note bodies, command output, links, and filenames embedded in text. Remove unrelated material and any confidential information the redactor missed. Approve only the reviewed text and metadata. Never add a raw export or backup to the manifest.

## Preview a reviewed import

Calculate SHA-256 after editing each approved text file. Put the manifest outside Git, alongside the evidence directories. For example:

```json
[
  {
    "category": "codex",
    "title": "Codex-assisted maintenance verification",
    "activity": "Ran regression tests for duplicate-note recovery",
    "file": "codex-session/review.txt",
    "sha256": "REPLACE_WITH_SHA256_OF_THE_REVIEWED_FILE"
  }
]
```

Use `sha256sum codex-session/review.txt` on Linux or `shasum -a 256 codex-session/review.txt` on macOS. Categories are `chatgpt`, `codex`, `maintenance`, and `compliance`.

```sh
python3 scripts/zotero_ai_evidence.py archive "$HOME/.local/share/mnemosyne/evidence/manifest.json"
```

The preview is offline. It displays the destination, proposed subcollections, attachment names, activities, sizes, and hashes. It does not inspect existing remote records. Review the entries and the staged text before using the printed plan hash.

## Apply or resume

Group file editing and Zotero storage must be available for the API key. The CLI uses the maintenance tool's masked prompt or `ZOTERO_API_KEY`. It reuses configurable read retries and respects Zotero's backoff headers.

```sh
python3 scripts/zotero_ai_evidence.py archive "$HOME/.local/share/mnemosyne/evidence/manifest.json" \
  --apply REVIEWED_PLAN_SHA256 --timeout 60 --max-retries 5
```

Type `APPLY 6757728` when prompted. The plan hash binds the destination, approved content hashes, titles, and activities. The CLI sends only the approved bytes. It checks file metadata and downloads each stored attachment to verify its SHA-256 before creating the index. The API key is excluded from the storage upload and redirected file requests.

Reusing the same reviewed manifest reuses attachments by content and the index by plan hash. A changed redaction has a different content hash. The CLI stops if an existing record at the expected key conflicts with the proposal. If an import stops partway, preserve the manifest and reviewed files. Inspect the reported key, then rerun the same reviewed plan to reuse verified records and fill missing uploads. An upload can leave a metadata-only record, and the rerun can finish it. The CLI leaves existing records and files in place.
