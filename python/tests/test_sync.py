"""``d2b pull`` / ``d2b push`` end to end against a fake server.

Pins the on-disk layout (transforms / sheets / charts / data), the
conflict rule in both directions for text sections, the "unchanged → no
request" economy (every transform POST re-runs and is metered), the
export = branch / merge loop for data, ``--prune``, upstream-first push
ordering, and the label → path mapping.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest
import yaml

from d2b import D2BClient
from d2b.cli import main
from d2b.sync import (
    SyncError,
    canonical_json,
    csv_canonical,
    digest,
    execute_push,
    path_for,
    plan_push,
)

WB = "wb-1"
T_SRC = 'CREATE OR REPLACE VIEW "{{ artifact_name }}" AS SELECT * FROM "{{ src }}"'
SUMMARY = {"blocks": [{"kind": "heading", "text": "月次"}, {"kind": "table_view", "table": "sales"}]}
CHART = {
    "name": "trend", "chart_type": "line", "title": "Trend",
    "config": {"type": "line", "data": {"labels": ["a"], "datasets": [{"data": [1]}]}},
    "source_table": "sales", "recipe": {"tool": "create_line_chart", "params": {"x": "m"}},
    "artifact_id": "art_1", "config_omitted": None,
}


def _parse_multipart(request: httpx.Request) -> dict[str, tuple[str | None, bytes]]:
    ct = request.headers["content-type"]
    msg = BytesParser().parsebytes(
        b"Content-Type: " + ct.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.read(),
    )
    out: dict[str, tuple[str | None, bytes]] = {}
    for part in msg.get_payload():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_param("filename", header="content-disposition")
        out[str(name)] = (filename, part.get_payload(decode=True) or b"")
    return out


class FakeServer:
    """Just enough of /api/v1 for the bridge: transforms, sheets, charts,
    export (= branch), merge (a simplified per-cell 3-way), delete,
    versions. POST keeps the remote view consistent the way the real
    server does (latest template wins, one row per (name, artifact))."""

    def __init__(self, transforms=None, sheets=None, charts=None, tables=None):
        self.transforms = list(transforms or [])
        self.sheets = {s["name"]: s["spec"] for s in (sheets or [])}
        self.charts = list(charts or [])
        # table → list of row dicts (``__d2b_row_id`` included)
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.branches: dict[str, dict[str, list[dict]]] = {}
        self.posts: list[dict] = []
        self.sheet_puts: list[tuple[str, dict]] = []
        self.merges: list[dict] = []
        self.deleted: list[str] = []
        self.deny_delete: set[str] = set()
        self.versions: list[dict] = []
        self.export_calls: list[dict] = []

    def _csv(self, table: str) -> bytes:
        rows = self.tables[table]
        cols = [c for c in rows[0] if c != "__d2b_row_id"] + ["__d2b_row_id"] if rows else ["__d2b_row_id"]
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\r\n")
        w.writerow(cols)
        for r in reversed(rows):                      # deliberately not row-id order
            w.writerow([r.get(c, "") for c in cols])
        return out.getvalue().encode("utf-8-sig")

    def _merge(self, table: str, branch_id: str, content: bytes) -> dict:
        base = {r["__d2b_row_id"]: r for r in self.branches[branch_id][table]}
        ours = {r["__d2b_row_id"]: r for r in self.tables[table]}
        theirs = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        updated, conflicts = 0, []
        for t in theirs:
            rid = t["__d2b_row_id"]
            if rid not in base or rid not in ours:
                continue
            for col, val in t.items():
                if col == "__d2b_row_id" or col not in base[rid]:
                    continue
                if val == base[rid][col]:
                    continue                         # untouched in the file
                if ours[rid][col] == base[rid][col]:
                    ours[rid][col] = val             # file-only change → apply
                    updated += 1
                elif ours[rid][col] != val:
                    conflicts.append({"row_id": rid, "column": col, "theirs": val, "ours": ours[rid][col]})
        return {"branch_id": branch_id, "applied": {"updated": updated, "inserted": 0, "deleted": 0},
                "conflicts": conflicts}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        base = f"/api/v1/workbooks/{WB}"
        if m == "GET" and path == f"{base}/transforms":
            return httpx.Response(200, json={"transforms": self.transforms})
        if m == "POST" and path == f"{base}/transforms":
            body = json.loads(request.content)
            self.posts.append(body)
            self.transforms = [
                t for t in self.transforms
                if not (t["name"] == body["name"] and t["artifact_name"] == body["artifact_name"])
            ]
            for t in self.transforms:
                if t["name"] == body["name"]:
                    t["template"], t["kind"] = body["template"], body["kind"]
            self.transforms.append(_remote(
                body["name"], body["template"], kind=body["kind"],
                artifact=body["artifact_name"], args=body.get("args") or {},
                layer=body.get("layer"),
            ))
            return httpx.Response(201, json={
                "artifact": {"name": body["artifact_name"], "type": "view"},
                "transform_name": body["name"], "kind": body["kind"],
            })
        if m == "GET" and path == f"{base}/sheets":
            return httpx.Response(200, json={"sheets": [{"name": n, "spec": s} for n, s in self.sheets.items()]})
        if path.startswith(f"{base}/sheets/"):
            name = httpx.URL(path).path.rsplit("/", 1)[1]
            from urllib.parse import unquote
            name = unquote(name)
            if m == "PUT":
                spec = json.loads(request.content)["spec"]
                self.sheets[name] = spec
                self.sheet_puts.append((name, spec))
                return httpx.Response(200, json={"name": name, "spec": spec})
            if m == "DELETE":
                self.sheets.pop(name, None)
                self.deleted.append(f"sheet:{name}")
                return httpx.Response(204)
        if m == "GET" and path == f"{base}/charts":
            return httpx.Response(200, json={"charts": self.charts})
        if m == "POST" and path == f"{base}/export":
            body = json.loads(request.content)
            self.export_calls.append(body)
            (table,) = body["tables"]
            headers = {"content-type": "text/csv; charset=utf-8"}
            if body.get("record_branch"):
                bid = f"branch-{len(self.branches) + 1}"
                self.branches[bid] = {table: [dict(r) for r in self.tables[table]]}
                headers["X-D2B-Branch-Id"] = bid
            return httpx.Response(200, content=self._csv(table), headers=headers)
        if m == "POST" and path.endswith("/merge") and path.startswith(f"{base}/tables/"):
            from urllib.parse import unquote
            table = unquote(path[len(f"{base}/tables/"):-len("/merge")])
            parts = _parse_multipart(request)
            branch_id = parts["branch_id"][1].decode()
            filename, content = parts["file"]
            self.merges.append({"table": table, "branch_id": branch_id, "filename": filename, "content": content})
            if branch_id not in self.branches:
                return httpx.Response(404, json={"detail": "unknown branch", "title": "No branch"})
            return httpx.Response(200, json=self._merge(table, branch_id, content))
        if m == "DELETE" and path.startswith(f"{base}/tables/"):
            from urllib.parse import unquote
            name = unquote(path.rsplit("/", 1)[1])
            if name in self.deny_delete:
                return httpx.Response(403, json={"detail": "Token missing required scope: workbooks:delete",
                                                 "title": "Forbidden", "suggested_fix": "use a workbooks:delete PAT"})
            self.deleted.append(f"artifact:{name}")
            self.transforms = [t for t in self.transforms if t["artifact_name"] != name]
            return httpx.Response(204)
        if m == "POST" and path == f"{base}/versions":
            body = json.loads(request.content)
            self.versions.append(body)
            return httpx.Response(201, json={"label": body["label"], "snapshot_id": 7})
        return httpx.Response(404, json={"detail": f"unhandled {m} {path}"})


def _remote(name, template=T_SRC, *, kind="sql", artifact=None, args=None, layer=None):
    return {
        "name": name, "kind": kind, "template": template,
        "artifact_name": artifact or name.replace("/", "_"),
        "args": {"src": "sales"} if args is None else args,
        "layer": layer, "file": f"{name.replace('/', '_')}_{digest(template)[:12]}.{kind}",
    }


def _rows(*pairs):
    return [{"product": p, "qty": q, "__d2b_row_id": str(i + 1)} for i, (p, q) in enumerate(pairs)]


def _client(server: FakeServer) -> D2BClient:
    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return server.handle(request)

    http = httpx.Client(base_url="https://api.example.test", transport=Transport())
    return D2BClient(api_key="d2b_pat_test", http_client=http)


def _run(client, argv, capsys):
    rc = main(argv, client=client)
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    return rc, out, captured.err


def _manifest(root: Path) -> dict:
    return json.loads((root / "d2b.json").read_text(encoding="utf-8"))


def _write_manifest(root: Path, manifest: dict) -> None:
    (root / "d2b.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def _pulled(server: FakeServer, root: Path, capsys, *extra) -> D2BClient:
    client = _client(server)
    rc, _, err = _run(client, ["pull", "--workbook", WB, "--dir", str(root), *extra], capsys)
    assert rc == 0, err
    return client


# ── pull ──────────────────────────────────────────────────────────────────────


def test_pull_writes_every_section_and_the_manifest(tmp_path, capsys):
    server = FakeServer(
        transforms=[
            _remote("agg/monthly"),
            _remote("cf_runway", "import pandas as pd\n", kind="python", artifact="runway", layer="marts"),
        ],
        sheets=[{"name": "summary", "spec": SUMMARY}],
        charts=[CHART],
    )
    rc, out, _ = _run(_client(server), ["pull", "--workbook", WB, "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert out["transforms"]["added"] == ["agg/monthly.sql", "cf_runway.py"]
    assert out["sheets"]["added"] == ["summary.json"]
    assert out["charts"]["added"] == ["trend.json"]
    assert out["data"] == {k: [] for k in ("added", "updated", "unchanged", "removed",
                                            "local_only", "untracked", "conflicts", "errors")}
    assert (tmp_path / "transforms/agg/monthly.sql").read_text(encoding="utf-8") == T_SRC + "\n"
    assert (tmp_path / "transforms/cf_runway.py").read_text(encoding="utf-8") == "import pandas as pd\n"
    assert json.loads((tmp_path / "sheets/summary.json").read_text(encoding="utf-8")) == SUMMARY
    chart = json.loads((tmp_path / "charts/trend.json").read_text(encoding="utf-8"))
    assert chart == {k: CHART[k] for k in ("chart_type", "title", "config", "source_table", "recipe")}

    manifest = _manifest(tmp_path)
    assert manifest["workbook_id"] == WB
    assert manifest["transforms"]["agg/monthly.sql"] == {
        "name": "agg/monthly", "artifact_name": "agg_monthly",
        "args": {"src": "sales"}, "layer": None, "hash": digest(T_SRC),
    }
    assert manifest["transforms"]["cf_runway.py"]["layer"] == "marts"
    assert manifest["sheets"]["summary.json"] == {"name": "summary", "hash": digest(canonical_json(SUMMARY))}
    assert manifest["charts"]["trend.json"]["readonly"] is True
    assert "data" not in manifest                      # unused sections stay out of the file

    # Second pull: the workbook id is remembered, nothing is rewritten.
    rc, out, _ = _run(_client(server), ["pull", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert out["transforms"]["unchanged"] == ["agg/monthly.sql", "cf_runway.py"]
    assert out["sheets"]["unchanged"] == ["summary.json"] and out["charts"]["unchanged"] == ["trend.json"]


def test_pull_takes_server_changes_when_local_is_clean(tmp_path, capsys):
    server = FakeServer([_remote("a")], sheets=[{"name": "s", "spec": SUMMARY}])
    client = _pulled(server, tmp_path, capsys)
    server.transforms = [_remote("a", T_SRC + " LIMIT 5")]
    server.sheets["s"] = {"blocks": [{"kind": "text", "text": "hi"}]}
    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert out["transforms"]["updated"] == ["a.sql"] and out["sheets"]["updated"] == ["s.json"]
    assert (tmp_path / "transforms/a.sql").read_text(encoding="utf-8") == T_SRC + " LIMIT 5\n"
    assert _manifest(tmp_path)["transforms"]["a.sql"]["hash"] == digest(T_SRC + " LIMIT 5")


def test_pull_refuses_to_clobber_local_edits_unless_forced(tmp_path, capsys):
    server = FakeServer([_remote("a")])
    client = _pulled(server, tmp_path, capsys)
    f = tmp_path / "transforms/a.sql"
    f.write_text(T_SRC + " WHERE 1=1\n", encoding="utf-8")
    server.transforms = [_remote("a", T_SRC + " LIMIT 5")]

    rc, out, err = _run(client, ["pull", "--dir", str(tmp_path)], capsys)
    assert rc == 1 and "pull refused" in err
    assert out["transforms"]["conflicts"][0]["file"] == "a.sql"
    assert f.read_text(encoding="utf-8") == T_SRC + " WHERE 1=1\n"      # untouched
    assert _manifest(tmp_path)["transforms"]["a.sql"]["hash"] == digest(T_SRC)  # manifest untouched

    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path), "--force"], capsys)
    assert rc == 0 and out["transforms"]["updated"] == ["a.sql"]
    assert f.read_text(encoding="utf-8") == T_SRC + " LIMIT 5\n"


def test_pull_removes_vanished_and_keeps_local_only_and_untracked(tmp_path, capsys):
    server = FakeServer([_remote("a"), _remote("b")])
    client = _pulled(server, tmp_path, capsys)
    # A transform authored here (manifest entry, never pushed) and a scratch file.
    (tmp_path / "transforms/c.sql").write_text(T_SRC, encoding="utf-8")
    m = _manifest(tmp_path)
    m["transforms"]["c.sql"] = {"artifact_name": "c", "args": {"src": "sales"}}
    _write_manifest(tmp_path, m)
    (tmp_path / "transforms/scratch.sql").write_text("SELECT 1", encoding="utf-8")
    server.transforms = [_remote("a")]                        # b was deleted on the server

    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    t = out["transforms"]
    assert t["removed"] == ["b.sql"] and not (tmp_path / "transforms/b.sql").exists()
    assert t["local_only"] == ["c.sql"] and (tmp_path / "transforms/c.sql").exists()
    assert t["untracked"] == ["scratch.sql"]
    assert set(_manifest(tmp_path)["transforms"]) == {"a.sql", "c.sql"}


def test_pull_json_formatting_is_not_a_change(tmp_path, capsys):
    server = FakeServer(sheets=[{"name": "s", "spec": SUMMARY}])
    client = _pulled(server, tmp_path, capsys)
    (tmp_path / "sheets/s.json").write_text(json.dumps(SUMMARY), encoding="utf-8")   # one line
    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path)], capsys)
    assert rc == 0 and out["sheets"]["unchanged"] == ["s.json"]


def test_pull_refuses_another_workbook_in_a_bound_dir(tmp_path, capsys):
    client = _pulled(FakeServer([_remote("a")]), tmp_path, capsys)
    rc, _, err = _run(client, ["pull", "--workbook", "other", "--dir", str(tmp_path)], capsys)
    assert rc == 1 and "bound to workbook" in err


@pytest.mark.parametrize("rel", [
    "../../outside.sql",     # traversal
    "/tmp/outside.sql",      # posix absolute
    "C:\\outside.sql",       # windows drive
    "a/../outside.sql",      # un-normalised traversal
    "a/./b.sql",             # un-normalised, no traversal
    ".",                     # the section directory itself
    "",                      # empty key
])
def test_pull_refuses_unsafe_manifest_paths(tmp_path, capsys, rel):
    _write_manifest(tmp_path, {
        "workbook_id": WB,
        "transforms": {rel: {"name": "a", "hash": digest(T_SRC)}},
    })

    rc, _, err = _run(_client(FakeServer([_remote("a", "attacker content")])),
                      ["pull", "--dir", str(tmp_path)], capsys)

    assert rc == 1 and "unsafe 'transforms' path" in err


def test_pull_never_writes_outside_the_dir(tmp_path, capsys):
    """A manifest key that escapes the section must not reach the disk —
    the extension decides overwrite vs. unlink, so pin both."""
    root = tmp_path / "repo"
    root.mkdir()
    victim = tmp_path / "victim.sql"
    victim.write_text("do not replace", encoding="utf-8")
    _write_manifest(root, {
        "workbook_id": WB,
        "transforms": {"../victim.sql": {"name": "a", "hash": digest(T_SRC)}},
    })

    rc, _, err = _run(_client(FakeServer([_remote("a", "attacker content")])),
                      ["pull", "--dir", str(root)], capsys)

    assert rc == 1 and "unsafe 'transforms' path" in err
    assert victim.read_text(encoding="utf-8") == "do not replace"


def test_pull_never_unlinks_outside_the_dir(tmp_path, capsys):
    """Same key, an extension the incoming transform cannot reuse: the
    entry becomes a removal, which used to unlink straight through it."""
    root = tmp_path / "repo"
    root.mkdir()
    victim = tmp_path / "victim.sh"
    victim.write_text("# keep me", encoding="utf-8")
    _write_manifest(root, {
        "workbook_id": WB,
        "transforms": {"../victim.sh": {"name": "a", "hash": digest(T_SRC)}},
    })

    rc, _, err = _run(_client(FakeServer([_remote("a")])), ["pull", "--dir", str(root)], capsys)

    assert rc == 1 and "unsafe 'transforms' path" in err
    assert victim.exists()


def test_pull_refuses_to_write_through_symlinked_dir(tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "transforms").mkdir()
    os.symlink(outside, tmp_path / "transforms" / "linked")

    rc, _, err = _run(_client(FakeServer([_remote("linked/a", "attacker content")])),
                      ["pull", "--workbook", WB, "--dir", str(tmp_path)], capsys)

    assert rc == 1 and "symbolic link" in err
    assert not (outside / "a.sql").exists()


def test_pull_refuses_to_write_through_symlinked_file(tmp_path, capsys):
    """The leaf itself is the symlink — the common shape, and the one the
    error message must name as a link rather than as a path escape."""
    victim = tmp_path / "victim.sql"
    victim.write_text("do not replace", encoding="utf-8")
    (tmp_path / "transforms").mkdir()
    os.symlink(victim, tmp_path / "transforms" / "a.sql")

    rc, _, err = _run(_client(FakeServer([_remote("a", "attacker content")])),
                      ["pull", "--workbook", WB, "--dir", str(tmp_path)], capsys)

    assert rc == 1 and "refusing symbolic link in transforms: a.sql" in err
    assert victim.read_text(encoding="utf-8") == "do not replace"


def test_pull_ignores_symlinks_the_section_never_reads(tmp_path, capsys):
    """Only files the sync would read are its business — a symlinked
    README next to the transforms is not a reason to refuse the pull."""
    (tmp_path / "notes.md").write_text("team notes", encoding="utf-8")
    (tmp_path / "transforms").mkdir()
    os.symlink(tmp_path / "notes.md", tmp_path / "transforms" / "README.md")

    rc, _, err = _run(_client(FakeServer([_remote("a")])),
                      ["pull", "--workbook", WB, "--dir", str(tmp_path)], capsys)

    assert rc == 0, err
    assert (tmp_path / "transforms/a.sql").read_text(encoding="utf-8").strip() == T_SRC


# ── data: export = branch, push = 3-way merge ────────────────────────────────


def test_pull_data_branches_once_and_writes_canonical_csv(tmp_path, capsys):
    server = FakeServer(tables={"customers": _rows(("-A", "1"), ("B", "2"), ("C", "3"))})
    client = _pulled(server, tmp_path, capsys, "--data", "customers")
    f = tmp_path / "data/customers.csv"
    # Sorted by row id, LF, no BOM, values untouched (the ``-A`` defence is the server's).
    assert f.read_text(encoding="utf-8") == (
        "product,qty,__d2b_row_id\n-A,1,1\nB,2,2\nC,3,3\n"
    )
    entry = _manifest(tmp_path)["data"]["customers.csv"]
    assert entry["table"] == "customers" and entry["branch_id"] == "branch-1"
    assert entry["hash"] == digest(f.read_text(encoding="utf-8"))
    assert [c["record_branch"] for c in server.export_calls] == [True]

    # A later pull re-checks cheaply and keeps the branch when nothing moved.
    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path)], capsys)
    assert rc == 0 and out["data"]["unchanged"] == ["customers.csv"]
    assert [c["record_branch"] for c in server.export_calls] == [True, False]
    assert _manifest(tmp_path)["data"]["customers.csv"]["branch_id"] == "branch-1"

    # The server moved → a fresh branch (new base) and the file follows.
    server.tables["customers"][1]["qty"] = "20"
    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path)], capsys)
    assert rc == 0 and out["data"]["updated"] == ["customers.csv"]
    assert _manifest(tmp_path)["data"]["customers.csv"]["branch_id"] == "branch-2"
    assert "B,20,2" in f.read_text(encoding="utf-8")


def test_pull_data_error_keeps_the_entry(tmp_path, capsys):
    server = FakeServer(tables={"customers": _rows(("A", "1"))})
    client = _pulled(server, tmp_path, capsys, "--data", "customers")
    server.tables.pop("customers")           # the export now fails (500 from the fake)
    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert out["data"]["errors"][0]["table"] == "customers"
    assert "customers.csv" in _manifest(tmp_path)["data"]           # still tracked
    assert (tmp_path / "data/customers.csv").exists()


def test_push_data_merges_then_rebranches(tmp_path, capsys):
    server = FakeServer(tables={"customers": _rows(("A", "1"), ("B", "2"))})
    client = _pulled(server, tmp_path, capsys, "--data", "customers")
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0 and out["data"]["unchanged"] == ["customers.csv"] and server.merges == []

    f = tmp_path / "data/customers.csv"
    f.write_text(f.read_text(encoding="utf-8").replace("A,1,1", "A,10,1"), encoding="utf-8")
    server.tables["customers"][1]["qty"] = "22"                  # D2B-only change, kept
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert len(server.merges) == 1
    assert server.merges[0]["branch_id"] == "branch-1" and server.merges[0]["filename"] == "customers.csv"
    assert out["data"]["pushed"][0]["applied"] == {"updated": 1, "inserted": 0, "deleted": 0}
    assert out["data"]["pushed"][0]["conflicts"] == []
    # The file now reflects the merged server state and a fresh branch.
    assert f.read_text(encoding="utf-8") == "product,qty,__d2b_row_id\nA,10,1\nB,22,2\n"
    entry = _manifest(tmp_path)["data"]["customers.csv"]
    assert entry["branch_id"] == "branch-2" and entry["hash"] == digest(f.read_text(encoding="utf-8"))

    # Both sides changed the same cell → the server queues a conflict, D2B's value stays.
    f.write_text(f.read_text(encoding="utf-8").replace("B,22,2", "B,5,2"), encoding="utf-8")
    server.tables["customers"][1]["qty"] = "99"
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert out["data"]["pushed"][0]["conflicts"] == [{"row_id": "2", "column": "qty", "theirs": "5", "ours": "99"}]
    assert "B,99,2" in f.read_text(encoding="utf-8")


def test_execute_push_refuses_to_rebranch_through_a_symlink(tmp_path, capsys):
    """``plan_push`` screens the section, but the post-merge re-branch is a
    second visit to the disk — it must refuse a link swapped in meanwhile."""
    server = FakeServer(tables={"customers": _rows(("A", "1"))})
    client = _pulled(server, tmp_path, capsys, "--data", "customers")
    f = tmp_path / "data/customers.csv"
    f.write_text(f.read_text(encoding="utf-8").replace("A,1,1", "A,10,1"), encoding="utf-8")
    plan = plan_push(client, tmp_path, WB)

    victim = tmp_path / "victim.csv"
    victim.write_text("do not replace", encoding="utf-8")
    f.unlink()
    os.symlink(victim, f)

    with pytest.raises(SyncError, match="symbolic link"):
        execute_push(client, tmp_path, plan)
    assert victim.read_text(encoding="utf-8") == "do not replace"


def test_push_data_file_without_branch_is_refused(tmp_path, capsys):
    server = FakeServer([_remote("a")], tables={"customers": _rows(("A", "1"))})
    client = _pulled(server, tmp_path, capsys)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/customers.csv").write_text("product,qty,__d2b_row_id\nA,1,1\n", encoding="utf-8")
    rc, out, err = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 1 and out["data"]["missing_entry"] == ["customers.csv"]
    assert "d2b pull --data" in err and server.merges == []


# ── push: transforms ─────────────────────────────────────────────────────────


def test_push_sends_only_what_changed_and_normalises(tmp_path, capsys):
    server = FakeServer([_remote("a", layer="marts"), _remote("b")])
    client = _pulled(server, tmp_path, capsys)

    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0 and out["transforms"]["pushed"] == [] and out["transforms"]["unchanged"] == ["a.sql", "b.sql"]
    assert server.posts == []

    # CRLF + trailing newline from an editor are not a change of logic.
    (tmp_path / "transforms/a.sql").write_text(T_SRC + " LIMIT 1\r\n", encoding="utf-8")
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert server.posts == [{
        "name": "a", "kind": "sql", "template": T_SRC + " LIMIT 1",
        "artifact_name": "a", "args": {"src": "sales"}, "layer": "marts",
    }]
    assert out["transforms"]["pushed"] == [{
        "file": "a.sql", "name": "a", "kind": "sql", "artifacts": ["a"], "reason": "template changed",
    }]
    assert _manifest(tmp_path)["transforms"]["a.sql"]["hash"] == digest(T_SRC + " LIMIT 1")

    # In sync again: the next push is a no-op.
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0 and out["transforms"]["pushed"] == [] and len(server.posts) == 1


def test_push_refuses_transform_symlink(tmp_path, capsys):
    server = FakeServer([_remote("a")])
    client = _pulled(server, tmp_path, capsys)
    secret = tmp_path / "secret.txt"
    secret.write_text("LOCAL_SECRET", encoding="utf-8")
    transform = tmp_path / "transforms/a.sql"
    transform.unlink()
    os.symlink(secret, transform)

    rc, _, err = _run(client, ["push", "--dir", str(tmp_path)], capsys)

    assert rc == 1 and "symbolic link" in err
    assert server.posts == []


def test_push_binding_change_without_template_change(tmp_path, capsys):
    server = FakeServer([_remote("a")])
    client = _pulled(server, tmp_path, capsys)
    m = _manifest(tmp_path)
    m["transforms"]["a.sql"]["args"] = {"src": "sales_2026"}
    _write_manifest(tmp_path, m)
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert server.posts[0]["args"] == {"src": "sales_2026"}
    assert out["transforms"]["pushed"][0]["reason"] == "binding changed"


def test_push_refuses_when_server_moved_since_pull(tmp_path, capsys):
    server = FakeServer([_remote("a")])
    client = _pulled(server, tmp_path, capsys)
    (tmp_path / "transforms/a.sql").write_text(T_SRC + " LIMIT 1", encoding="utf-8")
    server.transforms = [_remote("a", T_SRC + " LIMIT 2")]   # the chat agent edited it meanwhile

    rc, out, err = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 1 and "push refused" in err
    assert out["conflicts"][0]["file"] == "transforms/a.sql" and server.posts == []

    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path), "--force"], capsys)
    assert rc == 0 and server.posts[0]["template"] == T_SRC + " LIMIT 1"


def test_push_new_file_needs_a_manifest_entry(tmp_path, capsys):
    server = FakeServer([_remote("a")])
    client = _pulled(server, tmp_path, capsys)
    (tmp_path / "transforms/new.sql").write_text(T_SRC, encoding="utf-8")

    rc, out, err = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 1 and out["transforms"]["missing_entry"] == ["new.sql"] and "artifact_name" in err
    assert server.posts == []

    m = _manifest(tmp_path)
    m["transforms"]["new.sql"] = {"artifact_name": "fresh", "args": {"src": "sales"}}
    _write_manifest(tmp_path, m)
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert server.posts[0]["name"] == "new" and server.posts[0]["artifact_name"] == "fresh"
    assert out["transforms"]["pushed"][0]["reason"] == "new"
    assert _manifest(tmp_path)["transforms"]["new.sql"]["hash"] == digest(T_SRC)


def test_push_orders_upstream_first(tmp_path, capsys):
    # ``a`` reads ``z``'s output; alphabetical order would push it first.
    server = FakeServer([
        _remote("z", artifact="z_out"),
        _remote("a", artifact="a_out", args={"src": "z_out"}),
    ])
    client = _pulled(server, tmp_path, capsys)
    for name in ("a", "z"):
        (tmp_path / f"transforms/{name}.sql").write_text(T_SRC + " LIMIT 1", encoding="utf-8")
    rc, _, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert [p["name"] for p in server.posts] == ["z", "a"]


def test_push_dry_run_sends_nothing(tmp_path, capsys):
    server = FakeServer([_remote("a")])
    client = _pulled(server, tmp_path, capsys)
    (tmp_path / "transforms/a.sql").write_text(T_SRC + " LIMIT 1", encoding="utf-8")
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path), "--dry-run"], capsys)
    assert rc == 0 and out["dry_run"] is True
    assert [i["file"] for i in out["transforms"]["to_push"]] == ["a.sql"] and server.posts == []
    assert _manifest(tmp_path)["transforms"]["a.sql"]["hash"] == digest(T_SRC)  # untouched


def test_push_commit_labels_a_version(tmp_path, capsys):
    server = FakeServer([_remote("a")])
    client = _pulled(server, tmp_path, capsys)
    (tmp_path / "transforms/a.sql").write_text(T_SRC + " LIMIT 1", encoding="utf-8")
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path), "--commit", "abc123"], capsys)
    assert rc == 0
    assert server.versions == [{"label": "abc123", "summary": "d2b push: 1 change(s)"}]
    assert out["version"]["label"] == "abc123"


def test_push_without_manifest_points_at_pull(tmp_path, capsys):
    rc, _, err = _run(_client(FakeServer()), ["push", "--workbook", WB, "--dir", str(tmp_path)], capsys)
    assert rc == 1 and "d2b pull" in err


def test_multi_output_template_round_trips(tmp_path, capsys):
    server = FakeServer([
        _remote("dedupe", artifact="x_clean", args={"src": "x"}),
        _remote("dedupe", artifact="y_clean", args={"src": "y"}),
    ])
    client = _pulled(server, tmp_path, capsys)
    entry = _manifest(tmp_path)["transforms"]["dedupe.sql"]
    assert entry["outputs"] == [
        {"artifact_name": "x_clean", "args": {"src": "x"}, "layer": None},
        {"artifact_name": "y_clean", "args": {"src": "y"}, "layer": None},
    ]
    (tmp_path / "transforms/dedupe.sql").write_text(T_SRC + " LIMIT 1", encoding="utf-8")
    rc, _, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert [p["artifact_name"] for p in server.posts] == ["x_clean", "y_clean"]


# ── push: sheets and charts ──────────────────────────────────────────────────


def test_push_sheets_put_changed_and_new(tmp_path, capsys):
    server = FakeServer(sheets=[{"name": "s", "spec": SUMMARY}])
    client = _pulled(server, tmp_path, capsys)
    changed = {"blocks": [{"kind": "heading", "text": "四半期"}]}
    (tmp_path / "sheets/s.json").write_text(json.dumps(changed), encoding="utf-8")
    (tmp_path / "sheets/new.json").write_text(json.dumps(SUMMARY, ensure_ascii=False), encoding="utf-8")
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0
    assert server.sheet_puts == [("new", SUMMARY), ("s", changed)]
    assert [p["reason"] for p in out["sheets"]["pushed"]] == ["new", "changed"]
    m = _manifest(tmp_path)["sheets"]
    assert m["s.json"]["hash"] == digest(canonical_json(changed))
    assert m["new.json"] == {"name": "new", "hash": digest(canonical_json(SUMMARY))}

    # A sheet the server changed since the pull is refused, like a transform.
    server.sheets["s"] = {"blocks": [{"kind": "text", "text": "server"}]}
    (tmp_path / "sheets/s.json").write_text(json.dumps({"blocks": [{"kind": "text", "text": "mine"}]}), encoding="utf-8")
    rc, out, err = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 1 and out["conflicts"][0]["file"] == "sheets/s.json"


def test_push_invalid_sheet_json_is_refused(tmp_path, capsys):
    server = FakeServer(sheets=[{"name": "s", "spec": SUMMARY}])
    client = _pulled(server, tmp_path, capsys)
    (tmp_path / "sheets/s.json").write_text("{not json", encoding="utf-8")
    rc, out, err = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 1 and "not valid JSON" in err and server.sheet_puts == []


def test_charts_are_read_only(tmp_path, capsys):
    server = FakeServer(charts=[CHART])
    client = _pulled(server, tmp_path, capsys)
    p = tmp_path / "charts/trend.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["title"] = "edited"
    p.write_text(json.dumps(doc), encoding="utf-8")
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 0 and out["charts"]["readonly_modified"] == ["trend.json"]
    # pull refuses to clobber the local edit (same rule), --force restores the server's copy
    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path)], capsys)
    assert rc == 1 and out["charts"]["conflicts"][0]["file"] == "trend.json"
    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path), "--force"], capsys)
    assert rc == 0 and json.loads(p.read_text(encoding="utf-8"))["title"] == "Trend"


# ── prune ────────────────────────────────────────────────────────────────────


def test_push_prune_deletes_outputs_and_sheets_but_only_untracks_data(tmp_path, capsys):
    server = FakeServer(
        [_remote("a", artifact="a_out"), _remote("b", artifact="b_out")],
        sheets=[{"name": "s", "spec": SUMMARY}, {"name": "gone", "spec": SUMMARY}],
        tables={"customers": _rows(("A", "1"))},
    )
    client = _pulled(server, tmp_path, capsys, "--data", "customers")
    (tmp_path / "transforms/b.sql").unlink()
    (tmp_path / "sheets/gone.json").unlink()
    (tmp_path / "data/customers.csv").unlink()

    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path)], capsys)      # without --prune: report only
    assert rc == 0 and out["transforms"]["deleted_locally"] == ["b.sql"]
    assert out["sheets"]["deleted_locally"] == ["gone.json"] and out["data"]["deleted_locally"] == ["customers.csv"]
    assert server.deleted == [] and "b.sql" in _manifest(tmp_path)["transforms"]

    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path), "--prune"], capsys)
    assert rc == 0
    assert server.deleted == ["artifact:b_out", "sheet:gone"]
    assert [p["name"] for p in out["prune"]["pruned"]] == ["b_out", "gone"] and out["prune"]["errors"] == []
    m = _manifest(tmp_path)
    assert "b.sql" not in m["transforms"] and "gone.json" not in m["sheets"]
    assert "data" not in m                                   # untracked, table untouched
    assert "customers" in server.tables


def test_push_prune_keeps_the_entry_when_the_server_refuses(tmp_path, capsys):
    server = FakeServer([_remote("b", artifact="b_out")])
    server.deny_delete.add("b_out")
    client = _pulled(server, tmp_path, capsys)
    (tmp_path / "transforms/b.sql").unlink()
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path), "--prune"], capsys)
    assert rc == 0
    assert out["prune"]["errors"][0]["name"] == "b_out" and "workbooks:delete" in out["prune"]["errors"][0]["error"]
    assert "b.sql" in _manifest(tmp_path)["transforms"]


# ── misc ─────────────────────────────────────────────────────────────────────


def test_label_to_path_mapping():
    assert path_for("agg/monthly", "sql") == "agg/monthly.sql"
    assert path_for("cf_runway", "python") == "cf_runway.py"
    assert path_for("summary", ".json") == "summary.json"
    assert path_for("../../evil", "sql") == "evil.sql"
    assert path_for("売上 集計 (v2)", "python") == "売上_集計__v2.py"   # server-style, no collapsing
    assert path_for("///", "sql") == "item.sql"


def test_csv_canonical_sorts_by_row_id_and_keeps_values():
    raw = "﻿product,qty,__d2b_row_id\r\nB,2,10\r\n'-A,1,2\r\n\r\nC,3,\r\n"
    assert csv_canonical(raw) == "product,qty,__d2b_row_id\n'-A,1,2\nB,2,10\nC,3,\n"


def test_github_workflow_prints_yaml(capsys):
    rc = main(["github-workflow"])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("name: d2b sync")
    # version-pinned invocations (a workbook-rewriting workflow must not float)
    assert 'uvx --from "d2b-sdk==${D2B_VERSION}" d2b push' in out
    assert 'uvx --from "d2b-sdk==${D2B_VERSION}" d2b pull' in out
    assert "concurrency" in out and "GITHUB_STEP_SUMMARY" in out
    # Dependencies are immutable and the workbook PAT is available only to
    # the two CLI steps, never to checkout, setup, git, or the PR action.
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in out
    assert "astral-sh/setup-uv@0c5e2b8115b80b4c7c5ddf6ffdd634974642d182" in out
    assert "peter-evans/create-pull-request@271a8d0340265f705b14b6d32b9829c1cb33d45e" in out
    # The exact pins above only stay honest while nothing floats back to a
    # tag — that regression is what the shape check catches, not a typo'd SHA.
    refs = re.findall(r"uses:\s*(\S+)", out)
    assert refs and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", r) for r in refs), refs
    assert out.count("D2B_API_KEY: ${{ secrets.D2B_API_KEY }}") == 2
    assert out.count("persist-credentials: false") == 2
    # The base64 credential the push step derives never reaches the log.
    assert "::add-mask::$auth" in out
    # This test's name is a promise, and a template that is not valid YAML
    # passes every string assertion above. Parse it, then pin the invariant
    # behind the deny-all default: no job runs on the repository's own
    # token permissions.
    wf = yaml.safe_load(out)
    assert wf["permissions"] == {}
    assert wf["jobs"]["push"]["permissions"] == {"contents": "write"}
    assert wf["jobs"]["pull"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
