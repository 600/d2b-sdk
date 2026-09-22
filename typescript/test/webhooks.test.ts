import { createHmac } from "node:crypto";

import { describe, expect, it } from "vitest";

import { Webhooks } from "../src/index.js";

/**
 * The delivery verifier accepts what the backend's `sign_delivery` makes —
 * the V2 signature over delivery id, send time and body — within the
 * tolerance, and refuses a replay by its age, a changed body or id, a
 * different secret, or a missing header. Mirrors
 * backend/tests/test_python_sdk_integration.py.
 */
describe("Webhooks.verifyDelivery", () => {
  const secret = "sec";
  const body = new TextEncoder().encode('{"event": "artifact.updated"}');
  const sentAt = 1_700_000_000;

  function v2(id: string, ts: number, payload: Uint8Array, key = secret): string {
    const h = createHmac("sha256", key);
    h.update(`${id}.${ts}.`);
    h.update(payload);
    return `sha256=${h.digest("hex")}`;
  }

  const headers = {
    "X-D2B-Delivery": "dlv_1",
    "X-D2B-Timestamp": String(sentAt),
    "X-D2B-Signature-V2": v2("dlv_1", sentAt, body),
  };

  it("accepts a fresh, correctly signed attempt", async () => {
    expect(await Webhooks.verifyDelivery(secret, headers, body, { now: sentAt + 10 })).toBe(true);
    // Header names are matched case-insensitively, and a Headers object works too.
    const lowered = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
    expect(await Webhooks.verifyDelivery(secret, lowered, body, { now: sentAt + 10 })).toBe(true);
    expect(await Webhooks.verifyDelivery(secret, new Headers(headers), body, { now: sentAt + 10 })).toBe(true);
  });

  it("refuses an attempt outside the tolerance, even with a valid signature", async () => {
    expect(await Webhooks.verifyDelivery(secret, headers, body, { now: sentAt + 301 })).toBe(false);
    expect(await Webhooks.verifyDelivery(secret, headers, body, { now: sentAt - 301 })).toBe(false);
    expect(
      await Webhooks.verifyDelivery(secret, headers, body, { now: sentAt + 3600, toleranceSeconds: 7200 }),
    ).toBe(true);
  });

  it("refuses a changed body, id, secret, or a missing header", async () => {
    const other = new TextEncoder().encode('{"event": "artifact.updated"} ');
    expect(await Webhooks.verifyDelivery(secret, headers, other, { now: sentAt })).toBe(false);
    expect(
      await Webhooks.verifyDelivery(secret, { ...headers, "X-D2B-Delivery": "dlv_2" }, body, { now: sentAt }),
    ).toBe(false);
    expect(await Webhooks.verifyDelivery("other", headers, body, { now: sentAt })).toBe(false);
    const { "X-D2B-Timestamp": _dropped, ...withoutTimestamp } = headers;
    expect(await Webhooks.verifyDelivery(secret, withoutTimestamp, body, { now: sentAt })).toBe(false);
    expect(
      await Webhooks.verifyDelivery(secret, { ...headers, "X-D2B-Timestamp": "soon" }, body, { now: sentAt }),
    ).toBe(false);
  });

  it("still verifies the body-only signature", async () => {
    const h = createHmac("sha256", secret);
    h.update(body);
    expect(await Webhooks.verifySignature(secret, body, `sha256=${h.digest("hex")}`)).toBe(true);
    expect(await Webhooks.verifySignature(secret, body, "sha256=beef")).toBe(false);
  });
});
