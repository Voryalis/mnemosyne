import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from scripts import zotero_ai_evidence as archive
from scripts import zotero_maintenance as maintenance


def encoded(value):
    return json.dumps(value).encode()


class PreparationTests(unittest.TestCase):
    def test_codex_metadata_messages_and_private_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rollout-synthetic.jsonl"
            lines = [
                {"type": "session_meta", "payload": {"cli_version": "1.2.3", "timestamp": "2026-06-01T10:20:00Z"}},
                {"type": "turn_context", "payload": {"model": "mock-model-exact"}},
                {"type": "response_item", "timestamp": "2026-06-01T10:20:01Z",
                 "payload": {"type": "message", "role": "user", "content": [
                     {"type": "input_text", "text": "Review MNEMOSYNE.\nZOTERO_API_KEY=example-secret"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "Verified both checks."}]}},
                {"type": "response_item", "payload": {"type": "function_call_output", "output": "Private tool output"}},
            ]
            original = b"\n".join(map(encoded, lines))
            source.write_bytes(original)
            output = io.StringIO()
            with redirect_stdout(output):
                archive.discover([root], ["mnemosyne"])
                archive.prepare("codex", source, root / "evidence", None)
            self.assertIn(source.name, output.getvalue())
            self.assertNotIn("example-secret", output.getvalue())
            saved = root / "evidence" / "original.private"
            self.assertEqual(saved.read_bytes(), original)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(saved.parent.stat().st_mode), 0o700)
            draft = saved.with_name("review.txt").read_text()
            for value in ("1.2.3", "mock-model-exact", "2026-06-01T10:20:01Z", "Review MNEMOSYNE.",
                          "Verified both checks.", "[REDACTED]"):
                self.assertIn(value, draft)
            for value in ("example-secret", "Private tool output"):
                self.assertNotIn(value, draft)

    def test_chatgpt_selects_one_conversation_and_visible_branch(self):
        def message(role, body, **metadata):
            return {"author": {"role": role}, "create_time": 1, "metadata": metadata,
                    "content": {"parts": [body]}}

        selected = {"id": "selected", "create_time": 0, "current_node": "answer", "mapping": {
            "prompt": {"message": message("user", "Exact\nquestion")},
            "hidden": {"parent": "prompt", "message": message("assistant", "Hidden",
                                                             is_visually_hidden_from_conversation=True)},
            "tool": {"parent": "hidden", "message": message("assistant", "Tool payload") | {"recipient": "tool"}},
            "answer": {"parent": "tool", "message": message("assistant", "Exact answer",
                                                              model_slug="actual-slug")},
            "other": {"parent": "prompt", "message": message("assistant", "Abandoned branch")},
        }}
        text = archive.chatgpt_transcript(encoded([selected, {"id": "unrelated", "title": "Private"}]),
                                          "selected")
        self.assertIn("actual-slug", text)
        self.assertIn("1970-01-01T00:00:00+00:00", text)
        self.assertIn("Exact\nquestion", text)
        self.assertIn("Exact answer", text)
        self.assertIn("Recorded CLI version: Unverified", text)
        for value in ("Hidden", "Tool payload", "Abandoned branch", "Private"):
            self.assertNotIn(value, text)


    def test_manual_markdown_and_backup_capture_explicit_metadata(self):
        markdown = b"**Model:** o3-exact\nDate: 2026-10-05\n## User\nExact prompt\n## Assistant\nAnswer\n"
        report = archive.render_input("codex", Path("manual.md"), markdown, None)
        for value in ("o3-exact", "2026-10-05", "Prompt headings found: 1", "Exact prompt", "Unverified"):
            self.assertIn(value, report)
        plain = archive.render_input("chatgpt", Path("manual.txt"), b"No metadata here", None)
        self.assertIn("Model identifiers labeled in source: Unverified", plain)
        backup = archive.render_input("maintenance", Path("backup.json"),
                                      encoded({"time": "2026-10-07T12:00:00Z", "groups": []}), None)
        self.assertIn("Interaction dates labeled in source: 2026-10-07T12:00:00Z", backup)

    def test_manual_json_omits_nonconversation_roles_and_requires_selection_for_multiple(self):
        entry = {"date": "2026-10-05", "model": "test-model", "messages": [
            {"role": "developer", "content": "Excluded"},
            {"role": "user", "content": "Preserved prompt"},
        ]}
        text = archive.render_input("chatgpt", Path("manual.json"), encoded(entry), None)
        self.assertIn("Preserved prompt", text)
        self.assertNotIn("Excluded", text)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            archive.render_input("chatgpt", Path("mixed.json"), encoded([entry, entry]), None)

    @unittest.skipUnless(shutil.which("pdftotext"), "Requires Poppler")
    def test_text_pdf_is_extracted_with_poppler(self):
        stream = b"BT /F1 12 Tf 40 150 Td (Model: pdf-model) Tj 0 -20 Td (User: Exact PDF prompt) Tj ET"
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        pdf = b"%PDF-1.4\n"
        offsets = []
        for number, obj in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
        xref = len(pdf)
        pdf += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
            b"%010d 00000 n \n" % offset for offset in offsets)
        pdf += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % xref
        report = archive.render_input("chatgpt", Path("manual.pdf"), pdf, None)
        self.assertIn("Model identifiers labeled in source: pdf-model", report)
        self.assertIn("Exact PDF prompt", report)
        self.assertNotIn("\f", report)


class PlanTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.manifest = self.root / "manifest.json"
        self.entry = {"category": "maintenance", "title": "Checks", "activity": "Verify changes", "file": "review.txt"}

    def write_plan(self, data, copies=1):
        (self.root / "review.txt").write_bytes(data)
        self.manifest.write_bytes(encoded([self.entry | {"sha256": archive.sha(data)}] * copies))

    def test_default_preview_never_prompts_or_requests(self):
        self.write_plan(b"All tests passed.\n")
        with (patch.object(sys, "argv", ["evidence", "archive", str(self.manifest)]),
              patch.object(maintenance, "masked_key") as auth,
              patch.object(maintenance, "urlopen") as network,
              redirect_stdout(io.StringIO()) as output):
            archive.main()
        self.assertIn("Maintenance Evidence", output.getvalue())
        self.assertIn("Compliance Review", output.getvalue())
        self.assertIn("No Zotero requests", output.getvalue())
        auth.assert_not_called()
        network.assert_not_called()

    def test_digest_approval_and_typed_confirmation_precede_network(self):
        self.write_plan(b"Approved evidence")
        digest = archive.preview(archive.load_plan(self.manifest))["plan_sha256"]
        for supplied, error in (("wrong", SystemExit), (digest, RuntimeError)):
            with (patch.object(sys, "argv", ["evidence", "archive", str(self.manifest), "--apply", supplied]),
                  patch("builtins.input", return_value="no"),
                  patch.object(maintenance, "urlopen") as network,
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(error)):
                archive.main()
            network.assert_not_called()

    def test_manifest_builder_hashes_reviewed_files_and_refuses_overwrite(self):
        reviewed = self.root / "review.txt"
        reviewed.write_bytes(b"Reviewed attachment")
        rows = [("maintenance", str(reviewed), "Maintenance run", "Verify the recovery")]
        evidence = archive.write_manifest(self.manifest, rows)
        self.assertEqual(evidence, archive.load_plan(self.manifest))
        self.assertEqual(json.loads(self.manifest.read_text())[0]["file"], "review.txt")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            archive.write_manifest(self.manifest, rows)

    def test_modified_files_and_duplicate_bytes_are_refused(self):
        self.write_plan(b"Reviewed")
        (self.root / "review.txt").write_bytes(b"Changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            archive.load_plan(self.manifest)
        self.write_plan(b"Reviewed", copies=2)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            archive.load_plan(self.manifest)

    def test_refuses_common_secrets_and_personal_identifiers(self):
        for text in (b"ZOTERO_API_KEY=example", b"Authorization: Bearer example", b"person@example.test"):
            with self.subTest(text=text):
                self.write_plan(text)
                with self.assertRaisesRegex(ValueError, "possible secret"):
                    archive.load_plan(self.manifest)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.api = maintenance.Zotero(archive.GROUP, "synthetic-key", max_retries=0)
        self.archive = archive.EvidenceArchive(self.api)
        self.records = {"collections": {archive.ROOT: {"key": archive.ROOT, "name": "AI-use logs"}}, "items": {}}
        self.posts = []
        self.files = {}
        self.fail_registration = False
        self.corrupt_download = False
        self.evidence = archive.Evidence("codex", "Maintenance session", "Duplicate cleanup <review>",
                                        archive.sha(b"Reviewed test evidence\n"), b"Reviewed test evidence\n")

    def urlopen(self, request, *, timeout):
        self.assertEqual(request.get_header("Zotero-api-key"), "synthetic-key")
        self.assertNotIn("Zotero-api-key", request.headers)
        self.assertEqual(timeout, 30)
        url = urlsplit(request.full_url)
        self.assertEqual(url.netloc, "api.zotero.org")
        group, gid, kind, *parts = url.path.strip("/").split("/")
        self.assertEqual((group, gid), ("groups", str(archive.GROUP)))
        method = request.get_method()
        records = self.records[kind]
        if method == "GET":
            if not parts:
                self.fail("Unexpected broad retrieval")
            if parts[1:] == ["collections"]:
                return io.BytesIO(encoded([{"data": r} for r in records.values()
                                           if r.get("parentCollection") == archive.ROOT]))
            key = parts[0]
            if key not in records:
                raise HTTPError(url.geturl(), 404, "Not found", {}, None)
            if parts[1:] == ["file"]:
                return io.BytesIO(b"Wrong bytes" if self.corrupt_download else self.files[key])
            return io.BytesIO(encoded({"data": records[key]}))
        self.assertEqual(method, "POST")
        self.posts.append(url.path)
        if not parts:
            record, = json.loads(request.data)
            self.assertEqual(record["version"], 0)
            self.assertNotIn(record["key"], records)
            records[record["key"]] = record | {"version": 1}
            return io.BytesIO(encoded({"successful": {"0": {"key": record["key"]}}}))
        self.assertEqual(parts[1:], ["file"])
        self.assertEqual(request.get_header("If-none-match"), "*")
        key = parts[0]
        fields = parse_qs(request.data.decode())
        if "upload" in fields:
            self.assertEqual(fields, {"upload": ["test-upload-key"]})
            records[key]["md5"] = self.pending[1]
            self.files[key] = self.evidence.content
            if self.fail_registration:
                raise TimeoutError()
            return io.BytesIO()
        self.pending = key, fields["md5"][0]
        self.assertEqual(fields["filename"], [self.evidence.filename])
        self.assertEqual(fields["filesize"], [str(len(self.evidence.content))])
        return io.BytesIO(encoded({"url": "https://storage.zotero.org/upload", "contentType": "multipart/form-data",
                                   "prefix": "START", "suffix": "END", "uploadKey": "test-upload-key"}))

    def run_archive(self):
        with (patch.object(maintenance, "urlopen", side_effect=self.urlopen),
              patch.object(archive, "storage_post") as upload,
              redirect_stdout(io.StringIO())):
            self.archive.archive([self.evidence], archive.preview([self.evidence])["plan_sha256"])
        return upload

    def test_upload_verify_and_rerun_without_duplicate_writes(self):
        self.fail_registration = True
        uploaded = self.run_archive()
        uploaded.assert_called_once_with("https://storage.zotero.org/upload", "multipart/form-data",
                                         b"STARTReviewed test evidence\nEND", 30)
        items = self.records["items"]
        attachment = next(r for r in items.values() if r["itemType"] == "attachment")
        note = next(r for r in items.values() if r["itemType"] == "note")
        self.assertIn(attachment["key"], note["note"])
        self.assertIn("Duplicate cleanup &lt;review&gt;", note["note"])
        self.assertEqual(len(self.records["collections"]), 5)
        count = len(self.posts)
        self.run_archive().assert_not_called()
        self.assertEqual(count, len(self.posts))
        note["note"] = '<div data-schema-version="9">' + note["note"] + "</div>"
        self.run_archive().assert_not_called()
        self.assertEqual(count, len(self.posts))

    def test_byte_verification_prevents_index_creation(self):
        self.corrupt_download = True
        with self.assertRaisesRegex(RuntimeError, "bytes did not verify"):
            self.run_archive()
        self.assertTrue(all(r["itemType"] != "note" for r in self.records["items"].values()))

    def test_destination_safety_rejects_unexpected_storage_host(self):
        for url in ("http://storage.zotero.org/file", "https://example.com/file",
                    "https://name@storage.zotero.org/file"):
            with self.subTest(url=url), patch.object(archive, "build_opener") as opener:
                with self.assertRaisesRegex(ValueError, "storage URL"):
                    archive.storage_post(url, "text/plain", b"reviewed", 30)
                opener.assert_not_called()
