#!/usr/bin/env python3
"""Opt-in Zotero Web API maintenance. Dry-run by default. Python 3.10+."""
import argparse
import errno
import getpass
import json
import math
import os
import random
import re
import socket
import ssl
import sys
import time
import warnings
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

KEY = re.compile(r"^[A-Z0-9]{8}$")


def progress(message):
    print(message, file=sys.stderr, flush=True)


def transient_error(error):
    if isinstance(error, HTTPError):
        return error.code == 429 or 500 <= error.code < 600
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(reason, (TimeoutError, ConnectionError, IncompleteRead)):
        return True
    if isinstance(reason, socket.gaierror):
        return reason.errno == socket.EAI_AGAIN
    if isinstance(reason, OSError) and reason.errno in {
        errno.ETIMEDOUT, errno.ENETUNREACH, errno.EHOSTUNREACH,
        errno.ENETDOWN, errno.ENETRESET,
    }:
        return True
    # urllib can wrap a TLS handshake timeout as an SSL error or a string.
    message = str(reason).lower()
    return ("timed out" in message
            and (isinstance(reason, (ssl.SSLError, str))
                 or (isinstance(reason, OSError) and "handshake" in message)))


def header_delay(value):
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


class Zotero:
    def __init__(self, group, token, *, timeout=30, max_retries=4):
        self.group = group
        self.base = f"https://api.zotero.org/groups/{group}"
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.not_before = 0

    def observe_backoff(self, headers):
        if headers:
            delay = max(header_delay(headers.get("Backoff")),
                        header_delay(headers.get("Retry-After")))
            if delay:
                self.not_before = max(self.not_before, time.monotonic() + delay)

    def request(self, path, method="GET", data=None, version=None, *, with_headers=False):
        headers = {"Zotero-API-Key": self.token, "Zotero-API-Version": "3"}
        if version is not None:
            headers["If-Unmodified-Since-Version"] = str(version)
        payload = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(data).encode()
        request = Request(self.base + path, payload, headers, method=method)
        retries = self.max_retries if method == "GET" else 0
        for attempt in range(retries + 1):
            delay = self.not_before - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    response_headers = getattr(response, "headers", {})
                    self.observe_backoff(response_headers)
                    raw = response.read()
                    result = json.loads(raw) if raw else None
                    return (result, response_headers) if with_headers else result
            except (OSError, IncompleteRead) as error:
                if isinstance(error, HTTPError):
                    self.observe_backoff(error.headers)
                    error.close()
                if attempt == retries or not transient_error(error):
                    raise
                ceiling = min(30, 2 ** min(attempt, 5))
                jitter = random.uniform(ceiling / 2, ceiling)
                now = time.monotonic()
                self.not_before = max(self.not_before, now + jitter)
                progress(f"Zotero read interrupted ({type(error).__name__}). "
                         f"Retry {attempt + 1}/{retries} in {self.not_before - now:.1f}s.")

    def library_items(self):
        """Read a consistent paginated snapshot, including trash for recovery."""
        records = {}
        snapshot_version = None
        total = None
        while total is None or len(records) < total:
            start = len(records)
            page, headers = self.request(
                f"/items?includeTrashed=1&limit=100&start={start}", with_headers=True)
            try:
                version = int(headers["Last-Modified-Version"])
                count = int(headers["Total-Results"])
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError("Zotero did not return pagination/version headers") from error
            if snapshot_version is None:
                snapshot_version, total = version, count
            if version != snapshot_version or count != total or count < 0:
                raise RuntimeError("Library changed during discovery; rerun the scan")
            for entry in page:
                record = entry["data"]
                records[record["key"]] = record
            if len(records) != start + len(page) or len(records) > total or (
                    not page and len(records) < total):
                raise RuntimeError("Zotero returned inconsistent item pages; rerun the scan")
            progress(f"Scanned {len(records)}/{total} Zotero items.")
        return records, snapshot_version

    def write_once(self, path, method, data, version):
        try:
            return self.request(path, method, data, version), None
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            progress("Unreadable write response. Checking the server before continuing.")
            return None, error
        except (OSError, IncompleteRead) as error:
            if not transient_error(error):
                raise
            progress("Zotero did not acknowledge the write. Checking the server before continuing.")
            return None, error

    def readback(self, path):
        try:
            return self.request(path)
        except (OSError, IncompleteRead, ValueError) as error:
            raise RuntimeError("Write could not be verified. Check server state before retrying") from error

    def verify_trashed(self, records, write_error=None):
        query = urlencode({"itemKey": ",".join(r["key"] for r in records),
                           "includeTrashed": 1, "limit": len(records)})
        page = self.readback(f"/items?{query}")
        try:
            actual = {entry["data"]["key"]: entry["data"] for entry in page}
        except (KeyError, TypeError) as error:
            raise RuntimeError("Unexpected trash readback response; review and resume from the backup") from error
        pending = [r["key"] for r in records if not already_trashed(r, actual.get(r["key"]))]
        if pending:
            raise RuntimeError("Trash readback incomplete for " + ", ".join(pending)
                               + ". Review and resume from the backup") from write_error
        return actual

    def trash_notes(self, records, library_version):
        # Zotero enforces the library precondition only on the first item in a
        # POST. Use one item per write to guard the retained copy every time.
        for number, original in enumerate(records, start=1):
            key = original["key"]
            progress(f"Moving note {number}/{len(records)} to Zotero trash: {key}")
            change = {"key": key, "version": original["version"], "deleted": 1}
            report, error = self.write_once("/items", "POST", [change], library_version)
            try:
                fresh = report["successful"]["0"]["data"]
            except (TypeError, KeyError):
                fresh = None
            if not already_trashed(original, fresh):
                fresh = self.verify_trashed([original], error)[key]
            # Only advance the guard by our own write. Incorporating a later
            # unrelated version could hide a change to a retained note.
            if fresh["version"] != library_version + 1:
                raise RuntimeError(f"Unexpected version after trashing {key}; review and resume")
            library_version = fresh["version"]

        for start in range(0, len(records), 50):
            batch = records[start:start + 50]
            self.verify_trashed(batch)
            progress(f"Verified {start + len(batch)}/{len(records)} notes in Zotero trash.")

    def item(self, key):
        return self.request(f"/items/{key}")["data"]

    def children(self, key):
        result = []
        while True:
            page = self.request(f"/items/{key}/children?limit=100&start={len(result)}")
            result.extend(entry["data"] for entry in page)
            if len(page) < 100:
                return result

    def patch(self, old, changes):
        path = f"/items/{old['key']}"
        _, write_error = self.write_once(path, "PATCH", changes, old["version"])
        fresh = self.readback(path)["data"]
        if write_error is not None and (
                fresh["version"] <= old["version"]
                or fresh.get("dateAdded") != old.get("dateAdded")
                or comparable_metadata(fresh) != comparable_metadata(old | changes)
                or fresh.get("deleted", 0) != changes.get("deleted", old.get("deleted", 0))):
            raise RuntimeError(f"Write to {old['key']} was not verified; review before retrying") from write_error
        for field, value in changes.items():
            if fresh.get(field) != value:
                raise RuntimeError(f"Readback mismatch: {old['key']} {field}") from write_error
        return fresh


def backup(data, path):
    # Exclusive creation with private permissions; never overwrite an audit snapshot.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    if hasattr(os, "O_DIRECTORY"):
        directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def union(values):
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def plan_merge(api, keys):
    records = [api.item(key) for key in keys]
    if any(r.get("deleted") or r.get("parentItem") or
           r["itemType"] in ("note", "attachment", "annotation") for r in records):
        raise ValueError("Only active top-level bibliographic records are supported")
    if len({r["itemType"] for r in records}) != 1:
        raise ValueError("Item types differ; review manually")
    master = records[0]
    children = {r["key"]: api.children(r["key"]) for r in records}
    descendants = {}
    for items in children.values():
        for item in items:
            if item["itemType"] not in ("note", "attachment") or item.get("deleted"):
                raise ValueError("Unexpected or trashed child; review manually")
            if item["itemType"] == "attachment":
                descendants[item["key"]] = api.children(item["key"])
    changes = {}
    conflicts = {}
    ignore = {"key", "version", "dateAdded", "dateModified", "itemType",
              "collections", "tags", "relations", "deleted", "parentItem"}
    for field in set().union(*(r.keys() for r in records)) - ignore:
        vals = union([r[field] for r in records if r.get(field)])
        if len(vals) > 1:
            conflicts[field] = vals
        if not master.get(field) and vals:
            changes[field] = vals[0]
    for field in ("tags", "collections"):
        merged = union([v for r in records for v in r.get(field, [])])
        if merged != master.get(field, []):
            changes[field] = merged
    relations = {}
    for record in records:
        for field, value in record.get("relations", {}).items():
            relations[field] = union(relations.get(field, []) +
                                     (value if isinstance(value, list) else [value]))
    # Do not manufacture native Zotero merge relations or citation redirects.
    if any(r.get("relations") for r in records[1:]):
        raise ValueError("Duplicate has existing relations; use native Zotero merge")
    return {"records": records, "children": children,
            "attachment_descendants": descendants, "changes": changes,
            "conflicts": conflicts}


def apply_merge(api, plan, trash):
    master, *duplicates = plan["records"]
    # Reject stale snapshots before the first write.
    for record in plan["records"]:
        if api.item(record["key"])["version"] != record["version"]:
            raise RuntimeError("Item changed since preview; restart")
        current = api.children(record["key"])
        expected = plan["children"][record["key"]]
        if {(c["key"], c["version"]) for c in current} != {
                (c["key"], c["version"]) for c in expected}:
            raise RuntimeError("Children changed since preview; restart")
    if plan["changes"]:
        api.patch(master, plan["changes"])
        print("Updated master", master["key"], flush=True)
    for record in duplicates:
        for child in plan["children"][record["key"]]:
            api.patch(child, {"parentItem": master["key"]})
            print("Moved child", child["key"], flush=True)
            if child["itemType"] == "attachment":
                expected = {x["key"] for x in plan["attachment_descendants"][child["key"]]}
                actual = {x["key"] for x in api.children(child["key"])}
                if expected != actual:
                    raise RuntimeError("Attachment annotation/child mismatch; stop")
    if trash:
        for record in duplicates:
            if api.children(record["key"]):
                raise RuntimeError("Duplicate still has children; refusing to trash")
            current = api.item(record["key"])
            for field, value in record.items():
                if field not in ("version", "dateModified") and current.get(field) != value:
                    raise RuntimeError("Duplicate metadata changed; refusing to trash")
            api.patch(current, {"deleted": 1})
            print("Trashed", record["key"], flush=True)


def bibliographic_parent(record):
    return (record and not record.get("deleted") and not record.get("parentItem")
            and record.get("itemType") not in {None, "note", "attachment", "annotation"})


def note_families(records, parents=()):
    families = {}
    for record in records.values():
        parent = record.get("parentItem")
        body = record.get("note")
        if (record.get("itemType") == "note" and not record.get("deleted")
                and isinstance(body, str) and bibliographic_parent(records.get(parent))
                and (not parents or parent in parents)):
            families.setdefault((parent, body), []).append(record)
    return families


def comparable_metadata(record):
    """Compare editable data conservatively, ignoring only identity and timestamps."""
    metadata = {field: value for field, value in record.items()
                if field not in {"key", "version", "dateAdded", "dateModified", "deleted"}}
    for field in ("collections", "tags"):
        metadata[field] = sorted(metadata.get(field, []), key=lambda value: json.dumps(value, sort_keys=True))
    # Empty relations can be omitted or returned as an empty object.
    metadata["relations"] = metadata.get("relations") or {}
    return metadata


def already_trashed(original, current):
    return (isinstance(current, dict) and current.get("deleted") == 1
            and current.get("key") == original["key"] and type(current.get("version")) is int
            and current["version"] > original["version"]
            and current.get("dateAdded") == original.get("dateAdded")
            and comparable_metadata(current) == comparable_metadata(original))


def protected_note_keys(records, group):
    """Find item references within the visible library, including its trash."""
    item_uri = re.compile(rf"/groups/{group}/(?:[^/\s]+/)?items/([A-Z0-9]{{8}})(?![A-Za-z0-9])")
    referenced = set()
    for record in records.values():
        parent = record.get("parentItem")
        if parent:
            referenced.add(parent)
        for targets in (record.get("relations") or {}).values():
            for url in targets if isinstance(targets, list) else [targets]:
                if isinstance(url, str):
                    referenced.update(item_uri.findall(url))
        if record.get("itemType") == "note":
            referenced.update(item_uri.findall(record.get("note") or ""))
    return referenced


def note_conflict(family, protected):
    if any(record.get("relations") or record["key"] in protected for record in family):
        return "existing relationship, item link, or child item"
    metadata = comparable_metadata(family[0])
    if any(comparable_metadata(record) != metadata for record in family[1:]):
        return "same content with conflicting metadata"
    return None


def plan_note_dedup(api, parents):
    progress("Discovering child-note duplicates.")
    records, _ = api.library_items()
    for parent in parents:
        if not bibliographic_parent(records.get(parent)):
            raise ValueError(f"Expected an active top-level reference for {parent}")
    families = note_families(records, set(parents))
    protected = protected_note_keys(records, api.group)
    groups = []
    for (parent, _), family in families.items():
        if len(family) < 2:
            continue
        conflict = note_conflict(family, protected)
        if conflict:
            keys = ", ".join(sorted(record["key"] for record in family))
            print(f"Skipped {parent} ({keys}): {conflict}")
            continue
        retained, *redundant = sorted(family, key=lambda r: (r.get("dateAdded") or "", r["key"]))
        groups.append({"retained": retained, "redundant": redundant})
    groups.sort(key=lambda g: (g["retained"]["parentItem"], g["retained"]["key"]))
    return groups


def load_note_snapshot(path, group, kind):
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(snapshot, dict) or snapshot.get("format") != 1
            or snapshot.get("group") != group or snapshot.get("kind") != kind
            or not isinstance(snapshot.get("groups"), list)):
        raise ValueError(f"Expected a {kind} snapshot for group {group}")
    seen = set()
    for family in snapshot["groups"]:
        if (not isinstance(family, dict) or not isinstance(family.get("redundant"), list)
                or not family["redundant"]):
            raise ValueError("Invalid duplicate-note group in snapshot")
        records = [family.get("retained"), *family["redundant"]]
        for record in records:
            if (not isinstance(record, dict) or record.get("itemType") != "note"
                    or record.get("deleted") or not isinstance(record.get("key"), str)
                    or not KEY.fullmatch(record["key"])
                    or not isinstance(record.get("parentItem"), str)
                    or not KEY.fullmatch(record["parentItem"])
                    or not isinstance(record.get("note"), str)
                    or type(record.get("version")) is not int or record["version"] < 1
                    or record["key"] in seen):
                raise ValueError("Invalid or repeated note in snapshot")
            seen.add(record["key"])
        if note_conflict(records, set()):
            raise ValueError("Conflicting notes in snapshot")
    return snapshot["groups"]


def validate_note_plan(api, groups, *, resuming):
    progress(f"Validating {len(groups)} duplicate-note groups against current Zotero state.")
    records, version = api.library_items()
    families = note_families(records)
    protected = protected_note_keys(records, api.group)
    pending = []
    for group in groups:
        retained = group["retained"]
        parent = retained["parentItem"]
        if not bibliographic_parent(records.get(parent)) or records.get(retained["key"]) != retained:
            raise RuntimeError(f"Parent or retained note changed for {retained['key']}; stop and review")
        family = families[(parent, retained["note"])]
        if note_conflict(family, protected):
            raise RuntimeError(f"Conflicting metadata or relationships for {retained['key']}; stop and review")
        for original in group["redundant"]:
            key = original["key"]
            current = records.get(key)
            if resuming and already_trashed(original, current):
                progress(f"Already verified in trash: {key}")
            elif current == original:
                pending.append(original)
            else:
                raise RuntimeError(f"Redundant note {key} changed or disappeared; stop and review")
    progress(f"Validated {len(pending)} pending notes.")
    return pending, version


def confirm_apply(group):
    confirmation = f"APPLY {group}"
    if input(f"Type {confirmation} to confirm: ") != confirmation:
        raise RuntimeError("Confirmation cancelled")


def deduplicate_notes(api, args):
    if args.from_plan or args.resume:
        kind = "backup" if args.resume else "plan"
        groups = load_note_snapshot(args.resume or args.from_plan, api.group, kind)
    else:
        groups = plan_note_dedup(api, args.parents)

    count = sum(len(group["redundant"]) for group in groups)
    for group in groups:
        retained = group["retained"]
        redundant = ", ".join(record["key"] for record in group["redundant"])
        print(f"{retained['parentItem']}: retain {retained['key']}, redundant {redundant}")
    print(f"{len(groups)} duplicate-note groups, {count} redundant notes.", flush=True)
    if args.expect and [len(groups), count] != args.expect:
        raise RuntimeError("Counts differ from --expect; review the preview before proceeding")

    snapshot = {"format": 1, "group": api.group, "kind": "plan",
                "time": datetime.now(timezone.utc).isoformat(), "groups": groups}
    if args.save_plan:
        backup(snapshot, args.save_plan)
        print("Saved private plan:", args.save_plan)
    if not args.apply:
        if args.resume or args.from_plan:
            validate_note_plan(api, groups, resuming=bool(args.resume))
        print("Dry run: no changes made.")
        return
    if not count:
        print("No redundant notes to trash.")
        return

    confirm_apply(api.group)
    pending, version = validate_note_plan(api, groups, resuming=bool(args.resume))
    if not pending:
        print("All planned notes are already verified in trash.")
        return
    if not args.resume:
        snapshot.update(kind="backup", time=datetime.now(timezone.utc).isoformat())
        backup(snapshot, args.backup)
        print("Private backup:", args.backup, flush=True)
    api.trash_notes(pending, version)
    print("Completed. Snapshot:", args.resume or args.backup)


def read_key_byte(fd):
    byte = os.read(fd, 1)
    if byte == b"\x03":
        raise KeyboardInterrupt
    if byte in (b"\x04", b""):
        raise EOFError
    return byte


def read_terminal_escape(fd):
    """Consume one 7-bit terminal escape and return a bracketed-paste marker."""
    first = read_key_byte(fd)
    if first in (b"]", b"P", b"X", b"^", b"_"):
        # OSC may end with BEL. All control strings can end with ST (ESC \\).
        previous = b""
        while True:
            byte = read_key_byte(fd)
            if (first == b"]" and byte == b"\x07") or (previous == b"\x1b" and byte == b"\\"):
                return None
            if byte in (b"\r", b"\n"):
                break
            previous = byte
    elif first in (b"[", b"O"):
        # CSI and SS3 can include parameters before the final byte.
        sequence = bytearray()
        for _ in range(64):
            byte = read_key_byte(fd)
            if b"\x20" <= byte <= b"\x3f":
                sequence.extend(byte)
                continue
            if b"\x40" <= byte <= b"\x7e":
                if first == b"[" and byte == b"~" and sequence in (b"200", b"201"):
                    return sequence == b"200"
                if first == b"[" and not sequence and byte in (b"M", b"["):
                    # Legacy mouse and function-key reports have extra bytes.
                    break
                return None
            break
    else:
        while b"\x20" <= first <= b"\x2f":
            first = read_key_byte(fd)
        if b"\x30" <= first <= b"\x7e":
            return None
    raise ValueError("Unsupported terminal escape in Zotero API key input")


def hidden_key(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise OSError("A terminal with hidden input or ZOTERO_API_KEY is required") from None


def masked_key(prompt="Zotero API key: "):
    """Mask API-key input, accepting normal and bracketed terminal paste."""
    if os.name != "posix":
        return hidden_key(prompt)
    import termios
    import tty
    try:
        terminal = open("/dev/tty", "w", encoding="utf-8", buffering=1,
                        opener=lambda path, flags: os.open(path, os.O_RDWR | os.O_NOCTTY))
    except OSError:
        return hidden_key(prompt)
    with terminal:
        fd = terminal.fileno()
        original = termios.tcgetattr(fd)
        chars = []
        pasting = False
        try:
            tty.setraw(fd)
            terminal.write("\x1b[?2004h" + prompt)
            terminal.flush()
            while True:
                byte = read_key_byte(fd)
                if byte == b"\x1b":
                    marker = read_terminal_escape(fd)
                    if marker is not None:
                        pasting = marker
                    continue
                if byte in (b"\r", b"\n"):
                    if not pasting:
                        return "".join(chars)
                elif byte in (b"\x08", b"\x7f"):
                    if chars:
                        chars.pop()
                        terminal.write("\b \b")
                elif byte == b"\x15":
                    terminal.write("\b \b" * len(chars))
                    chars.clear()
                elif byte.isalnum():
                    chars.append(byte.decode("ascii"))
                    terminal.write("●")
                elif byte not in (b" ", b"\t"):
                    raise ValueError("Unexpected character in Zotero API key")
                terminal.flush()
        finally:
            try:
                terminal.write("\x1b[?2004l")
                terminal.flush()
            finally:
                termios.tcsetattr(fd, termios.TCSAFLUSH, original)
            terminal.write("\n")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True, type=int)
    parser.add_argument("--timeout", type=float, default=30, help="Request timeout in seconds (default: 30)")
    parser.add_argument("--max-retries", type=int, default=4, help="Additional read attempts (default: 4)")
    parser.add_argument("--apply", action="store_true", help="Enable confirmed writes")
    parser.add_argument("--backup", type=Path, help="Private JSON audit snapshot path")
    sub = parser.add_subparsers(dest="command", required=True)
    merge = sub.add_parser("merge", help="Consolidate selected records, not a native merge")
    merge.add_argument("keys", nargs="+", help="Master key followed by duplicate keys")
    merge.add_argument("--trash", action="store_true", help="Trash redundant parents after checks")
    note = sub.add_parser("note", help="Update an existing note using HTML from a local file")
    note.add_argument("key")
    note.add_argument("file", type=Path)
    dedup = sub.add_parser("deduplicate-notes", help="Preview or trash exact duplicate child notes")
    dedup.add_argument("parents", nargs="*", help="Restrict discovery to these parent reference keys")
    source = dedup.add_mutually_exclusive_group()
    source.add_argument("--save-plan", type=Path, help="Write the reviewed dry-run plan to a new private file")
    source.add_argument("--from-plan", type=Path, help="Use a saved dry-run plan")
    source.add_argument("--resume", type=Path, help="Reconcile or resume from an existing private backup")
    dedup.add_argument("--expect", type=int, nargs=2, metavar=("GROUPS", "REDUNDANT"),
                       help="Require these duplicate-group and redundant-note counts")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.max_retries < 0:
        parser.error("--timeout must be positive and finite and --max-retries must be nonnegative")
    if args.command == "deduplicate-notes":
        keys = args.parents
        if keys and (args.from_plan or args.resume):
            parser.error("Parent keys are only accepted when discovering duplicates")
        if args.apply and not (args.resume or (args.from_plan and args.backup)):
            parser.error("Applying note cleanup requires --from-plan and --backup, or --resume")
        if args.backup and (args.resume or not args.apply):
            parser.error("--backup is only accepted for a new apply")
        if args.expect and any(count < 0 for count in args.expect):
            parser.error("--expect counts must be nonnegative")
    else:
        keys = args.keys if args.command == "merge" else [args.key]
    if args.group <= 0 or any(not KEY.fullmatch(k) for k in keys):
        parser.error("Invalid group ID or item key")
    if args.command == "merge" and (len(keys) < 2 or len(set(keys)) != len(keys)):
        parser.error("Supply at least two distinct item keys")
    token = os.environ.get("ZOTERO_API_KEY") or masked_key()
    api = Zotero(args.group, token, timeout=args.timeout, max_retries=args.max_retries)
    if args.command == "deduplicate-notes":
        deduplicate_notes(api, args)
        return
    if args.command == "merge":
        plan = plan_merge(api, keys)
        print(json.dumps({"master": keys[0], "duplicates": keys[1:],
                          "children": {k: len(v) for k, v in plan["children"].items()},
                          "changes": plan["changes"], "conflicts": plan["conflicts"],
                          "trash": args.trash}, indent=2, ensure_ascii=False))
    else:
        old = api.item(args.key)
        if old["itemType"] != "note" or old.get("deleted"):
            raise ValueError("Expected an active note")
        new = args.file.read_text(encoding="utf-8")
        if not new.strip():
            raise ValueError("Refusing empty replacement")
        plan = {"old": old, "replacement": new}
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    if not args.apply:
        print("Dry run: no changes made.")
        return
    if not args.backup:
        parser.error("--apply requires --backup")
    confirm_apply(args.group)
    backup({"group": args.group, "time": datetime.now(timezone.utc).isoformat(),
            "plan": plan}, args.backup)
    if args.command == "merge":
        apply_merge(api, plan, args.trash)
    else:
        api.patch(plan["old"], {"note": plan["replacement"]})
        print("Updated note", args.key)
    print("Completed. Snapshot:", args.backup)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        sys.exit(1)
    except (OSError, IncompleteRead, ValueError, RuntimeError) as exc:
        print(f"Stopped: {exc}. If applying, inspect the backup and server state before retrying.",
              file=sys.stderr)
        sys.exit(1)
