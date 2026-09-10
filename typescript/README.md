# d2b-sdk — D2B TypeScript SDK

Spreadsheets for AI Agents: typed, versioned, governed tables behind an
agent-native API.

```bash
npm install d2b-sdk
```

Node 18+, ESM only.

```ts
import { D2BClient } from "d2b-sdk";

const client = new D2BClient({ apiKey: "d2b_pat_...", baseUrl: "https://d2b.dev" });
const wb = (await client.workbooks.create({ title: "monthly" })).id as string;
await client.sources.upload(wb, fileBlob, { filename: "sales.xlsx", wait: true });
const grid = await client.tables.a1(wb, "sales", "A1:D10");  // read by Excel address
```

The client is a thin skin over the REST API — every method maps to one endpoint:

- an `Idempotency-Key` on every mutating call (retry-safe);
- exponential-backoff retry on 429 / 5xx. **409 (optimistic lock) is never
  retried** — re-read and re-apply is the contract;
- problem+json errors raised with their `suggested_fix` attached (written for
  LLMs to read and react to).

Docs: [docs.d2b.dev](https://docs.d2b.dev).

## License

MIT ([LICENSE](LICENSE)). "D2B", its name and logo, are trademarks and are not
covered by this license.
