#!/usr/bin/env python3
"""Opt-in Zotero Web API maintenance. Dry-run by default. Python 3.10+."""
import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

KEY = re.compile(r"^[A-Z0-9]{8}$")


class Zotero:
    def __init__(self, group, token):
        self.base = f"https://api.zotero.org/groups/{group}"
        self.token = token

    def request(self, path, method="GET", data=None, version=None):
        headers = {"Zotero-API-Key": self.token, "Zotero-API-Version": "3"}
        if version is not None:
            headers["If-Unmodified-Since-Version"] = str(version)
        payload = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(data).encode()
        with urlopen(Request(self.base + path, payload, headers, method=method), timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

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
        self.request(f"/items/{old['key']}", "PATCH", changes, old["version"])
        fresh = self.item(old["key"])
        for field, value in changes.items():
            if fresh.get(field) != value:
                raise RuntimeError(f"Readback mismatch: {old['key']} {field}")
        return fresh


def backup(data, path):
    # Exclusive creation with private permissions; never overwrite an audit snapshot.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())


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



def masked_key(prompt="Zotero API key: "):
    """Mask API-key input, accepting normal and bracketed terminal paste."""
    if os.name != "posix":
        return getpass.getpass(prompt)
    import termios
    import tty
    try:
        terminal = open("/dev/tty", "r+", encoding="utf-8", buffering=1)
    except OSError:
        return getpass.getpass(prompt)
    with terminal:
        fd = terminal.fileno()
        original = termios.tcgetattr(fd)
        chars = []
        escape = None
        terminal.write(prompt + "\x1b[?2004h")
        terminal.flush()
        try:
            tty.setcbreak(fd)
            while True:
                byte = os.read(fd, 1)
                if escape is not None:
                    escape += byte
                    if escape in (b"\x1b[200~", b"\x1b[201~"):
                        escape = None
                    elif not any(seq.startswith(escape) for seq in
                                 (b"\x1b[200~", b"\x1b[201~")):
                        escape = None
                    continue
                if byte == b"\x1b":
                    escape = byte
                    continue
                if byte in (b"\r", b"\n"):
                    return "".join(chars)
                if byte in (b"\x03", b"\x04", b""):
                    raise KeyboardInterrupt
                if byte in (b"\x08", b"\x7f"):
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
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
            terminal.write("\x1b[?2004l\n")
            terminal.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", required=True, type=int)
    parser.add_argument("--apply", action="store_true", help="Enable confirmed writes")
    parser.add_argument("--backup", type=Path, help="Private JSON audit snapshot path")
    sub = parser.add_subparsers(dest="command", required=True)
    merge = sub.add_parser("merge", help="Consolidate selected records, not a native merge")
    merge.add_argument("keys", nargs="+", help="Master key followed by duplicate keys")
    merge.add_argument("--trash", action="store_true", help="Trash redundant parents after checks")
    note = sub.add_parser("note", help="Update an existing note using HTML from a local file")
    note.add_argument("key")
    note.add_argument("file", type=Path)
    args = parser.parse_args()
    keys = args.keys if args.command == "merge" else [args.key]
    if args.group <= 0 or any(not KEY.fullmatch(k) for k in keys):
        parser.error("Invalid group ID or item key")
    if args.command == "merge" and (len(keys) < 2 or len(set(keys)) != len(keys)):
        parser.error("Supply at least two distinct item keys")
    token = os.environ.get("ZOTERO_API_KEY") or masked_key()
    api = Zotero(args.group, token)
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
    if input(f"Type APPLY {args.group} to confirm: ") != f"APPLY {args.group}":
        raise RuntimeError("Confirmation cancelled")
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
    except (HTTPError, OSError, ValueError, RuntimeError) as exc:
        print(f"Stopped: {exc}. If applying, inspect the backup and server state before retrying.",
              file=sys.stderr)
        sys.exit(1)
