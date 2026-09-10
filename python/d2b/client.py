"""The D2B client: one HTTP core + resource namespaces in the tables
vocabulary (the same nouns the API and MCP tools speak)."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ._version import __version__

# Client attribution for the server's product analytics (X-D2B-Client).
# The CLI overrides this to "d2b-cli/<version>" before building its client.
CLIENT_TAG = f"d2b-python/{__version__}"

_MUTATING = ("POST", "PUT", "PATCH", "DELETE")
_RETRY_STATUSES = (429, 502, 503, 504)


class D2BError(Exception):
    """problem+json error. ``suggested_fix`` is written to be actionable
    for both humans and LLMs — surface it, don't swallow it."""

    def __init__(self, status: int, payload: dict[str, Any] | None, fallback: str):
        payload = payload or {}
        self.status = status
        self.type = payload.get("type")
        self.title = payload.get("title")
        self.detail = payload.get("detail") or fallback
        self.suggested_fix = payload.get("suggested_fix")
        self.suggested_fix_cli = payload.get("suggested_fix_cli")
        # 5xx guidance says "report this trace_id" — so the id itself must
        # be part of what the caller sees (DX report 4 §2).
        self.trace_id = payload.get("trace_id")
        msg = f"[{status}] {self.detail}"
        if self.suggested_fix:
            msg += f" — {self.suggested_fix}"
        if self.trace_id:
            msg += f" [trace_id: {self.trace_id}]"
        super().__init__(msg)

    def cli_message(self) -> str:
        """The same error with CLI-vocabulary guidance when the server
        sent one (``suggested_fix`` speaks REST; ``suggested_fix_cli``
        speaks ``d2b ...``)."""
        fix = self.suggested_fix_cli or self.suggested_fix
        msg = f"[{self.status}] {self.detail}"
        if fix:
            msg += f" — {fix}"
        if self.trace_id:
            msg += f" [trace_id: {self.trace_id}]"
        return msg


class NotFoundError(D2BError):
    pass


class ConflictError(D2BError):
    """409 — workbook busy, stale ``expected_version``, or a taken name.
    For row edits: re-read (the rows GET carries ``edit_version``),
    re-apply your change, retry."""


class PolicyError(D2BError):
    """403 — blocked by a column policy or missing scope."""


def _seg(value: str) -> str:
    return quote(str(value), safe="")


def _error_for(status: int, payload: dict | None, fallback: str) -> D2BError:
    if status == 404:
        return NotFoundError(status, payload, fallback)
    if status == 409:
        return ConflictError(status, payload, fallback)
    if status == 403:
        return PolicyError(status, payload, fallback)
    return D2BError(status, payload, fallback)


class D2BClient:
    """Synchronous client. ``api_key`` is a PAT (``d2b_pat_...``)."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        http_client: httpx.Client | None = None,
    ):
        if http_client is not None:
            # Injection point for tests (e.g. an in-process ASGI test
            # client) — auth still applied here so callers don't have to.
            self._http = http_client
            self._http.headers["Authorization"] = f"Bearer {api_key}"
            self._http.headers["X-D2B-Client"] = CLIENT_TAG
            self._http.headers["User-Agent"] = CLIENT_TAG
        else:
            if not base_url:
                raise ValueError("base_url is required")
            self._http = httpx.Client(
                base_url=base_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-D2B-Client": CLIENT_TAG,
                    # Same value on the standard header: proxies that strip
                    # unknown headers still leave the server something to
                    # classify the client by.
                    "User-Agent": CLIENT_TAG,
                },
                timeout=timeout,
            )
        self._max_retries = max_retries
        self.workbooks = _Workbooks(self)
        self.workspaces = _Workspaces(self)
        self.sources = _Sources(self)
        self.file_links = _FileLinks(self)
        self.tables = _Tables(self)
        self.query = _Query(self)
        self.transforms = _Transforms(self)
        self.versions = _Versions(self)
        self.jobs = _Jobs(self)
        self.sheets = _Sheets(self)
        self.export = _Export(self)
        self.charts = _Charts(self)
        self.webhooks = _Webhooks(self)

    # -- HTTP core -----------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
        data: dict | None = None,
        idempotency_key: str | None = None,
        raw: bool = False,
        with_headers: bool = False,
    ) -> Any:
        headers: dict[str, str] = {}
        if method in _MUTATING:
            # Retry-safe by default: every mutation carries a key, so a
            # network-level retry can never double-apply.
            headers["Idempotency-Key"] = idempotency_key or uuid.uuid4().hex
        attempt = 0
        while True:
            resp = self._http.request(
                method, f"/api/v1{path}", json=json_body, params=params,
                files=files, data=data, headers=headers,
            )
            if resp.status_code in _RETRY_STATUSES and attempt < self._max_retries:
                attempt += 1
                time.sleep(min(2.0**attempt * 0.25, 8.0))
                continue
            break
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except Exception:
                payload = None
            raise _error_for(resp.status_code, payload, resp.text[:500])
        if raw:
            if with_headers:
                return resp.content, {k.lower(): v for k, v in resp.headers.items()}
            return resp.content
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> D2BClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Resource:
    def __init__(self, client: D2BClient):
        self._c = client


class _Workbooks(_Resource):
    def create(
        self, *, title: str | None = None, workspace_id: str | None = None
    ) -> dict:
        """Create a workbook. ``workspace_id`` places it in that workspace
        (creation-in-context — e.g. a provisioned customer workspace, or the
        self-relative alias ``"personal"``). A ``workspace:<id>``-pinned key
        defaults to its own workspace."""
        body: dict = {"title": title}
        if workspace_id is not None:
            body["workspace_id"] = workspace_id
        return self._c.request("POST", "/workbooks", json_body=body)

    def delete(self, workbook_id: str) -> None:
        """Delete a workbook you own (requires ``workbooks:delete`` — a default
        ``d2b login`` credential holds it)."""
        self._c.request("DELETE", f"/workbooks/{_seg(workbook_id)}")

    def get(self, workbook_id: str) -> dict:
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}")

    def list(self, *, workspace_id: str | None = None) -> list[dict]:
        """Every workbook the credential can see (optionally one workspace's).
        The endpoint pages at 200; this follows ``next_cursor`` to the end,
        so a workspace of thousands comes back whole."""
        params: dict[str, Any] = {"limit": 200}
        if workspace_id is not None:
            params["workspace_id"] = workspace_id
        out: list[dict] = []
        while True:
            page = self._c.request("GET", "/me/workbooks", params=params)
            out.extend(page.get("workbooks") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                return out
            params = {**params, "cursor": cursor}


class _Workspaces(_Resource):
    def list(self) -> list[dict]:
        """Workspaces this credential can address, by name. ``is_default``
        marks where ``workbooks.create`` lands without ``workspace_id``."""
        return self._c.request("GET", "/me/workspaces")["workspaces"]


def _read_file(file: str | Path | bytes, filename: str | None) -> tuple[bytes, str]:
    if isinstance(file, (str, Path)):
        return Path(file).read_bytes(), filename or Path(file).name
    return file, filename or "upload.xlsx"


class _FileLinks(_Resource):
    """An xlsx on OneDrive / SharePoint under D2B version control: every
    save becomes a version; diff, read a version's cells, revert D2B's
    side. D2B never writes to the drive."""

    def track(self, workbook_id: str, item_id: str, filename: str) -> dict:
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/file-links",
            json_body={"item_id": item_id, "filename": filename},
        )

    def list(self, workbook_id: str) -> list[dict]:
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/file-links")["links"]

    def track_local(
        self, workbook_id: str, file: str | Path | bytes, *, filename: str | None = None,
        origin: str = "", as_name: str | None = None, on_existing: str = "auto",
    ) -> dict:
        """Track a file that lives on this machine: the bytes become version
        1; push later changes with :meth:`push` (``d2b watch`` does both).
        ``as_name`` / ``on_existing`` (auto | seed | replace | refuse) say
        what to do when the workbook already holds a same-name source."""
        content, name = _read_file(file, filename)
        data: dict = {"origin": origin, "on_existing": on_existing}
        if as_name:
            data["as_name"] = as_name
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/file-links/local",
            files={"file": (name, content)}, data=data,
        )

    def push(
        self, workbook_id: str, link_id: str, file: str | Path | bytes, *,
        filename: str | None = None, modified_by: str | None = None,
    ) -> dict:
        """Push the file's current bytes as the next version (identical
        bytes cut no version — ``changed`` false)."""
        content, name = _read_file(file, filename)
        data = {"modified_by": modified_by} if modified_by else None
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/file-links/{_seg(link_id)}/versions",
            files={"file": (name, content)}, data=data,
        )

    def sync(self, workbook_id: str, link_id: str) -> dict:
        return self._c.request("POST", f"/workbooks/{_seg(workbook_id)}/file-links/{_seg(link_id)}/sync")

    def diff(self, workbook_id: str, link_id: str, n: int, *, against: int | None = None) -> dict:
        params = {"against": against} if against is not None else None
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/file-links/{_seg(link_id)}/versions/{n}/diff",
            params=params,
        )

    def cells(self, workbook_id: str, link_id: str, n: int, *, sheet: str | None = None) -> dict:
        params = {"sheet": sheet} if sheet else None
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/file-links/{_seg(link_id)}/versions/{n}/cells",
            params=params,
        )

    def revert(self, workbook_id: str, link_id: str, n: int) -> dict:
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/file-links/{_seg(link_id)}/versions/{n}/revert",
        )


class _Sources(_Resource):
    def upload(
        self,
        workbook_id: str,
        file: str | Path | bytes,
        *,
        filename: str | None = None,
        mode: str = "auto",
        structuring: str | None = None,
        async_: bool = False,
        wait: bool = False,
        timeout: float = 600.0,
    ) -> dict:
        """Upload a file. ``mode="staged"`` lands bytes only (follow with
        analyze → materialize). ``wait=True`` uses the async form and
        polls the job to completion — the friendly default for big
        files. ``structuring`` (auto mode): "skip" lands the raw baseline
        only (~1s — BYO-LLM callers reshape with their own model);
        "defer" lands raw now and swaps the LLM-structured tables in
        later (artifact.updated events fire on the swap)."""
        if isinstance(file, (str, Path)):
            content = Path(file).read_bytes()
            filename = filename or Path(file).name
        else:
            content = file
            filename = filename or "upload.bin"
        use_async = async_ or wait
        form = {"mode": mode, "async": "true" if use_async else "false"}
        if structuring is not None:
            form["structuring"] = structuring
        out = self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/sources",
            files={"file": (filename, content)},
            data=form,
        )
        if use_async and wait:
            job = self._c.jobs.wait(out["job_id"], timeout=timeout)
            return job["result"]
        return out

    def list(self, workbook_id: str) -> list[dict]:
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/sources")["sources"]

    def list_cloud(
        self, provider: str, *, folder_id: str = "root", query: str | None = None,
    ) -> list[dict]:
        """Browse the linked Google Drive (``google_drive``) or OneDrive /
        SharePoint (``sharepoint``) for importable files. Folders come
        first (``isFolder``); SharePoint also searches by name (``query``)."""
        params: dict = {"folder_id": folder_id}
        if query:
            params["q"] = query
        return self._c.request("GET", f"/cloud-files/{_seg(provider)}", params=params)["items"]

    def import_cloud(self, workbook_id: str, provider: str, file_id: str, filename: str) -> dict:
        """Import a drive file into the workbook (same ingest as upload,
        provenance stamped for later refresh)."""
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/sources/cloud",
            json_body={"provider": provider, "file_id": file_id, "filename": filename},
        )

    def analyze(self, workbook_id: str, source_name: str) -> dict:
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/sources/{_seg(source_name)}/analyze",
        )

    def get_parse_spec(self, workbook_id: str, source_name: str) -> dict:
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/sources/{_seg(source_name)}/parse-spec",
        )

    def update_parse_spec(self, workbook_id: str, source_name: str, regions: list[dict]) -> dict:
        return self._c.request(
            "PUT", f"/workbooks/{_seg(workbook_id)}/sources/{_seg(source_name)}/parse-spec",
            json_body={"regions": regions},
        )

    def materialize(
        self, workbook_id: str, source_name: str,
        *, region_ids: list[str] | None = None, target: dict | None = None,
    ) -> dict:
        body: dict = {}
        if region_ids is not None:
            body["region_ids"] = region_ids
        if target is not None:
            body["target"] = target
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/sources/{_seg(source_name)}/materialize",
            json_body=body or None,
        )

    def preview(self, workbook_id: str, source_name: str, *, rows: int = 20) -> dict:
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/sources/{_seg(source_name)}/preview",
            params={"rows": rows},
        )

    def download_original(self, workbook_id: str, source_name: str) -> bytes:
        """L0 reproduction: the uploaded bytes, byte-identical."""
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/sources/{_seg(source_name)}/download",
            raw=True,
        )

    def render_template(
        self, workbook_id: str, source_name: str,
        *, overrides: dict[str, str] | None = None,
    ) -> bytes:
        """L1 reproduction: the original xlsx with data-region values
        replaced by the current tables (styles/charts preserved).

        ``overrides`` maps a region-linked table name → a replacement
        (e.g. transform) table name, so a derived table renders into the
        template in place of its source region — faithful output with the
        computation kept server-side and lineage-traceable."""
        params = {}
        if overrides:
            import json
            params["overrides"] = json.dumps(overrides, ensure_ascii=False)
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/sources/{_seg(source_name)}/render",
            params=params or None, raw=True,
        )

    def revise(
        self, workbook_id: str, source_name: str, *,
        transform: dict, region: dict | None = None,
    ) -> bytes:
        """Faithful edit in ONE call (G5): apply a SQL transform to a region
        and get the original xlsx back with only that region's values changed.

        Folds analyze + materialize + transform + L1 render. ``transform`` is
        ``{"name", "template", "args"?, "artifact_name"?}`` — a
        ``{{ artifact_name }}`` SQL view with ``{{ src }}`` bound to the region
        table (carry ``MIN("__d2b_row_id")`` to keep row positions on a folding
        merge). ``region`` selects the target: ``{"region_id": ...}`` or
        ``{"sheet"?: ..., "range": ...}``, or ``None`` for the source's single
        data region. Styles/charts preserved, in-region formulas kept, and the
        transform persists in the lineage DAG."""
        body: dict = {"transform": transform}
        if region is not None:
            body["region"] = region
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/sources/{_seg(source_name)}/revise",
            json_body=body, raw=True,
        )


class _Tables(_Resource):
    def list(self, workbook_id: str, *, include_archived: bool = False) -> list[dict]:
        """Tables and views. Archived raws (superseded at ingest) are hidden
        unless ``include_archived`` — ``unarchive`` brings one back."""
        params = {"include_archived": "true"} if include_archived else None
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/tables", params=params,
        )["artifacts"]

    def create(self, workbook_id: str, name: str, columns: list[dict]) -> dict:
        """Create an empty editable table — the 'open a blank sheet and
        start writing' gesture. Upsert rows immediately after with
        ``expected_version=1``."""
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/tables",
            json_body={"name": name, "columns": columns},
        )["table"]

    def get(self, workbook_id: str, name: str) -> dict:
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}")

    def schema(self, workbook_id: str, name: str) -> dict:
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/schema")

    def row_json_schema(self, workbook_id: str, name: str) -> dict:
        """JSON Schema for one row of this table (policy-applied columns;
        ``__d2b_row_id`` omitted = insert, set = update). Feed it to a
        harness to constrained-decode ``upsert_rows`` payloads."""
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/schema",
            params={"format": "json-schema"},
        )["json_schema"]

    def rows(self, workbook_id: str, name: str, *, limit: int = 100, offset: int = 0) -> dict:
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/rows",
            params={"limit": limit, "offset": offset},
        )

    def iter_rows(self, workbook_id: str, name: str, *, page_size: int = 1000):
        """Iterate every row as a dict, paging transparently."""
        offset = 0
        while True:
            page = self.rows(workbook_id, name, limit=page_size, offset=offset)
            cols = [c["name"] for c in page["columns"]]
            for row in page["rows"]:
                yield dict(zip(cols, row))
            offset = page.get("next_offset")
            if offset is None:
                return

    def upsert_rows(
        self, workbook_id: str, name: str, rows: list[dict],
        *, expected_version: int | None, actor: str | None = None,
    ) -> dict:
        """Optimistic-locked write. On ConflictError: re-read
        (``rows()`` carries ``edit_version``), re-apply, retry —
        deliberately not automatic, so concurrent edits are never
        silently clobbered."""
        body: dict = {"rows": rows, "expected_version": expected_version}
        if actor:
            body["actor"] = actor
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/rows", json_body=body,
        )

    def delete_rows(
        self, workbook_id: str, name: str, row_ids: list[int],
        *, expected_version: int, actor: str | None = None,
    ) -> dict:
        body: dict = {"row_ids": row_ids, "expected_version": expected_version}
        if actor:
            body["actor"] = actor
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/rows/delete",
            json_body=body,
        )

    def add_column(
        self, workbook_id: str, name: str, column: str, type: str = "VARCHAR",
        *, default=None, expected_version: int | None, actor: str | None = None,
    ) -> dict:
        """Schema evolution: add a column (NULL, or ``default`` everywhere)
        to an editable table — it lands before ``__d2b_row_id``."""
        body: dict = {"column": column, "type": type, "expected_version": expected_version}
        if default is not None:
            body["default"] = default
        if actor:
            body["actor"] = actor
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/columns", json_body=body,
        )

    def rename_column(
        self, workbook_id: str, name: str, column: str, new_name: str,
        *, expected_version: int | None, actor: str | None = None,
    ) -> dict:
        body: dict = {"new_name": new_name, "expected_version": expected_version}
        if actor:
            body["actor"] = actor
        return self._c.request(
            "PATCH", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/columns/{_seg(column)}",
            json_body=body,
        )

    def retype_column(
        self, workbook_id: str, name: str, column: str, type: str,
        *, expected_version: int | None, actor: str | None = None,
    ) -> dict:
        """Change a column's type, casting existing values (a value that
        can't cast cleanly raises)."""
        body: dict = {"type": type, "expected_version": expected_version}
        if actor:
            body["actor"] = actor
        return self._c.request(
            "PATCH", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/columns/{_seg(column)}",
            json_body=body,
        )

    def drop_column(
        self, workbook_id: str, name: str, column: str,
        *, expected_version: int | None, actor: str | None = None,
    ) -> dict:
        params: dict = {"expected_version": expected_version}
        if actor:
            params["actor"] = actor
        return self._c.request(
            "DELETE", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/columns/{_seg(column)}",
            params=params,
        )

    def a1(self, workbook_id: str, name: str, range: str) -> list[list]:
        """A1 read facade: row 1 = header, data from row 2."""
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/a1",
            params={"range": range},
        )["values"]

    def write_a1(
        self, workbook_id: str, name: str, range: str, values: list[list],
        *, expected_version: int | None, actor: str | None = None,
    ) -> dict:
        """A1 write facade: set a rectangle of cells. ``values`` is a
        row-major block matching the range shape; grid row 2 is the
        first data row, rows past the bottom append contiguously. Same
        optimistic locking as upsert_rows."""
        body: dict = {"range": range, "values": values, "expected_version": expected_version}
        if actor:
            body["actor"] = actor
        return self._c.request(
            "PUT", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/a1", json_body=body,
        )

    def profile(self, workbook_id: str, name: str) -> dict:
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/profile")

    def lineage(self, workbook_id: str, name: str) -> dict:
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/lineage")

    def unarchive(self, workbook_id: str, name: str) -> dict:
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/unarchive",
        )

    def rename(self, workbook_id: str, name: str, new_name: str) -> dict:
        """Rename a table/view's display name (the immutable id and physical
        table are unchanged, so downstream transforms follow and reads work
        under the new name). Returns the artifact with its (unchanged) id."""
        return self._c.request(
            "PATCH", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}",
            json_body={"new_name": new_name},
        )

    def delete(self, workbook_id: str, name: str) -> None:
        self._c.request("DELETE", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}")

    def merge(
        self, workbook_id: str, name: str, content: bytes, *,
        filename: str, branch_id: str, actor: str | None = None,
    ) -> dict:
        """3-way merge an edited export (xlsx/csv carrying ``__d2b_row_id``)
        back into its base table against the branch the export recorded.
        File-only cell changes apply as attributed edits; cells both sides
        changed land in the conflict queue (D2B's value stays until resolved)."""
        data = {"branch_id": branch_id}
        if actor:
            data["actor"] = actor
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/merge",
            files={"file": (filename, content)}, data=data,
        )

    # -- live formulas (delivery projection: column → per-row Excel formula) --

    def formulas(self, workbook_id: str, name: str) -> dict:
        """The table's formula columns as ``{column: expr}``."""
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/formulas",
        )["formulas"]

    def set_formula(
        self, workbook_id: str, name: str, column: str, expr: str,
        *, actor: str | None = None,
    ) -> dict:
        """Deliver ``column`` as a per-row Excel formula instead of its frozen
        value (e.g. ``"{running} / {count}"``). Reference columns as
        ``{column_name}`` placeholders, never A1 cell addresses; the stored
        value is untouched. The response carries a ``verification`` report —
        whether the formula reproduces the column's current values."""
        body: dict = {"expr": expr}
        if actor:
            body["actor"] = actor
        return self._c.request(
            "PUT", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/columns/{_seg(column)}/formula",
            json_body=body,
        )

    def clear_formula(self, workbook_id: str, name: str, column: str) -> dict:
        """Drop a column's formula — it delivers its stored value again."""
        return self._c.request(
            "DELETE",
            f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/columns/{_seg(column)}/formula",
        )

    # -- styles (3-layer: spec / rules / annotations) -----------------------

    def get_style(self, workbook_id: str, name: str) -> dict:
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/style")

    def set_style(self, workbook_id: str, name: str, spec: dict) -> dict:
        """Bulk styling belongs in spec rules ('amount < 0' → red) —
        they re-evaluate at delivery and never drift."""
        return self._c.request(
            "PUT", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/style",
            json_body={"spec": spec},
        )

    def annotate(self, workbook_id: str, name: str, annotations: list[dict]) -> dict:
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/tables/{_seg(name)}/style/annotations",
            json_body={"annotations": annotations},
        )


class _Query(_Resource):
    def sql(self, workbook_id: str, sql: str, *, limit: int = 1000) -> dict:
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/query",
            json_body={"sql": sql, "limit": limit},
        )

    def validate(self, workbook_id: str, sql: str) -> dict:
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/query/validate",
            json_body={"sql": sql},
        )

    def result(self, result_id: str) -> dict:
        return self._c.request("GET", f"/results/{_seg(result_id)}")


class _Transforms(_Resource):
    def list(self, workbook_id: str) -> list[dict]:
        """Every authored transform currently producing an artifact — name,
        kind, template and its output binding (``artifact_name`` / ``args``
        / ``layer``), one entry per output. The read half of ``create``:
        what ``d2b pull`` turns into ``transforms/*.sql|py`` files."""
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/transforms",
        )["transforms"]

    def create(
        self, workbook_id: str, *, name: str, kind: str, template: str,
        artifact_name: str, args: dict[str, str] | None = None,
        layer: str | None = None,
    ) -> dict:
        """Author a derived table. Reference upstream tables via
        ``{{ arg }}`` placeholders bound through ``args`` — that is what
        makes lineage traceable (hard-coded names are rejected)."""
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/transforms",
            json_body={
                "name": name, "kind": kind, "template": template,
                "artifact_name": artifact_name, "args": args or {},
                "layer": layer,
            },
        )


class _Versions(_Resource):
    def commit(self, workbook_id: str, label: str, *, summary: str | None = None) -> dict:
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/versions",
            json_body={"label": label, "summary": summary},
        )

    def list(self, workbook_id: str) -> list[dict]:
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/versions")["versions"]

    def revert(self, workbook_id: str, label: str) -> dict:
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/versions/{_seg(label)}/revert",
        )


class _Jobs(_Resource):
    def get(self, job_id: str) -> dict:
        return self._c.request("GET", f"/jobs/{_seg(job_id)}")

    def wait(self, job_id: str, *, timeout: float = 600.0, interval: float = 1.0) -> dict:
        """Poll until the job reaches a terminal state. Raises D2BError
        on job failure with the job's error message."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if job["status"] == "succeeded":
                return job
            if job["status"] == "failed":
                raise D2BError(500, {"detail": job.get("error") or "job failed"}, "job failed")
            if time.monotonic() > deadline:
                raise TimeoutError(f"job {job_id} did not finish within {timeout}s")
            time.sleep(interval)


class _Sheets(_Resource):
    def put(self, workbook_id: str, name: str, blocks: list[dict]) -> dict:
        """Compose a presentation sheet: blocks REFERENCE tables (n:m);
        the sheet owns no data."""
        return self._c.request(
            "PUT", f"/workbooks/{_seg(workbook_id)}/sheets/{_seg(name)}",
            json_body={"spec": {"blocks": blocks}},
        )

    def list(self, workbook_id: str) -> list[dict]:
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/sheets")["sheets"]

    def get(self, workbook_id: str, name: str) -> dict:
        """One sheet: ``{"name", "spec": {"blocks": [...]}}``."""
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/sheets/{_seg(name)}")

    def render(self, workbook_id: str, name: str) -> bytes:
        return self._c.request(
            "GET", f"/workbooks/{_seg(workbook_id)}/sheets/{_seg(name)}/render", raw=True,
        )

    def delete(self, workbook_id: str, name: str) -> None:
        self._c.request("DELETE", f"/workbooks/{_seg(workbook_id)}/sheets/{_seg(name)}")


class _Export(_Resource):
    def tables(
        self, workbook_id: str, *, tables: list[str] | None = None,
        format: str = "xlsx", formula_mode: str = "values",
        include_row_ids: bool = False, record_branch: bool = False,
    ) -> bytes:
        return self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/export",
            json_body={
                "tables": tables, "format": format,
                "formula_mode": formula_mode,
                "include_row_ids": include_row_ids,
                "record_branch": record_branch,
            },
            raw=True,
        )

    def branch(
        self, workbook_id: str, tables: list[str], *, format: str = "csv",
    ) -> tuple[bytes, str]:
        """Export = branch: the file (row-ids included) plus the branch id
        that ``tables.merge`` later merges the edited file back against.
        Only base (editable) tables can branch."""
        content, headers = self._c.request(
            "POST", f"/workbooks/{_seg(workbook_id)}/export",
            json_body={
                "tables": tables, "format": format, "formula_mode": "values",
                "include_row_ids": True, "record_branch": True,
            },
            raw=True, with_headers=True,
        )
        return content, headers.get("x-d2b-branch-id", "")


class _Charts(_Resource):
    def list(self, workbook_id: str) -> list[dict]:
        """Every chart with its rendered ``config`` and the ``recipe`` (tool +
        params) it was generated from. Read-only: charts are re-generated
        from their recipe, never edited as raw config. ``config`` is omitted
        on governed workbooks."""
        return self._c.request("GET", f"/workbooks/{_seg(workbook_id)}/charts")["charts"]


class _Webhooks(_Resource):
    def create(self, url: str, *, events: list[str] | None = None) -> dict:
        body: dict = {"url": url}
        if events is not None:
            body["events"] = events
        return self._c.request("POST", "/me/webhooks", json_body=body)

    def list(self) -> list[dict]:
        return self._c.request("GET", "/me/webhooks")["webhooks"]

    def delete(self, webhook_id: str) -> None:
        self._c.request("DELETE", f"/me/webhooks/{_seg(webhook_id)}")

    @staticmethod
    def verify_signature(secret: str, payload: bytes, signature: str) -> bool:
        """Verify ``X-D2B-Signature`` (= ``sha256=<hex HMAC-SHA256>``)
        over the RAW request body. Constant-time comparison."""
        mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256)
        return hmac.compare_digest(f"sha256={mac.hexdigest()}", signature)

    @staticmethod
    def parse_event(payload: bytes) -> dict:
        return json.loads(payload.decode("utf-8"))
