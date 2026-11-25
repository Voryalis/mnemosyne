# Archive AI-use evidence in Zotero

`scripts/zotero_ai_evidence.py` is an opt-in CLI for group `6757728`. Its destination is the existing **AI-use logs** collection (`3ALQQNFN`). On apply it reuses or creates **ChatGPT Interaction Records**, **Codex Interaction Records**, **Maintenance Evidence**, and **Compliance Review** under that collection. It creates an index note in Compliance Review that links each verified stored file to its AI-use activity.

## Find and prepare sources

Save each shared conversation as a local PDF, TXT, Markdown, or JSON file, or use the uploaded file's path in the managed workspace. You can prepare manually supplied Codex transcripts in the same formats, including native JSONL rollouts. Session directories are optional. If available, `discover --match MNEMOSYNE --match Zotero` can list candidate rollouts without printing messages.

Use a fresh staging directory outside the checkout:

```sh
mkdir -p -m 700 "$HOME/.local/share/mnemosyne/evidence"
python3 scripts/zotero_ai_evidence.py prepare chatgpt "$HOME/Downloads/chatgpt-export.pdf" \
  "$HOME/.local/share/mnemosyne/evidence/chatgpt-session"
python3 scripts/zotero_ai_evidence.py prepare codex "$HOME/Downloads/codex-transcript.md" \
  "$HOME/.local/share/mnemosyne/evidence/codex-session"
python3 scripts/zotero_ai_evidence.py prepare maintenance "$HOME/Downloads/zotero-backup.json" \
  "$HOME/.local/share/mnemosyne/evidence/maintenance-backup"
python3 scripts/zotero_ai_evidence.py prepare maintenance "$HOME/Downloads/zotero-execution.log" \
  "$HOME/.local/share/mnemosyne/evidence/maintenance-log"
```

PDF extraction uses Poppler's `pdftotext` (for example, `sudo pacman -S poppler` on Arch Linux or `brew install poppler` on macOS). For scanned or image-only PDFs, supply a reviewed OCR text copy. PDF layout extraction can change wrapping, so check prompts against the original.

For a ChatGPT export ZIP, extract `conversations.json` privately. With multiple conversations, add `--conversation-id ID` to select exactly one. Native ChatGPT JSON renders the active branch and visible user and assistant text. A manually saved JSON conversation can also use a `messages` array of objects with `role`, `content`, and optional `timestamp` and `model` strings, with optional top-level `id`, `date`, `model`, and `cli_version` fields. Native Codex JSONL extracts the metadata and messages recorded in the rollout. PDF, TXT, and Markdown preserve source text and summarize explicit metadata labels and prompt headings where present. Missing fields appear as **Unverified**. A label in a manual transcript is a source claim, with no independent verification of the underlying model or date. Source files can be up to 128 MiB, and each approved text attachment can be up to 10 MiB.

For compliance records, use `prepare compliance`. To archive Git evidence, put reviewed commit IDs and repository links in a text file and prepare it as maintenance evidence.

Each new source directory is private and contains an unchanged `original.private` and a separate readable `review.txt`. Keep originals and staging files outside Git. The redactor removes common credential patterns and some personal identifiers. Review every line of each draft, including transcript excerpts, note bodies, command output, links, and filenames embedded in text. Remove unrelated material and any confidential information the redactor missed. Approve only the reviewed text and metadata. Never add a raw export or backup to the manifest.

## Preview a reviewed import

After reviewing and editing each draft, generate the private manifest. Repeat `--entry` for each approved file and omit entries for any files you have yet to review.

```sh
python3 scripts/zotero_ai_evidence.py manifest "$HOME/.local/share/mnemosyne/evidence/manifest.json" \
  --entry chatgpt "$HOME/.local/share/mnemosyne/evidence/chatgpt-session/review.txt" \
  "ChatGPT source audit" "Review bibliographic sources and claims" \
  --entry codex "$HOME/.local/share/mnemosyne/evidence/codex-session/review.txt" \
  "Codex maintenance session" "Develop and verify the Zotero maintenance workflow" \
  --entry maintenance "$HOME/.local/share/mnemosyne/evidence/maintenance-log/review.txt" \
  "Zotero execution log" "Record the maintenance outcome"
```

The manifest command hashes the reviewed files and refuses to overwrite an existing manifest. Use a new manifest name if you change the reviewed files or metadata. To repeat the preview:

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
