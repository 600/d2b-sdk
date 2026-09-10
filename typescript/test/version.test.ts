import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { VERSION } from "../src/index.js";

describe("VERSION", () => {
  it("matches package.json — the published version has one meaning", () => {
    // The header `X-D2B-Client: d2b-node/<VERSION>` is how the server
    // attributes traffic; a version that drifts from the tarball's makes
    // that attribution a lie.
    const pkg = JSON.parse(
      readFileSync(new URL("../package.json", import.meta.url), "utf8"),
    );
    expect(VERSION).toBe(pkg.version);
  });
});
