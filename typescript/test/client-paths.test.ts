import { describe, expect, it } from "vitest";

import { D2BClient } from "../src/index.js";

/**
 * Regression test for the SDK path-injection fix: every dynamic path segment
 * must be percent-encoded before request construction, so that `/` and
 * dot-segments like `..` cannot change the normalized request target
 * (route-confusion). Mirrors sdks/python/tests/test_client_paths.py.
 */
describe("SDK encodes dynamic path segments before request", () => {
  function clientCapturing(seen: string[]): D2BClient {
    const fetchImpl = (async (input: URL | RequestInfo) => {
      const url = input instanceof URL ? input : new URL(String(input));
      seen.push(url.pathname + url.search);
      return new Response("{}", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;
    return new D2BClient({ apiKey: "d2b_pat_test", baseUrl: "https://api.example.test", fetch: fetchImpl });
  }

  it("encodes slashes and dot-segments in high-value identifiers", async () => {
    const seen: string[] = [];
    const client = clientCapturing(seen);

    await client.workbooks.get("../me/data");
    await client.tables.rows("victim/tables/payroll/rows", "finance/2026", { limit: 100, offset: 0 });
    await client.jobs.get("job123/../../me/webhooks");
    await client.webhooks.delete("../workbooks/victim");

    expect(seen).toEqual([
      "/api/v1/workbooks/..%2Fme%2Fdata",
      "/api/v1/workbooks/victim%2Ftables%2Fpayroll%2Frows/tables/finance%2F2026/rows?limit=100&offset=0",
      "/api/v1/jobs/job123%2F..%2F..%2Fme%2Fwebhooks",
      "/api/v1/me/webhooks/..%2Fworkbooks%2Fvictim",
    ]);
  });

  it("leaves ordinary identifiers untouched", async () => {
    const seen: string[] = [];
    const client = clientCapturing(seen);

    await client.workbooks.get("wb_123");
    await client.sheets.render("wb_123", "summary");
    await client.transforms.list("wb_123");
    await client.charts.list("wb_123");
    await client.sheets.get("wb_123", "summary");

    expect(seen).toEqual([
      "/api/v1/workbooks/wb_123",
      "/api/v1/workbooks/wb_123/sheets/summary/render",
      "/api/v1/workbooks/wb_123/transforms",
      "/api/v1/workbooks/wb_123/charts",
      "/api/v1/workbooks/wb_123/sheets/summary",
    ]);
  });
});
