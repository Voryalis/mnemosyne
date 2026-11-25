#!/usr/bin/env python3
"""Prepare and archive explicitly reviewed AI-use evidence. Python 3.10+."""
import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__:
    from . import zotero_maintenance as maintenance
else:
    import zotero_maintenance as maintenance

GROUP = 6757728
ROOT = "3ALQQNFN"
COLLECTIONS = {
    "chatgpt": "ChatGPT Interaction Records",
    "codex": "Codex Interaction Records",
    "maintenance": "Maintenance Evidence",
    "compliance": "Compliance Review",
}
MAX_BYTES = 10 * 1024 * 1024
MAX_SOURCE_BYTES = 128 * 1024 * 1024
SENSITIVE = [
    re.compile(r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"(?im)\b(?:authorization|cookie|set-cookie|x-api-key|zotero-api-key)\s*:\s*[^\n]+"),
    re.compile(r"""(?i)\b(?:api[_ -]?key|token|password|secret|credential)["']?\s*[:=]\s*["']?[^\s"',;}]+"""),
    re.compile(r"""(?im)\b[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|REFRESH_TOKEN|PASSWORD|SECRET_KEY)["']?\s*[:=]\s*[^\n]+"""),
    re.compile(r"(?i)\b(?:bearer\s+[\w.+/=-]+|(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-)[\w-]+)"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\beyJ[\w-]+\.[\w-]+\.[\w-]+\b"),
    re.compile(r"(?i)\bhttps?://[^\s<>]+[?&](?:token|signature|key|x-amz-[^=]+)=[^\s<>]+"),
    re.compile(r"(?i)[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:/home/|/Users/)[^/\s]+"),
    re.compile(r"(?i)(?:Zotero API key:|API key:)[^\n]*"),
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def redact(text):
    for pattern in SENSITIVE:
        text = pattern.sub("[REDACTED]", text)
    return text


def read_limited(path, limit=MAX_BYTES):
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"File exceeds the {limit // (1024 * 1024)} MiB limit")
    return data


def codex_events(raw):
    for line in raw.decode("utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def codex_transcript(raw):
    metadata, models, messages, fallback = {}, set(), [], []
    for event in codex_events(raw):
        kind, payload = event.get("type"), event.get("payload", {})
        timestamp = event.get("timestamp")
        if kind == "session_meta":
            metadata = payload
        if kind in ("session_meta", "turn_context"):
            if payload.get("model"):
                models.add(payload["model"])
        if kind == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                content = [part["text"] for part in payload.get("content", [])
                           if part.get("type") in ("input_text", "output_text", "text")
                           and isinstance(part.get("text"), str)]
                if content:
                    messages.append((timestamp, role, "\n".join(content)))
        if kind == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
            if isinstance(payload.get("message"), str):
                role = "user" if payload["type"] == "user_message" else "assistant"
                fallback.append((timestamp, role, payload["message"]))
    if not (messages or fallback):
        raise ValueError("No user or assistant text found in the Codex transcript")
    return render_transcript("Codex", metadata.get("timestamp"), sorted(models),
                             metadata.get("cli_version"), messages or fallback)


def chatgpt_transcript(raw, conversation_id):
    exported = json.loads(raw)
    conversations = exported if isinstance(exported, list) else [exported]
    selected = [c for c in conversations if isinstance(c, dict)
                and (c.get("id") or c.get("conversation_id")) == conversation_id]
    if len(selected) != 1:
        raise ValueError("Select exactly one ChatGPT conversation by its exported ID")
    conversation = selected[0]
    mapping = conversation["mapping"]
    key = conversation["current_node"]
    seen, models, messages = set(), set(), []
    while key is not None:
        if key in seen:
            raise ValueError("Conversation export has a circular message chain")
        seen.add(key)
        node = mapping[key]
        key = node.get("parent")
        message = node.get("message") or {}
        role = (message.get("author") or {}).get("role")
        metadata = message.get("metadata") or {}
        if (role not in ("user", "assistant") or metadata.get("is_visually_hidden_from_conversation")
                or message.get("recipient") not in (None, "all")):
            continue
        for field in ("model_slug",):
            if metadata.get(field):
                models.add(metadata[field])
        parts = (message.get("content") or {}).get("parts", [])
        strings = [part for part in parts if isinstance(part, str)]
        if strings:
            messages.append((utc_time(message.get("create_time")), role, "\n".join(strings)))
    if not messages:
        raise ValueError("No visible user or assistant text found in the selected conversation")
    return render_transcript("ChatGPT", utc_time(conversation.get("create_time")), sorted(models),
                             None, reversed(messages))


def utc_time(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def render_transcript(source, date, models, cli, messages):
    chunks = [f"{source} interaction record", f"Session date: {date or 'Unavailable'}",
              f"Recorded model identifiers: {', '.join(models) or 'Unavailable'}",
              f"Recorded CLI version: {cli or 'Unavailable'}"]
    for timestamp, role, body in messages:
        chunks.extend(["", f"[{timestamp or 'Timestamp unavailable'}] {role}", body])
    return "\n".join(chunks) + "\n"


def private_file(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def prepare(kind, source, directory, conversation_id):
    raw = read_limited(source, MAX_SOURCE_BYTES)
    if kind == "codex":
        rendered = codex_transcript(raw)
    elif kind == "chatgpt":
        rendered = chatgpt_transcript(raw, conversation_id)
    else:
        rendered = (json.dumps(json.loads(raw), ensure_ascii=False, indent=2) + "\n"
                    if source.suffix.lower() == ".json" else raw.decode("utf-8"))
    directory.mkdir(mode=0o700)
    private_file(directory / "original.private", raw)
    private_file(directory / "review.txt", redact(rendered).encode("utf-8"))
    print("Private original and review copy prepared. Review review.txt before adding it to a manifest.")


def discover(roots, terms):
    terms = [term.casefold() for term in terms]
    paths = {path for root in roots for path in root.rglob("rollout-*.jsonl")}
    unreadable = 0
    for path in sorted(paths):
        try:
            metadata, matched = {}, False
            for event in codex_events(read_limited(path, MAX_SOURCE_BYTES)):
                payload = event.get("payload", {})
                if event.get("type") == "session_meta":
                    metadata = payload
                if ((payload.get("type") == "message" and payload.get("role") == "user")
                        or payload.get("type") == "user_message"):
                    prompt = json.dumps(payload, ensure_ascii=False).casefold()
                    matched |= any(term in prompt for term in terms)
            if matched:
                print(f"{metadata.get('timestamp', 'Date unavailable')}  {path}")
        except (OSError, ValueError, TypeError, AttributeError):
            unreadable += 1
    print(f"Scanned {len(paths)} Codex session files; {unreadable} unreadable or oversized.")


@dataclass(frozen=True)
class Evidence:
    category: str
    title: str
    activity: str
    digest: str
    content: bytes

    @property
    def filename(self):
        return f"ai-evidence-{self.digest[:16]}.txt"


def load_plan(manifest):
    entries = json.loads(read_limited(manifest))
    if not isinstance(entries, list) or not entries:
        raise ValueError("Manifest must be a nonempty JSON array")
    result, seen = [], set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"category", "title", "activity", "file", "sha256"}
                or not all(isinstance(value, str) and value.strip() for value in entry.values())
                or entry["category"] not in COLLECTIONS):
            raise ValueError("Invalid evidence manifest entry")
        path = manifest.parent / entry["file"]
        if path.suffix != ".txt" or path.is_symlink() or not path.is_file():
            raise ValueError("Evidence must be a reviewed .txt file")
        data = read_limited(path)
        digest = sha(data)
        if not data.strip() or digest != entry["sha256"]:
            raise ValueError("Evidence has changed since review or is empty")
        if digest in seen:
            raise ValueError("Duplicate evidence bytes in manifest")
        seen.add(digest)
        for value in (data.decode("utf-8"), entry["title"], entry["activity"]):
            if redact(value) != value or any(ord(char) < 32 and char not in "\n\r\t" for char in value):
                raise ValueError("Evidence or metadata contains a possible secret, identifier or control character")
        result.append(Evidence(entry["category"], entry["title"], entry["activity"], digest, data))
    return sorted(result, key=lambda item: item.digest)


def preview(evidence):
    entries = [{"collection": COLLECTIONS[item.category], "title": item.title,
                "activity": item.activity, "filename": item.filename,
                "sha256": item.digest, "bytes": len(item.content)}
               for item in evidence]
    plan_id = sha(json.dumps([GROUP, ROOT, COLLECTIONS, entries], sort_keys=True).encode())
    return {"group": GROUP, "parent": ROOT, "subcollections": list(COLLECTIONS.values()),
            "evidence": entries, "index": COLLECTIONS["compliance"], "plan_sha256": plan_id}


# Zotero keys are eight characters. Every reuse also verifies full evidence identity.
def zotero_key(namespace):
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    number = int(sha(namespace.encode()), 16)
    return "".join(alphabet[(number >> (index * 5)) & 31] for index in range(8))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def storage_post(url, content_type, data, timeout):
    target = urlsplit(url)
    host = target.hostname or ""
    if (target.scheme != "https" or target.username is not None or target.password is not None
            or target.port not in (None, 443)
            or not (host == "zotero.org" or host.endswith((".zotero.org", ".amazonaws.com")))):
        raise ValueError("Unexpected Zotero upload storage URL")
    request = Request(url, data, {"Content-Type": content_type}, method="POST")
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        if response.status != 201:
            raise RuntimeError("Zotero storage did not acknowledge the upload")


class NoteContent(HTMLParser):
    """Compare verified note text and links across Zotero's HTML normalization."""
    def __init__(self, source):
        super().__init__()
        self.text = []
        self.links = []
        self.feed(source)
        self.close()

    def handle_data(self, data):
        if data.strip():
            self.text.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href"))

    def signature(self):
        return " ".join("".join(self.text).split()), self.links


def matches(current, expected, fields):
    for field in fields:
        actual = current.get(field)
        value = expected[field]
        if field == "note" and isinstance(actual, str):
            if NoteContent(actual).signature() == NoteContent(value).signature():
                continue
        if actual != value:
            return False
    return True


class EvidenceArchive:
    def __init__(self, api):
        self.api = api

    def lookup(self, kind, key):
        try:
            return self.api.request(f"/{kind}/{key}")["data"]
        except HTTPError as error:
            if error.code == 404:
                return None
            raise

    def ensure(self, kind, data, fields):
        key = data["key"]
        current = self.lookup(kind, key)
        if current is None:
            _, error = self.api.write_once(f"/{kind}", "POST", [data | {"version": 0}], None)
            current = self.lookup(kind, key)
            if current is None:
                raise RuntimeError(f"Creation could not be verified for {kind} {key}") from error
        if current.get("deleted") or current.get("parentItem") or not matches(current, data, fields):
            raise RuntimeError(f"Existing {kind} {key} conflicts with the reviewed evidence")
        return current

    def collections(self):
        root = self.lookup("collections", ROOT)
        if not root or root.get("deleted") or root.get("name", "").casefold() != "ai-use logs":
            raise RuntimeError("Expected AI-use logs at the configured collection key")
        existing = {}
        start = 0
        while True:
            page = self.api.request(f"/collections/{ROOT}/collections?limit=100&start={start}")
            for entry in page:
                record = entry["data"]
                name = record["name"]
                if name in COLLECTIONS.values():
                    if name in existing:
                        raise RuntimeError(f"Ambiguous existing subcollection: {name}")
                    existing[name] = record["key"]
            if len(page) < 100:
                break
            start += len(page)
        for category, name in COLLECTIONS.items():
            key = existing.get(name, zotero_key(f"{ROOT}:collection:{category}"))
            self.ensure("collections", {"key": key, "name": name, "parentCollection": ROOT},
                        ("name", "parentCollection"))
            existing[name] = key
        return {category: existing[name] for category, name in COLLECTIONS.items()}

    def file_form(self, key, fields):
        return self.api.request(f"/items/{key}/file", "POST", urlencode(fields).encode(),
                                extra_headers={"Content-Type": "application/x-www-form-urlencoded",
                                               "If-None-Match": "*"})

    def upload(self, key, item, current):
        md5 = hashlib.md5(item.content, usedforsecurity=False).hexdigest()
        if current.get("md5") not in (None, "", md5):
            raise RuntimeError(f"An unrelated file is already stored for attachment {key}")
        if not current.get("md5"):
            try:
                auth = self.file_form(key, {"md5": md5, "filename": item.filename,
                                           "filesize": len(item.content),
                                           "mtime": int(datetime.now(timezone.utc).timestamp() * 1000)})
                if auth.get("exists") != 1:
                    storage_post(auth["url"], auth["contentType"],
                                 auth["prefix"].encode() + item.content + auth["suffix"].encode(),
                                 self.api.timeout)
                    self.file_form(key, {"upload": auth["uploadKey"]})
            except (OSError, IncompleteRead, ValueError, TypeError, KeyError, RuntimeError) as error:
                # Never repeat a write automatically. A lost registration response
                # is safe to accept only after seeing the exact file.
                remote = self.lookup("items", key)
                if not remote or remote.get("md5") != md5:
                    raise RuntimeError(f"Upload was not verified for attachment {key}; review and rerun") from error
        remote = self.lookup("items", key)
        if not remote or remote.get("deleted") or remote.get("md5") != md5:
            raise RuntimeError(f"Uploaded file metadata mismatch for attachment {key}")
        stored = self.api.request(f"/items/{key}/file", raw_response=True,
                                  max_response_bytes=len(item.content))
        if sha(stored) != item.digest:
            raise RuntimeError(f"Uploaded file bytes did not verify for attachment {key}")

    def archive(self, evidence, plan_id):
        collections = self.collections()
        links = []
        for item in evidence:
            key = zotero_key(f"{ROOT}:sha256:{item.digest}")
            data = {"key": key, "itemType": "attachment", "linkMode": "imported_file",
                    "title": item.title, "filename": item.filename, "charset": "utf-8",
                    "contentType": "text/plain", "collections": [collections[item.category]],
                    "note": f"<p>SHA-256: {item.digest}</p>"}
            current = self.ensure("items", data, ("itemType", "linkMode", "title", "filename",
                                                  "collections", "note", "contentType"))
            self.upload(key, item, current)
            print(f"Verified stored evidence {key}.", flush=True)
            url = f"https://www.zotero.org/groups/{GROUP}/items/{key}"
            links.append(f'<li>{html.escape(item.activity)} — <a href="{url}">'
                         f'{html.escape(item.title)}</a> ({html.escape(COLLECTIONS[item.category])})</li>')
        index = {"key": zotero_key(f"{ROOT}:index:{plan_id}"), "itemType": "note",
                 "collections": [collections["compliance"]],
                 "note": "<h1>AI-use evidence index</h1><ul>" + "".join(links)
                         + f"</ul><p>Reviewed plan SHA-256: {plan_id}</p>"}
        self.ensure("items", index, ("itemType", "collections", "note"))
        print(f"Verified index note {index['key']}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("discover", help="List matching local Codex session files")
    scan.add_argument("--root", action="append", type=Path)
    scan.add_argument("--match", action="append", required=True)
    prep = commands.add_parser("prepare", help="Preserve an original and create a redaction draft")
    prep.add_argument("kind", choices=COLLECTIONS)
    prep.add_argument("source", type=Path)
    prep.add_argument("directory", type=Path)
    prep.add_argument("--conversation-id")
    archive = commands.add_parser("archive", help="Preview or import a reviewed manifest")
    archive.add_argument("manifest", type=Path)
    archive.add_argument("--apply", metavar="PLAN_SHA256")
    archive.add_argument("--timeout", type=float, default=30)
    archive.add_argument("--max-retries", type=int, default=4)
    args = parser.parse_args()
    if args.command == "discover":
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        discover(args.root or [codex_home / "sessions", codex_home / "archived_sessions"], args.match)
    elif args.command == "prepare":
        if args.kind == "chatgpt" and not args.conversation_id:
            parser.error("ChatGPT preparation requires --conversation-id")
        prepare(args.kind, args.source, args.directory, args.conversation_id)
    else:
        if not math.isfinite(args.timeout) or args.timeout <= 0 or args.max_retries < 0:
            parser.error("Timeout must be positive and retries nonnegative")
        evidence = load_plan(args.manifest)
        plan = preview(evidence)
        print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
        if not args.apply:
            print("Local dry run. No Zotero requests were made.")
            return
        if args.apply != plan["plan_sha256"]:
            parser.error("Apply requires the exact reviewed plan SHA-256")
        maintenance.confirm_apply(GROUP)
        token = os.environ.get("ZOTERO_API_KEY") or maintenance.masked_key()
        if not token:
            raise ValueError("A Zotero API key is required")
        if any(token.encode() in item.content or token in item.title or token in item.activity
               for item in evidence):
            raise ValueError("Evidence contains the current Zotero API key")
        api = maintenance.Zotero(GROUP, token, timeout=args.timeout, max_retries=args.max_retries)
        EvidenceArchive(api).archive(evidence, args.apply)


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        sys.exit("Cancelled.")
    except HTTPError as error:
        sys.exit(f"Zotero or storage returned HTTP {error.code}. Inspect the archive before retrying.")
    except (OSError, IncompleteRead, ValueError, RuntimeError, KeyError, TypeError) as error:
        sys.exit(f"Stopped: {error}")
