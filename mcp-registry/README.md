# MCP Registry manifest

`server.json` is D2B's entry for the official MCP Registry
(https://registry.modelcontextprotocol.io). Once published, registry-aware
hosts install the server by name (`dev.d2b/d2b`) without any per-host
config from our side — the scalable end of the `d2b init` story
(docs/public/ja/agents.md).

## Publishing (owner action — needs DNS access for d2b.dev)

The `dev.d2b/*` namespace is claimed by DNS verification, so this is not
something a script can do for you.

```bash
brew install mcp-publisher            # or the release binary
cd sdks/mcp-registry
mcp-publisher login dns --domain d2b.dev   # prints the TXT record to add under d2b.dev
mcp-publisher publish                       # validates server.json against $schema, then publishes
```

Bump `version` on every publish (the registry refuses a re-used version).
Keep `remotes[0].url` equal to the public endpoint documented in
docs/public/ja/mcp.mdx.

## What is deliberately NOT here

- No packages (`npm` / `pypi`): D2B is a remote server; the Python SDK's
  CLI is a client, not the server.
- No token: the `Authorization` header is a secret variable the host asks
  the user for at install time.
