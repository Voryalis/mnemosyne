import io
import json
import os
from pathlib import Path
import select
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from scripts import zotero_maintenance as maintenance

if os.name == "posix":
    import pty
    import termios

ROOT = Path(__file__).resolve().parents[1]
KEY = b"Synthetic123"
MASK = "●".encode()
PROMPT = b"Zotero API key: "
# Attach a controlling terminal, then run the real CLI with a mocked transport.
PTY_CLI = """
import fcntl
import io
import json
import runpy
import sys
import termios
from unittest.mock import patch

fcntl.ioctl(0, termios.TIOCSCTTY, 0)

def mock_urlopen(request, *, timeout):
    assert request.get_method() == "GET", "Writes are forbidden in this test"
    assert request.get_header("Zotero-api-key") == "Synthetic123", "Wrong key"
    prefix = "https://api.zotero.org/groups/42/items/"
    assert request.full_url.startswith(prefix), "Wrong API endpoint"
    item, _, suffix = request.full_url[len(prefix):].partition("/")
    assert item in {"MASTER01", "DUPLIC01"}
    assert suffix in {"", "children?limit=100&start=0"}
    payload = [] if suffix else {"data": {"key": item, "itemType": "book", "version": 1}}
    return io.BytesIO(json.dumps(payload).encode())

with patch("urllib.request.urlopen", side_effect=mock_urlopen) as transport:
    sys.argv = ["scripts/zotero_maintenance.py", "--group", "42", "merge", "MASTER01", "DUPLIC01"]
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    finally:
        print(f"Mock API calls: {transport.call_count}")
"""


@unittest.skipUnless(os.name == "posix", "Requires a POSIX controlling terminal")
class MaskedKeyTests(unittest.TestCase):
    def start_cli(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        original = termios.tcgetattr(slave)
        env = os.environ.copy()
        env.pop("ZOTERO_API_KEY", None)
        process = subprocess.Popen(
            [sys.executable, "-c", PTY_CLI], cwd=ROOT, env=env,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        output = self.read_until(master, PROMPT)
        self.assertIn(b"\x1b[?2004h", output)
        self.assertFalse(termios.tcgetattr(slave)[3] & termios.ECHO)
        return process, master, slave, original, output

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    def read_until(self, master, marker):
        deadline = time.monotonic() + 5
        output = b""
        while marker not in output and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], max(0, deadline - time.monotonic()))
            if ready:
                output += os.read(master, 4096)
        self.assertIn(marker, output)
        return output

    def finish_cli(self, session, output, expected_exit, expected_calls):
        process, master, slave, original, startup = session
        output += self.read_until(master, b"Mock API calls: ")
        exit_code = process.wait(timeout=5)
        while select.select([master], [], [], 0)[0]:
            output += os.read(master, 4096)
        output = startup + output
        self.assertEqual(exit_code, expected_exit, output)
        self.assertNotIn(KEY, output)
        self.assertIn(b"\x1b[?2004l", output)
        self.assertIn(f"Mock API calls: {expected_calls}".encode(), output)
        self.assertEqual(termios.tcgetattr(slave), original)
        return output

    def test_ordinary_paste_and_backspace(self):
        session = self.start_cli()
        os.write(session[1], KEY[:-1] + b"z\x7f" + KEY[-1:] + b"\r")
        output = self.finish_cli(session, b"", 0, 4)
        self.assertIn(MASK * len(KEY) + b"\b \b" + MASK, output)
        self.assertIn(b"Dry run: no changes made.", output)

    def test_bracketed_paste_with_control_sequences(self):
        session = self.start_cli()
        master = session[1]
        os.write(master, b"\x1b[200~" + KEY + b"\r")
        output = self.read_until(master, MASK * len(KEY))
        # The pasted newline stays inside the paste. Only the subsequent Enter submits.
        os.write(master, b"\x1b[201~\x1b[27;5;9~\x1b[1;5D\x1bOA"
                         b"\x1b]0;title\x07\x1bPignored\x1b\\\r")
        output = self.finish_cli(session, output, 0, 4)
        self.assertEqual(output.count(MASK), len(KEY))
        self.assertIn(b"Dry run: no changes made.", output)

    def test_cancel_and_eof_restore_terminal(self):
        for control in (b"\x03", b"\x04"):
            with self.subTest(control=control):
                session = self.start_cli()
                os.write(session[1], KEY)
                output = self.read_until(session[1], MASK * len(KEY))
                os.write(session[1], control)
                output = self.finish_cli(session, output, 1, 0)
                self.assertIn(b"Cancelled.", output)

    def test_incomplete_escape_fails_closed(self):
        session = self.start_cli()
        os.write(session[1], KEY + b"\x1b[27;\r")
        output = self.finish_cli(session, b"", 1, 0)
        self.assertIn(b"Unsupported terminal escape", output)


class MaintenanceTests(unittest.TestCase):
    def test_read_eof(self):
        with patch.object(maintenance.os, "read", return_value=b""):
            with self.assertRaises(EOFError):
                maintenance.read_key_byte(0)

    def test_getpass_echo_fallback_is_rejected(self):
        def fallback(prompt):
            warnings.warn("Input may be echoed", maintenance.getpass.GetPassWarning)
            self.fail("getpass must stop before reading an echoed key")

        with patch.object(maintenance.getpass, "getpass", side_effect=fallback):
            with self.assertRaisesRegex(OSError, "hidden input"):
                maintenance.hidden_key("Key: ")

    def test_api_patch_uses_version_and_verifies_readback(self):
        api = maintenance.Zotero(42, KEY.decode())
        updated = {"key": "NOTEKEY1", "version": 8, "note": "<p>Updated</p>"}
        with patch.object(maintenance, "urlopen", side_effect=[
            io.BytesIO(), io.BytesIO(json.dumps({"data": updated}).encode()),
        ]) as transport:
            self.assertEqual(api.patch({"key": "NOTEKEY1", "version": 7},
                                       {"note": updated["note"]}), updated)

        write, read = [call.args[0] for call in transport.call_args_list]
        self.assertEqual(write.get_method(), "PATCH")
        self.assertEqual(write.get_header("If-unmodified-since-version"), "7")
        self.assertEqual(json.loads(write.data), {"note": updated["note"]})
        self.assertEqual(read.get_method(), "GET")
        self.assertEqual(read.full_url, write.full_url)


def response(data, **headers):
    result = io.BytesIO(json.dumps(data).encode())
    result.headers = headers
    return result


def http_error(code, **headers):
    return HTTPError("https://api.zotero.org/groups/42/items", code, "mock error", headers, None)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.api = maintenance.Zotero(42, KEY.decode(), timeout=55, max_retries=3)
        sleep = patch.object(maintenance.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        for patcher in (patch.object(maintenance, "progress"),
                        patch.object(maintenance.time, "monotonic", return_value=100)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_handshake_timeout_reset_and_5xx_retry_only_reads(self):
        errors = [URLError(ssl.SSLError("_ssl.c:1064: The handshake operation timed out")),
                  ConnectionResetError(), http_error(503)]
        with patch.object(maintenance, "urlopen", side_effect=[
            *errors, response({"data": {"key": "NOTE0001"}}),
        ]) as transport:
            self.assertEqual(self.api.item("NOTE0001"), {"key": "NOTE0001"})
        self.assertEqual(transport.call_count, 4)
        self.assertTrue(all(call.kwargs["timeout"] == 55 for call in transport.call_args_list))
        self.assertTrue(all(call.args[0].get_method() == "GET" for call in transport.call_args_list))
        for call, bounds in zip(self.sleep.call_args_list, [(0.5, 1), (1, 2), (2, 4)]):
            self.assertTrue(bounds[0] <= call.args[0] <= bounds[1])

    def test_retry_after_and_successful_backoff(self):
        with patch.object(maintenance, "urlopen", side_effect=[
            http_error(429, **{"Retry-After": "12"}), response([], Backoff="18"), response([]),
        ]):
            self.api.request("/items")
            self.api.request("/items")
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [12, 18])

    def test_retry_after_http_date_and_invalid_header(self):
        later = datetime.now(timezone.utc) + timedelta(seconds=60)
        self.assertAlmostEqual(maintenance.header_delay(format_datetime(later)), 60, delta=2)
        self.assertEqual(maintenance.header_delay("invalid"), 0)

    def test_retry_limit_and_permanent_failures(self):
        for error, requests in [(TimeoutError(), 4),
                                (URLError(OSError("_ssl.c:1064: The handshake operation timed out")), 4),
                                (http_error(403), 1),
                                (URLError(ssl.SSLCertVerificationError("certificate")), 1)]:
            with self.subTest(error=error):
                with patch.object(maintenance, "urlopen", side_effect=error) as transport:
                    with self.assertRaises(type(error)):
                        self.api.item("NOTE0001")
                self.assertEqual(transport.call_count, requests)

    def test_ambiguous_patch_reconciles_without_repeating_write(self):
        old = {"key": "NOTE0001", "version": 3, "note": "<p>Original</p>"}
        changes = {"note": "<p>Updated</p>"}
        updated = old | changes | {"version": 4}
        for readback in (updated, old, updated | {"tags": [{"tag": "Changed concurrently"}]}):
            with self.subTest(readback=readback), patch.object(maintenance, "urlopen", side_effect=[
                TimeoutError(), response({"data": readback}),
            ]) as transport:
                if readback == updated:
                    self.assertEqual(self.api.patch(old, changes), updated)
                else:
                    with self.assertRaisesRegex(RuntimeError, "not verified"):
                        self.api.patch(old, changes)
                self.assertEqual([c.args[0].get_method() for c in transport.call_args_list],
                                 ["PATCH", "GET"])


def child_note(key, parent="PARENT01", body="<p>Exact text</p>", **metadata):
    return {"key": key, "parentItem": parent, "itemType": "note", "version": 1,
            "note": body, "tags": [], "relations": {}} | metadata


class NoteDedupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.plan = Path(directory.name) / "reviewed.json"
        self.backup = Path(directory.name) / "backup.json"
        self.records = {r["key"]: r for r in [
            {"key": "PARENT01", "itemType": "book", "version": 1},
            child_note("NOTE0001"), child_note("NOTE0002"), child_note("NOTE0003"),
            child_note("UNIQUE01", body="<p>Unique research</p>"),
        ]}
        self.library_version = 10
        self.writes = []
        self.scans = 0
        self.batch_reads = 0
        self.write_mode = "success"
        self.fail_key = None
        self.concurrent_change = False
        self.change_next_page = False

    def urlopen(self, request, *, timeout):
        self.assertEqual(request.get_header("Zotero-api-key"), KEY.decode())
        target = urlsplit(request.full_url)
        self.assertEqual((target.netloc, target.path), ("api.zotero.org", "/groups/42/items"))
        query = parse_qs(target.query)
        if request.get_method() == "GET":
            self.assertEqual(query["includeTrashed"], ["1"])
            if "itemKey" in query:
                self.batch_reads += 1
                keys = query["itemKey"][0].split(",")
                return response([{"data": self.records[key]} for key in keys if key in self.records])
            start, limit = int(query["start"][0]), int(query["limit"][0])
            if not start:
                self.scans += 1
            version = self.library_version + int(bool(start and self.change_next_page))
            return response([{"data": record} for record in list(self.records.values())[start:start + limit]],
                            **{"Last-Modified-Version": str(version), "Total-Results": str(len(self.records))})
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(self.backup.is_file(), "Every write must have a pre-existing backup")
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode) & 0o077, 0)
        batch = json.loads(request.data)
        self.writes.append(batch)
        if request.get_header("If-unmodified-since-version") != str(self.library_version) or self.write_mode == "stale":
            raise http_error(412)
        change, = batch
        key = change["key"]
        self.assertEqual(change, {"key": key, "version": self.records[key]["version"], "deleted": 1})
        if key == self.fail_key:
            if self.write_mode == "timeout":
                raise URLError(TimeoutError("timed out with no write"))
            return response({"failed": {"0": {"key": key, "code": 400}}})
        self.library_version += 1
        self.records[key] = self.records[key] | {"deleted": 1, "version": self.library_version}
        if self.concurrent_change:
            self.library_version += 1
            self.records["NOTE0001"] |= {"version": self.library_version, "note": "<p>Changed</p>"}
        if self.write_mode == "timeout":
            raise URLError(TimeoutError("timed out after write"))
        return response({"successful": {"0": {"key": key, "data": self.records[key]}}})

    def run_cli(self, *options, parent_options=(), confirm="APPLY 42"):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["zotero_maintenance.py", "--group", "42", *options, "deduplicate-notes",
                *map(str, parent_options)]
        with (patch.object(sys, "argv", argv), patch.dict(os.environ, {"ZOTERO_API_KEY": KEY.decode()}),
              patch.object(maintenance, "urlopen", side_effect=self.urlopen),
              patch("builtins.input", return_value=confirm),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            maintenance.main()
        return stdout.getvalue(), stderr.getvalue()

    def save_plan(self):
        return self.run_cli(parent_options=["--save-plan", self.plan])

    def apply_plan(self, confirm="APPLY 42"):
        return self.run_cli("--apply", "--backup", str(self.backup),
                            parent_options=["--from-plan", self.plan], confirm=confirm)

    def test_preview_and_apply_preserve_unique_and_cross_parent_notes(self):
        records = [
            {"key": "PARENT02", "itemType": "book", "version": 1},
            child_note("CROSS001", parent="PARENT02"),
        ]
        self.records.update((r["key"], r) for r in records)
        output, _ = self.save_plan()
        self.assertIn("retain NOTE0001, redundant NOTE0002, NOTE0003", output)
        self.assertNotIn("CROSS001", output)
        self.assertEqual(stat.S_IMODE(self.plan.stat().st_mode) & 0o077, 0)
        self.assertEqual(self.writes, [])
        output, progress = self.apply_plan()
        self.assertIn("Completed", output)
        self.assertIn("Verified 2/2 notes in Zotero trash", progress)
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(self.batch_reads, 1)
        self.assertEqual(self.scans, 2)
        self.assertTrue(all(not self.records[k].get("deleted")
                            for k in ("NOTE0001", "CROSS001", "UNIQUE01")))
        snapshot = json.loads(self.backup.read_text())
        self.assertEqual(snapshot["kind"], "backup")
        self.assertEqual(snapshot["groups"][0]["retained"], child_note("NOTE0001"))
        self.assertEqual(snapshot["groups"][0]["redundant"],
                         [child_note("NOTE0002"), child_note("NOTE0003")])

    def test_preview_skips_conflicting_tags_relationships_and_incoming_links(self):
        for conflict, expected in [
            (child_note("NOTE0003", tags=[{"tag": "Unique tag"}]), "conflicting metadata"),
            (child_note("NOTE0003", relations={"dc:relation": "https://example.test/"}), "relationship"),
            (child_note("UNIQUE01", body='<a href="zotero://select/groups/42/items/NOTE0003">note</a>'),
             "item link"),
        ]:
            with self.subTest(expected=expected), patch.dict(self.records, {conflict["key"]: conflict}):
                output, _ = self.run_cli()
                self.assertIn(expected, output)
                self.assertIn("0 duplicate-note groups, 0 redundant notes", output)
        self.assertEqual(self.writes, [])

    def test_exact_counts_and_parent_scope(self):
        self.records["PARENT02"] = {"key": "PARENT02", "itemType": "book", "version": 1}
        with self.assertRaisesRegex(RuntimeError, "Counts differ"):
            self.run_cli(parent_options=["PARENT02", "--expect", "1", "2", "--save-plan", self.plan])
        self.assertFalse(self.plan.exists())
        output, _ = self.run_cli(parent_options=["PARENT01", "--expect", "1", "2", "--save-plan", self.plan])
        self.assertIn("1 duplicate-note groups, 2 redundant notes", output)

    def test_confirmation_and_stale_versions_stop_before_backup_or_write(self):
        self.save_plan()
        with self.assertRaisesRegex(RuntimeError, "Confirmation cancelled"):
            self.apply_plan(confirm="")
        for key in ("NOTE0001", "NOTE0002"):
            with self.subTest(key=key), patch.dict(self.records, {key: self.records[key] | {"version": 2}}):
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    self.apply_plan()
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.writes, [])

    def test_existing_backup_is_preserved_and_prevents_writes(self):
        self.save_plan()
        self.backup.write_bytes(b"Existing audit snapshot")
        with self.assertRaises(FileExistsError):
            self.apply_plan()
        self.assertEqual(self.backup.read_bytes(), b"Existing audit snapshot")
        self.assertEqual(self.writes, [])

    def test_library_precondition_rejection_never_retries_write(self):
        self.save_plan()
        self.write_mode = "stale"
        with self.assertRaises(HTTPError) as error:
            self.apply_plan()
        self.assertEqual(error.exception.code, 412)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.batch_reads, 0)
        self.assertTrue(self.backup.exists())

    def test_partial_failure_reconciles_then_resumes_only_pending_keys(self):
        for mode in ("timeout", "success"):
            with self.subTest(mode=mode):
                self.plan = self.plan.with_name(f"{mode}-plan.json")
                self.backup = self.backup.with_name(f"{mode}-backup.json")
                self.records["NOTE0002"] = child_note("NOTE0002")
                self.records["NOTE0003"] = child_note("NOTE0003")
                self.writes.clear()
                self.write_mode, self.fail_key = mode, "NOTE0003"
                self.save_plan()
                with self.assertRaisesRegex(RuntimeError, "incomplete for NOTE0003"):
                    self.apply_plan()
                self.assertEqual(len(self.writes), 2)
                before = self.backup.read_bytes()
                _, progress = self.run_cli(parent_options=["--resume", self.backup])
                self.assertIn("Already verified in trash: NOTE0002", progress)
                self.assertIn("Validated 1 pending notes", progress)
                self.assertEqual(len(self.writes), 2)
                self.write_mode, self.fail_key = "success", None
                self.run_cli("--apply", parent_options=["--resume", self.backup])
                self.assertEqual([r["key"] for r in self.writes[-1]], ["NOTE0003"])
                self.assertEqual(self.backup.read_bytes(), before)
                result, _ = self.run_cli("--apply", parent_options=["--resume", self.backup])
                self.assertIn("already verified", result)
                self.assertEqual(len(self.writes), 3)

    def test_concurrent_change_to_retained_note_prevents_next_write(self):
        self.save_plan()
        self.concurrent_change = True
        with self.assertRaises(HTTPError) as error:
            self.apply_plan()
        self.assertEqual(error.exception.code, 412)
        self.assertFalse(self.records["NOTE0003"].get("deleted"))
        with self.assertRaisesRegex(RuntimeError, "retained note changed"):
            self.run_cli("--apply", parent_options=["--resume", self.backup])
        self.assertEqual(len(self.writes), 2)

    def test_project_scale_uses_pagination_and_one_readback_batch(self):
        self.records = {}
        for num in range(19):
            parent = f"P{num:07d}"
            self.records[parent] = {"key": parent, "version": 1, "itemType": "book"}
            for index in range(3 if num < 11 else 2):
                key = f"N{num:04d}{index:03d}"
                self.records[key] = child_note(key, parent)
        for num in range(40):
            key = f"U{num:07d}"
            self.records[key] = child_note(key, "P0000000", body=str(num))
        self.change_next_page = True
        with self.assertRaisesRegex(RuntimeError, "changed during discovery"):
            self.save_plan()
        self.assertFalse(self.plan.exists())
        self.change_next_page = False
        self.run_cli(parent_options=["--expect", "19", "30", "--save-plan", self.plan])
        self.apply_plan()
        self.assertEqual([len(batch) for batch in self.writes], [1] * 30)
        self.assertEqual(self.batch_reads, 1)
