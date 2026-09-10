# d2b-sdk — D2B Python SDK

Spreadsheets for AI Agents: typed, versioned, governed tables behind an
agent-native API.

```bash
pip install d2b-sdk
```

The distribution is `d2b-sdk`; the import package and the command are both
`d2b` (`d2b` on PyPI is an unrelated project). With `uvx`, that means
`uvx --from d2b-sdk d2b ...`.

```python
from d2b import D2BClient, ConflictError

client = D2BClient(api_key="d2b_pat_...", base_url="https://d2b.dev")
wb = client.workbooks.create(title="monthly")["id"]

# Large files are safe: the SDK drives the async ingest and waits on the job.
client.sources.upload(wb, "sales.xlsx", wait=True)

for t in client.tables.list(wb):
    print(t["name"], t["row_count"])

# Row edits are optimistically locked — a 409 is ConflictError, and the
# contract is to re-read and re-apply rather than retry.
page = client.tables.rows(wb, "sales")
try:
    client.tables.upsert_rows(
        wb, "sales",
        rows=[{"product": "apple", "qty": 3}],
        expected_version=page["edit_version"],
    )
except ConflictError:
    page = client.tables.rows(wb, "sales")
```

The client is a thin skin over the REST API — every method maps to one
endpoint:

- an `Idempotency-Key` on every mutating call (retry-safe);
- exponential-backoff retry on 429 / 5xx. **409 is never retried**;
- problem+json errors raised with their `suggested_fix` attached (written for
  LLMs to read and react to);
- helpers for the parts that bite: `jobs.wait()`, `tables.iter_rows()`,
  `webhooks.verify_signature()`.

Docs: [docs.d2b.dev](https://docs.d2b.dev) ·
[API reference](https://docs.d2b.dev/en/api-reference) ·
[日本語](https://docs.d2b.dev/ja)

## CLI

```bash
d2b login                                            # browser login; no raw API keys
d2b workbooks create --title monthly
d2b upload sales.xlsx --workbook WB --wait
d2b query 'SELECT 1' --workbook WB
d2b pull --workbook WB                               # transforms/ sheets/ charts/ + d2b.json
d2b pull --data customers                            # track base tables as data/*.csv too
d2b push --commit "$(git rev-parse --short HEAD)"    # re-run what changed, then name the version after the commit
d2b github-workflow > .github/workflows/d2b.yml      # CI: push on merge to main, scheduled pull → PR
```

Output is JSON on stdout; errors carry their `suggested_fix` on stderr; exit
codes are 0 / 1 (API errors, refused syncs) / 2 (usage). Nothing prompts
interactively.

`d2b login` targets `https://d2b.dev` unless `--base-url` or `$D2B_BASE_URL`
says otherwise. For non-interactive use set `D2B_API_KEY` and `D2B_BASE_URL`
— **never pass secrets as command-line arguments** (`--api-key` is not
accepted, and secret-shaped strings are redacted from error messages).

## License

MIT ([LICENSE](LICENSE)). "D2B", its name and logo, are trademarks and are not
covered by this license.
