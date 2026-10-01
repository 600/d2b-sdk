# D2B SDKs

> **Read-only mirror.** The SDKs are developed in D2B's main repository and
> published here on each release, so pull requests opened here can't be merged.
> Issues are welcome — open one and we'll carry it upstream.

Client libraries and the `d2b` CLI for [D2B](https://d2b.dev) — Spreadsheets
for AI Agents: typed, versioned, governed tables behind an agent-native API.

| | Install | Registry | Docs |
|---|---|---|---|
| Python + CLI | `pip install d2b-sdk` | [PyPI](https://pypi.org/project/d2b-sdk/) | [docs.d2b.dev/en/sdks](https://docs.d2b.dev/en/sdks) · [CLI](https://docs.d2b.dev/en/cli) |
| TypeScript | `npm install d2b-sdk` | [npm](https://www.npmjs.com/package/d2b-sdk) | [docs.d2b.dev/en/sdks](https://docs.d2b.dev/en/sdks) |
| MCP server | no install — a hosted remote server | — | [docs.d2b.dev/en/mcp](https://docs.d2b.dev/en/mcp) |

The distribution is `d2b-sdk` on both registries; the Python import package and
the command are both `d2b` (`d2b` on PyPI is an unrelated project). With `uvx`,
that means `uvx --from d2b-sdk d2b ...`.

- [`python/`](python) — the Python SDK and the `d2b` CLI
- [`typescript/`](typescript) — the TypeScript SDK
- [`mcp-registry/`](mcp-registry) — the MCP Registry manifest for the hosted server
- [`openapi.json`](openapi.json) — the OpenAPI 3.1 description the SDKs are built against

## Releases

A release is a tag on the main repository — `sdk-py-v<version>` for Python,
`sdk-ts-v<version>` for TypeScript — synced here with the content, from
where [release.yml](.github/workflows/release.yml) publishes to PyPI (trusted
publishing) and npm (with provenance). The [tags](https://github.com/600/d2b-sdk/tags)
list every version ever published.

## License

MIT — see [python/LICENSE](python/LICENSE) and
[typescript/LICENSE](typescript/LICENSE). "D2B", its name and logo, are
trademarks and are not covered by the license.
