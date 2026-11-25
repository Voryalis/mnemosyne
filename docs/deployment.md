# Deploying for ChatGPT

Run MNEMOSYNE under a dedicated account on the same host or network namespace as the gateway. Give the process a Zotero key with read access limited to the libraries your app users can see. Use HTTPS on the gateway with a certificate trusted by ChatGPT. Keep the JVM listener on its built-in loopback address.

## Gateway setup

Deploy an MCP-compatible OAuth 2.1 gateway and authorization server using the provider's documentation. The two services can be integrated in one gateway.

1. Route the public resource `https://your-host.example/mcp` to the localhost endpoint. Publish OAuth Protected Resource Metadata, and advertise it in the public endpoint's `WWW-Authenticate` challenge for missing or invalid tokens. Set the resource URI to the public MCP URL and publish the issuer's discovery metadata.
2. Configure the authorization server for Authorization Code with PKCE and a ChatGPT-compatible client registration method. Register the exact ChatGPT redirect URI shown by OpenAI or your workspace. Scope access to the approved users or groups, and validate issuer, audience/resource, expiry, and scope before proxying each MCP request.
3. Read the upstream service token from the gateway's private secret configuration. On authenticated traffic, replace `Authorization` with `Bearer <upstream-service-token>` and set upstream `Host` to `127.0.0.1:8080` (or your configured port). Apply the public origin policy at the gateway and omit `Origin` on the local hop. Keep Zotero credentials exclusively in the MNEMOSYNE environment.
4. Forward POST, GET and DELETE along with `Accept`, `Mcp-Session-Id`, `MCP-Protocol-Version`, and `Last-Event-ID` when present. Forward MCP session response headers. Disable proxy buffering for event streams and configure timeouts to allow streaming and document extraction. Use one instance or session affinity.
5. Verify that anonymous public requests receive a 401 and OAuth discovery challenge. Test with an authorized account, confirm users outside the policy cannot connect, and confirm that network clients cannot connect directly to the JVM listener. Avoid recording bearer headers, response bodies, signed storage URLs, or document contents in reverse-proxy logs.

Use ChatGPT's custom-app setup after confirming gateway behavior. The OAuth access token is distinct from the MNEMOSYNE service token and from the Zotero API key. OpenAI's guide describes supported discovery and registration options and the redirect URIs. [OpenAI authentication guide](https://developers.openai.com/apps-sdk/build/auth/) · [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

## Local verification

The following request initializes a local MCP session with a bearer token already available in the shell environment. Run it from a trusted terminal.

```sh
curl -i http://127.0.0.1:8080/mcp \
  -H "Authorization: Bearer ${MNEMOSYNE_HTTP_TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"local-check","version":"1"}}}'
```

An MCP client handles session headers and initialization notifications for subsequent calls. To rotate the service token, update both local service and gateway secret configuration, then restart the MNEMOSYNE process. Rotate the Zotero key independently.
