"""D2B Python SDK — Spreadsheets for AI Agents.

A thin, agent-native client over the D2B v1 API. The ergonomics live
in behaviour, not just types:

- automatic ``Idempotency-Key`` on every mutating call (retry-safe);
- transparent retry with backoff on 429/5xx;
- problem+json errors surfaced as typed exceptions carrying
  ``suggested_fix`` (written for LLMs to read and react);
- ``jobs.wait()`` for the async ingest flow;
- ``webhooks.verify_signature()`` for the delivery HMAC.

Quickstart::

    from d2b import D2BClient

    client = D2BClient(api_key="d2b_pat_...", base_url="https://d2b.dev")
    wb = client.workbooks.create(title="monthly")
    job = client.sources.upload(wb["id"], "sales.xlsx", wait=True)
    for t in client.tables.list(wb["id"]):
        print(t["name"], t["row_count"])
"""
from ._version import __version__
from .client import (
    ConflictError,
    D2BClient,
    D2BError,
    NotFoundError,
    PolicyError,
)

__all__ = [
    "D2BClient",
    "D2BError",
    "ConflictError",
    "NotFoundError",
    "PolicyError",
    "__version__",
]
