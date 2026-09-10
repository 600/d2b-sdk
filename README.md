# D2B SDKs

Client libraries and the `d2b` CLI for [D2B](https://d2b.dev) — Spreadsheets
for AI Agents: typed, versioned, governed tables behind an agent-native API.

| | Install | Docs |
|---|---|---|
| Python + CLI | `pip install d2b-sdk` | [docs.d2b.dev](https://docs.d2b.dev/en/sdks) |
| TypeScript | `npm install d2b-sdk` | [docs.d2b.dev](https://docs.d2b.dev/en/sdks) |
| MCP server | no install — a hosted remote server | [docs.d2b.dev](https://docs.d2b.dev/en/mcp) |

The distribution is `d2b-sdk` on both registries; the Python import package and
the command are both `d2b` (`d2b` on PyPI is an unrelated project). With `uvx`,
that means `uvx --from d2b-sdk d2b ...`.

- [`python/`](python) — the Python SDK and the `d2b` CLI
- [`typescript/`](typescript) — the TypeScript SDK
- [`mcp-registry/`](mcp-registry) — the MCP Registry manifest for the hosted server
- [`openapi.json`](openapi.json) — the OpenAPI 3.1 description the SDKs are built against

## This repository is generated

The SDKs are authored in D2B's main repository and synced here as a snapshot on
each release, so **pull requests against these files cannot be merged here**.
Issues and discussion are very welcome — please open an issue and we will carry
the change upstream. Packages are published from this repository, so the
provenance on PyPI and npm points at code you can read.

## License

MIT — see [python/LICENSE](python/LICENSE) and
[typescript/LICENSE](typescript/LICENSE). "D2B", its name and logo, are
trademarks and are not covered by the license.
