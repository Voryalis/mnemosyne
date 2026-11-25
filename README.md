<div align="center">

# MNEMOSYNE

*Μνημοσύνη · Memory*

**A Zotero research library for your AI assistant**

[![Java 21](https://img.shields.io/badge/Java-21-263550)](https://adoptium.net/temurin/releases/?version=21)
[![Maven verification](https://github.com/Voryalis/mnemosyne/actions/workflows/maven.yml/badge.svg)](https://github.com/Voryalis/mnemosyne/actions/workflows/maven.yml)
[![MIT](https://img.shields.io/badge/License-MIT-82633c)](LICENSE)

[Installation](#installation) · [Research tools](#research-tools) · [ChatGPT](#chatgpt) · [Architecture](docs/architecture.md)

</div>

In Greek mythology, Mnemosyne personifies memory. This project brings a researcher's accumulated reading into an AI session. Search Zotero for a reference, open an attached paper or note, then cite the original source using its bibliographic record and a page or cell locator.

MNEMOSYNE is a read-only Java MCP server for personal and group Zotero libraries, including private groups authorized by your Zotero API key. It uses the Zotero Web API v3 and supports local stdio and authenticated Streamable HTTP.

## Installation

Install a **Java 21 JDK**, **Maven 3.9+**, and Git. Build from source.

```sh
git clone https://github.com/Voryalis/mnemosyne.git
cd mnemosyne
mvn clean verify
```

The executable is `target/mnemosyne.jar`.

Create a [Zotero API key](https://www.zotero.org/settings/keys) with read access to the libraries you intend to expose. Enable group-library access for the selected private groups and leave write permissions disabled. Use the numeric user ID shown on the Zotero key settings page and the numeric IDs from group library URLs. Libraries must be synced to Zotero's servers.

### Linux and Fish

In Bash, prompt for the key in a private terminal and substitute your library IDs.

```bash
read -rsp 'Zotero API key: ' ZOTERO_API_KEY
printf '\n'
export ZOTERO_API_KEY
export ZOTERO_USER_ID='<numeric-user-id>'
# Optional: export ZOTERO_GROUP_IDS='<group-id>,<group-id>'
java -jar target/mnemosyne.jar stdio
```

In Fish, use a session-scoped exported variable and a silent prompt.

```fish
read --silent --export --prompt-str 'Zotero API key: ' ZOTERO_API_KEY
set -gx ZOTERO_USER_ID '<numeric-user-id>'
# Optional: set -gx ZOTERO_GROUP_IDS '<group-id>,<group-id>'
java -jar target/mnemosyne.jar stdio
```

A running stdio server waits for MCP messages. In an MCP client, configure the command to launch it with an absolute JAR path. Launch the client with the exported environment, or use the client's secret or environment configuration.

```json
{
  "mcpServers": {
    "mnemosyne": {
      "command": "java",
      "args": ["-jar", "/absolute/path/to/mnemosyne.jar", "stdio"]
    }
  }
}
```

| Environment variable | Purpose |
| --- | --- |
| `ZOTERO_API_KEY` | Read-only Zotero key. May be omitted for resources available without credentials. |
| `ZOTERO_USER_ID` | Numeric ID of the personal library. |
| `ZOTERO_GROUP_IDS` | Comma-separated numeric group IDs. Configure a user ID, group IDs, or both. |
| `MNEMOSYNE_PROXY` | Empty for direct connections. Use `http://proxy.example:3128` for HTTP CONNECT, or `socks5h://127.0.0.1:1080` for SOCKS5 with proxy-side destination DNS. `socks5://` is an alias with the same DNS behavior. |
| `MNEMOSYNE_PORT` | Local Streamable HTTP port. Defaults to `8080`. |
| `MNEMOSYNE_HTTP_TOKEN` | Required for HTTP. Use a separate random bearer token of 32–256 URL-safe characters. |

For a Linux service, use a dedicated unprivileged account and supply the environment through a secret manager or a service-owned environment file outside the checkout with mode `0600`. Use the launcher or service account's private environment for local clients. Keep private attachments, API keys, and authentication headers out of Git, trace logs, and tool arguments.

## Research tools

| Tool | What it returns |
| --- | --- |
| `list_libraries` | Configured personal and group library aliases |
| `search`, `fetch` | Title, creator and year search with stable result IDs, followed by citation-ready Zotero metadata |
| `list_references`, `list_collections`, `get_reference` | Reference pages, collection hierarchy keys, and complete Zotero item records |
| `list_attachments`, `read_attachment` | Attachment descriptors and extracted PDF, DOCX, or XLSX chunks |
| `list_notes`, `read_note` | Standalone and child notes with plain text and source metadata |
| `search_full_text`, `read_indexed_text` | Zotero's everything search and synced indexed text with coverage fields |
| `detect_duplicates` | Candidate groups by DOI, URL, or normalized title within the scan window |
| `export_bibtex` | Zotero-generated BibTeX for an item or a page of references |
| `get_source` | Zotero item links and the parent reference's complete bibliographic fields |

Library aliases are `personal` and `group:<id>`. Calls default to the personal library if configured, or to the only configured group. Supply `library` explicitly when working across groups. For list tools, pass the returned `nextStart` as `start`. A null cursor ends the list. The default page size is 20, with a maximum of 100 (50 for BibTeX export). Duplicate detection defaults to a scan of 200 references and accepts up to 500 at a time.

Text tools accept an `offset` and `chars` (default 6,000, maximum 16,000). Continue with `nextOffset`. PDF offsets are relative to the 1-based physical `page`; use `nextPage` with an offset of zero when a page ends. For printed citations, verify the paper's printed pagination against its physical PDF pages. XLSX output identifies sheets and cell coordinates.

An example prompt after connecting the server:

> Search Zotero for “collective memory.” For the most relevant paper, retrieve the DOI and authors, then read the first two PDF pages. Cite the paper using its Zotero metadata and specify the physical PDF page for each quotation.

## ChatGPT

A local MCP client can connect directly. **ChatGPT needs a reachable HTTPS MCP URL and OAuth for a private library.** Deploy the server behind an authenticated OAuth-capable MCP gateway before connecting it. The local bearer token is an upstream service credential kept by the gateway.

1. Generate the upstream token with `openssl rand -hex 32`, put it in the service environment as `MNEMOSYNE_HTTP_TOKEN`, and run `java -jar target/mnemosyne.jar http`. The service listens at `http://127.0.0.1:8080/mcp` and rejects requests without the bearer token.
2. Follow the [deployment guide](docs/deployment.md) to expose `https://your-host.example/mcp` with TLS, OAuth discovery, and authorization for the intended ChatGPT users. Start with a public or test library when validating the gateway.
3. In an eligible ChatGPT workspace, enable Developer Mode, create a custom app with the HTTPS MCP URL, select OAuth, and complete authorization. A workspace administrator may need to enable custom apps. Test `list_libraries`, `search`, and `fetch` before opening private content. [OpenAI connection guide](https://developers.openai.com/apps-sdk/deploy/connect-chatgpt/)
4. Use the app in a conversation. Selected metadata, notes, and document chunks will be sent to ChatGPT as tool results. Give each deployment only the Zotero permissions its authorized users should share.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Zotero returns 401 or 403 | Check the Zotero key, numeric library ID, private group membership, and key permissions for that group. |
| Zotero returns 404 for a file or full text | Verify that the attachment was synced to Zotero and that an index exists. Linked local files and personal WebDAV storage are outside the Web API file flow. |
| Search misses text visible in Zotero | Check index synchronization and try a shorter search phrase. Zotero's `everything` mode also matches metadata. |
| HTTP endpoint returns 401, 403, or 421 | Confirm the service bearer token, the loopback upstream Host, and absence of browser Origin headers on the gateway-to-server request. |
| ChatGPT cannot discover or authenticate the app | Confirm publicly reachable HTTPS, the OAuth discovery challenge, token audience, redirect registration and workspace settings. |
| Proxy or TLS fails | Check the proxy scheme and endpoint. Install the applicable root CA in a trusted Java trust store and retain hostname and certificate checks. |
| Empty PDF page | The page may contain only images. Use an OCRed copy in Zotero, or query the synced index if present. |
| Zotero asks you to back off | Retry later. The process honors Zotero's `Backoff` and `Retry-After` headers. |

## Scope and limits

- Zotero reads only. There is no write, upload, delete, merge, or persistent search index.
- Document extraction supports text-bearing PDF pages, DOCX body paragraphs and tables, and displayed XLSX cell values. OCR, encrypted Office or PDF files, embedded objects, images, Word headers and footnotes, and other attachment formats are outside the current extraction scope.
- Each attachment read downloads and parses the file again. Attachment downloads are capped at 25 MiB. Text offsets use UTF-16 units and stop at a 5 million unit offset, with a `truncated` flag if continuation is unavailable.
- Zotero controls what is synchronized and indexed. Full-text matches can include metadata and may not have a citation page. Duplicate matches are candidates and do not cross scan windows or libraries.
- One process serves one configured Zotero permission scope. Deploy separate instances where different users need different upstream library access.
- Proxy endpoint authentication and native OAuth are outside the server's scope. ChatGPT plans and workspace policies determine custom app access.

## Development

Run `mvn clean verify` on Java 21. Tests use mock Zotero responses and generate document fixtures in memory. See [architecture and dependency references](docs/architecture.md). Contributions are covered by the existing [MIT license](LICENSE).
