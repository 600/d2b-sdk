from __future__ import annotations

import httpx

from d2b import D2BClient


def test_sdk_encodes_dynamic_path_segments_before_request() -> None:
    seen: list[bytes] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path)
            return httpx.Response(200, json={})

    http = httpx.Client(base_url="https://api.example.test", transport=Transport())
    client = D2BClient(api_key="d2b_pat_test", http_client=http)

    client.workbooks.get("../me/data")
    client.tables.rows("victim/tables/payroll/rows", "finance/2026")
    client.jobs.get("job123/../../me/webhooks")
    client.webhooks.delete("../workbooks/victim")

    assert seen == [
        b"/api/v1/workbooks/..%2Fme%2Fdata",
        b"/api/v1/workbooks/victim%2Ftables%2Fpayroll%2Frows/tables/finance%2F2026/rows?limit=100&offset=0",
        b"/api/v1/jobs/job123%2F..%2F..%2Fme%2Fwebhooks",
        b"/api/v1/me/webhooks/..%2Fworkbooks%2Fvictim",
    ]


def test_sdk_workbooks_carry_workspace_scope() -> None:
    """Creation-in-context + scoped listing reach the wire as workspace_id."""
    seen: list[tuple[str, bytes, bytes]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.raw_path, request.content))
            return httpx.Response(200, json={"workbooks": []})

    http = httpx.Client(base_url="https://api.example.test", transport=Transport())
    client = D2BClient(api_key="d2b_pat_test", http_client=http)

    client.workbooks.create(title="wb", workspace_id="ws-1")
    client.workbooks.list(workspace_id="personal")
    client.workbooks.list()

    assert seen[0][0] == "POST"
    assert b'"workspace_id": "ws-1"' in seen[0][2] or b'"workspace_id":"ws-1"' in seen[0][2]
    # list() asks for the largest page and follows next_cursor (a workspace of
    # thousands comes back whole); the workspace scope rides along.
    assert seen[1][1] == b"/api/v1/me/workbooks?limit=200&workspace_id=personal"
    assert seen[2][1] == b"/api/v1/me/workbooks?limit=200"


def test_sdk_file_links_and_cloud_paths() -> None:
    seen: list[tuple[str, bytes]] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.raw_path))
            return httpx.Response(200, json={"links": [], "items": []})

    http = httpx.Client(base_url="https://api.example.test", transport=Transport())
    client = D2BClient(api_key="d2b_pat_test", http_client=http)
    client.sources.list_cloud("sharepoint", query="sales")
    client.sources.import_cloud("wb", "sharepoint", "item-1", "sales.xlsx")
    client.file_links.track("wb", "item-1", "sales.xlsx")
    client.file_links.track_local("wb", b"bytes", filename="sales.xlsx", origin="/tmp/sales.xlsx")
    client.file_links.push("wb", "l1", b"bytes", filename="sales.xlsx", modified_by="mbp")
    client.file_links.list("wb")
    client.file_links.sync("wb", "l1")
    client.file_links.diff("wb", "l1", 2, against=1)
    client.file_links.cells("wb", "l1", 1, sheet="Sales")
    client.file_links.revert("wb", "l1", 1)
    assert [m + " " + p.decode() for m, p in seen] == [
        "GET /api/v1/cloud-files/sharepoint?folder_id=root&q=sales",
        "POST /api/v1/workbooks/wb/sources/cloud",
        "POST /api/v1/workbooks/wb/file-links",
        "POST /api/v1/workbooks/wb/file-links/local",
        "POST /api/v1/workbooks/wb/file-links/l1/versions",
        "GET /api/v1/workbooks/wb/file-links",
        "POST /api/v1/workbooks/wb/file-links/l1/sync",
        "GET /api/v1/workbooks/wb/file-links/l1/versions/2/diff?against=1",
        "GET /api/v1/workbooks/wb/file-links/l1/versions/1/cells?sheet=Sales",
        "POST /api/v1/workbooks/wb/file-links/l1/versions/1/revert",
    ]


def test_sdk_workspaces_list_path() -> None:
    seen: list[bytes] = []

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path)
            return httpx.Response(200, json={"workspaces": [{"id": "wsp-1", "name": "Default"}]})

    http = httpx.Client(base_url="https://api.example.test", transport=Transport())
    client = D2BClient(api_key="d2b_pat_test", http_client=http)
    assert client.workspaces.list() == [{"id": "wsp-1", "name": "Default"}]
    assert seen == [b"/api/v1/me/workspaces"]
