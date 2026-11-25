import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest
import warnings
from unittest.mock import patch

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
