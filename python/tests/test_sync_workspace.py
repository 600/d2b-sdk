"""``d2b pull --workspace`` / ``push`` / ``status`` against a fake server
that holds several workbooks in one workspace.

Pins the workspace-repository rules: every
workbook of the workspace lands in ``workbooks/<slug>--<id8>/``; a
directory is fixed at the first pull (a retitle keeps it); another
workspace is refused; a workbook gone from the workspace is stale and
``--prune`` removes it; push touches only changed workbooks and refuses a
directory the ledger does not know or a file whose header names another
workbook; ``status --strict`` exits 2 on any of those.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from d2b import D2BClient
from d2b.cli import main
from d2b.sync import parse_header, strip_header
from d2b.sync_workspace import dir_for, slug
from test_sync import T_SRC, FakeServer

WS = "ws-east"
WB_A = "3f9a1c2d-0000-4000-8000-000000000001"
WB_B = "9b02de11-0000-4000-8000-000000000002"


class FakeWorkspace:
    """One FakeServer per workbook, plus the listing endpoints."""

    def __init__(self, workspace_id: str, workbooks: dict[str, tuple[str, FakeServer]]):
        self.workspace_id = workspace_id
        self.workbooks = dict(workbooks)          # id -> (title, server)
        self.list_calls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, m = request.url.path, request.method
        if m == "GET" and path == "/api/v1/me/workspaces":
            return httpx.Response(200, json={"workspaces": [
                {"id": self.workspace_id, "name": "Sales East", "kind": "team", "is_default": False},
            ], "total": 1})
        if m == "GET" and path == "/api/v1/me/workbooks":
            self.list_calls += 1
            ws = request.url.params.get("workspace_id")
            if ws != self.workspace_id:
                return httpx.Response(403, json={"title": "Forbidden", "detail": "workspace out of reach"})
            rows = [{"id": i, "title": t, "workspace_id": ws, "created_at": "2026-09-01T00:00:00Z"}
                    for i, (t, _) in self.workbooks.items()]
            # Two pages, to prove the client follows the cursor.
            cursor = request.url.params.get("cursor")
            if cursor is None and len(rows) > 1:
                return httpx.Response(200, json={"workbooks": rows[:1], "total": len(rows), "next_cursor": "p2"})
            page = rows[1:] if cursor == "p2" else rows
            return httpx.Response(200, json={"workbooks": page, "total": len(rows), "next_cursor": None})
        for wb_id, (_, server) in self.workbooks.items():
            prefix = f"/api/v1/workbooks/{wb_id}"
            if path.startswith(prefix):
                # FakeServer answers for WB="wb-1"; rebase the path onto it.
                rebased = request.url.copy_with(path="/api/v1/workbooks/wb-1" + path[len(prefix):])
                req = httpx.Request(m, rebased, headers=request.headers, content=request.read())
                return server.handle(req)
        return httpx.Response(404, json={"detail": f"unhandled {m} {path}"})


def _client(ws: FakeWorkspace) -> D2BClient:
    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return ws.handle(request)

    http = httpx.Client(base_url="https://api.example.test", transport=Transport())
    return D2BClient(api_key="d2b_pat_test", http_client=http)


def _run(client, argv, capsys):
    try:
        rc = main(argv, client=client)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    return rc, out, captured.err


def _transform(name="agg/monthly", artifact="out"):
    return {"name": name, "kind": "sql", "template": T_SRC, "artifact_name": artifact,
            "args": {"src": "sales"}, "layer": None}


def _workspace():
    a = FakeServer(transforms=[_transform()], sheets=[{"name": "summary", "spec": {"blocks": []}}])
    b = FakeServer(transforms=[_transform("clean")])
    return FakeWorkspace(WS, {WB_A: ("月次レポート", a), WB_B: ("Supplier master", b)})


def _ledger(root: Path) -> dict:
    return json.loads((root / "d2b.json").read_text())


def test_slug_and_dir_naming():
    assert slug("月次レポート 2026/09 (v2)") == "月次レポート-2026-09-v2"
    assert slug("   ") == "workbook"
    assert dir_for("Sales", WB_A, set()) == "Sales--3f9a1c2d"
    assert dir_for("sales", WB_A, {"sales--3f9a1c2d"}) == "sales--3f9a1c2d-2"


def test_first_pull_lands_every_workbook_with_the_ledger(tmp_path, capsys):
    ws = _workspace()
    rc, out, err = _run(_client(ws), ["pull", "--workspace", WS, "--dir", str(tmp_path)], capsys)
    assert rc == 0, err
    assert out["counts"] == {"total": 2, "ok": 2, "refused": 0, "stale": 0, "pruned": 0}
    ledger = _ledger(tmp_path)
    assert ledger["workspace_id"] == WS and ledger["workspace_name"] == "Sales East"
    dirs = set(ledger["workbooks"])
    assert dirs == {"月次レポート--3f9a1c2d", "Supplier-master--9b02de11"}
    assert ws.list_calls == 2  # two pages, both fetched
    a = tmp_path / "workbooks" / "月次レポート--3f9a1c2d"
    assert json.loads((a / "d2b.json").read_text())["workbook_id"] == WB_A
    sql = (a / "transforms" / "agg" / "monthly.sql").read_text()
    assert sql.splitlines()[0] == f"-- d2b ws={WS} wb={WB_A} transform=agg/monthly"
    assert parse_header(sql) == {"ws": WS, "wb": WB_A, "transform": "agg/monthly"}
    assert strip_header(sql).strip() == T_SRC
    assert (a / "sheets" / "summary.json").exists()


def test_second_pull_is_unchanged_and_a_retitle_keeps_the_directory(tmp_path, capsys):
    ws = _workspace()
    client = _client(ws)
    _run(client, ["pull", "--workspace", WS, "--dir", str(tmp_path)], capsys)
    ws.workbooks[WB_A] = ("月次レポート(改)", ws.workbooks[WB_A][1])
    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path)], capsys)   # workspace read from the ledger
    assert rc == 0
    res = out["workbooks"]["月次レポート--3f9a1c2d"]
    assert res["status"] == "ok" and res["title"] == "月次レポート(改)"
    assert res["sections"]["transforms"]["unchanged"] == ["agg/monthly.sql"]
    assert _ledger(tmp_path)["workbooks"]["月次レポート--3f9a1c2d"]["title"] == "月次レポート(改)"
    assert (tmp_path / "workbooks" / "月次レポート--3f9a1c2d").exists()


def test_another_workspace_is_refused_without_an_override(tmp_path, capsys):
    ws = _workspace()
    client = _client(ws)
    _run(client, ["pull", "--workspace", WS, "--dir", str(tmp_path)], capsys)
    rc, _, err = _run(client, ["pull", "--workspace", "ws-west", "--dir", str(tmp_path)], capsys)
    assert rc == 1
    assert "bound to workspace ws-east" in err and "mix two workspaces" in err
    # And a workspace the credential cannot reach is the server's 403, verbatim.
    rc, _, err = _run(client, ["pull", "--workspace", "ws-west", "--dir", str(tmp_path / "other")], capsys)
    assert rc == 1 and "403" in err or "Forbidden" in err or "out of reach" in err


def test_a_workbook_that_left_the_workspace_is_stale_until_pruned(tmp_path, capsys):
    ws = _workspace()
    client = _client(ws)
    _run(client, ["pull", "--workspace", WS, "--dir", str(tmp_path)], capsys)
    del ws.workbooks[WB_B]
    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path)], capsys)
    assert rc == 0 and out["stale"] == ["Supplier-master--9b02de11"]
    assert (tmp_path / "workbooks" / "Supplier-master--9b02de11").exists()
    rc, out, _ = _run(client, ["status", "--dir", str(tmp_path), "--strict"], capsys)
    assert rc == 2 and out["stale"] == ["Supplier-master--9b02de11"] and out["clean"] is False
    rc, out, _ = _run(client, ["pull", "--dir", str(tmp_path), "--prune"], capsys)
    assert rc == 0 and out["pruned"] == ["Supplier-master--9b02de11"]
    assert not (tmp_path / "workbooks" / "Supplier-master--9b02de11").exists()
    assert "Supplier-master--9b02de11" not in _ledger(tmp_path)["workbooks"]
    rc, out, _ = _run(client, ["status", "--dir", str(tmp_path), "--strict"], capsys)
    assert rc == 0 and out["clean"] is True


def test_push_touches_only_the_changed_workbook(tmp_path, capsys):
    ws = _workspace()
    client = _client(ws)
    _run(client, ["pull", "--workspace", WS, "--dir", str(tmp_path)], capsys)
    a_sql = tmp_path / "workbooks" / "月次レポート--3f9a1c2d" / "transforms" / "agg" / "monthly.sql"
    a_sql.write_text(a_sql.read_text().replace('FROM "{{ src }}"', 'FROM "{{ src }}" WHERE qty > 0'))
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path), "--dry-run"], capsys)
    assert rc == 0
    assert out["workbooks"]["月次レポート--3f9a1c2d"]["status"] == "would_push"
    assert out["workbooks"]["Supplier-master--9b02de11"]["status"] == "unchanged"
    rc, out, _ = _run(client, ["push", "--dir", str(tmp_path), "--commit", "abc123"], capsys)
    assert rc == 0 and out["counts"]["pushed"] == 1 and out["counts"]["unchanged"] == 1
    posted = ws.workbooks[WB_A][1].posts
    assert len(posted) == 1 and "WHERE qty > 0" in posted[0]["template"]
    assert "-- d2b" not in posted[0]["template"]            # the header never reaches the server
    assert ws.workbooks[WB_B][1].posts == []
    assert ws.workbooks[WB_A][1].versions and ws.workbooks[WB_B][1].versions == []
    assert a_sql.read_text().startswith(f"-- d2b ws={WS} wb={WB_A}")   # re-stamped after the push


def test_push_refuses_unknown_directories_and_foreign_headers(tmp_path, capsys):
    ws = _workspace()
    client = _client(ws)
    _run(client, ["pull", "--workspace", WS, "--dir", str(tmp_path)], capsys)
    stray = tmp_path / "workbooks" / "scratch--deadbeef"
    stray.mkdir()
    rc, out, err = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 1 and "scratch--deadbeef" in err and "never by hand" in err
    stray.rmdir()
    # A transform copied from workbook A into B's directory keeps A's header.
    b_dir = tmp_path / "workbooks" / "Supplier-master--9b02de11" / "transforms"
    (b_dir / "copied.sql").write_text((tmp_path / "workbooks" / "月次レポート--3f9a1c2d" / "transforms" / "agg" / "monthly.sql").read_text())
    rc, out, err = _run(client, ["push", "--dir", str(tmp_path)], capsys)
    assert rc == 1 and "copied.sql" in err and f"wb={WB_A}" in err
    rc, out, _ = _run(client, ["status", "--dir", str(tmp_path), "--strict", "--offline"], capsys)
    assert rc == 2 and out["header_mismatches"][0]["file"] == "transforms/copied.sql"


@pytest.mark.parametrize("dangling", [False, True])
def test_workspace_pull_refuses_to_write_through_a_ledger_symlink(tmp_path, capsys, dangling):
    """The root ledger is a ``d2b.json`` too, and a workspace pull always
    saves it. A dangling link is the worse half: ``exists()`` reports it
    absent, so the pull would read the repository as never pulled and then
    create the link's target outside it."""
    root = tmp_path / "repo"
    root.mkdir()
    victim = tmp_path / "outside.json"
    kept = None if dangling else "do not replace"
    if kept is not None:
        victim.write_text(kept, encoding="utf-8")
    (root / "d2b.json").symlink_to("../outside.json")

    argv = ["pull", "--workspace", WS, "--dir", str(root)]
    rc, _, err = _run(_client(_workspace()), argv, capsys)

    assert rc == 1 and "refusing symbolic link for d2b.json" in err
    assert (victim.read_text(encoding="utf-8") if victim.exists() else None) == kept


@pytest.mark.parametrize("link_workbooks_dir", [False, True])
@pytest.mark.parametrize("command", ["pull", "push", "status"])
def test_workspace_commands_refuse_symlinked_path_ancestors(
    tmp_path, capsys, link_workbooks_dir, command,
):
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    directory = "Sales--3f9a1c2d"
    (root / "d2b.json").write_text(json.dumps({
        "workspace_id": WS,
        "workbooks": {directory: {"id": WB_A, "title": "Sales"}},
    }))
    if link_workbooks_dir:
        (root / "workbooks").symlink_to(outside, target_is_directory=True)
        (outside / directory).mkdir()
    else:
        (root / "workbooks").mkdir()
        (root / "workbooks" / directory).symlink_to(outside, target_is_directory=True)
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched")
    outside_entries = sorted(p.relative_to(outside) for p in outside.rglob("*"))

    argv = [command, "--dir", str(root)]
    if command == "pull":
        argv.extend(["--workspace", WS])
    elif command == "status":
        argv.append("--offline")
    rc, _, err = _run(_client(_workspace()), argv, capsys)

    assert rc == 1
    assert "symbolic link" in err
    assert sentinel.read_text() == "untouched"
    assert sorted(p.relative_to(outside) for p in outside.rglob("*")) == outside_entries


@pytest.mark.parametrize("non_dir_workbooks_dir", [False, True])
@pytest.mark.parametrize("command", ["pull", "push", "status"])
def test_workspace_commands_refuse_non_directory_path_ancestors(
    tmp_path, capsys, non_dir_workbooks_dir, command,
):
    root = tmp_path / "repo"
    root.mkdir()
    directory = "Sales--3f9a1c2d"
    (root / "d2b.json").write_text(json.dumps({
        "workspace_id": WS,
        "workbooks": {directory: {"id": WB_A, "title": "Sales"}},
    }))
    if non_dir_workbooks_dir:
        (root / "workbooks").write_text("not a directory")
    else:
        (root / "workbooks").mkdir()
        (root / "workbooks" / directory).write_text("not a directory")

    argv = [command, "--dir", str(root)]
    if command == "pull":
        argv.extend(["--workspace", WS])
    elif command == "status":
        argv.append("--offline")
    rc, _, err = _run(_client(_workspace()), argv, capsys)

    assert rc == 1
    assert "not a directory" in err


def test_single_workbook_mode_still_works_and_the_two_do_not_mix(tmp_path, capsys):
    ws = _workspace()
    client = _client(ws)
    one = tmp_path / "one"
    rc, out, _ = _run(client, ["pull", "--workbook", WB_A, "--dir", str(one)], capsys)
    assert rc == 0 and out["workbook_id"] == WB_A
    assert "workspace_id" not in json.loads((one / "d2b.json").read_text())
    rc, _, err = _run(client, ["pull", "--workspace", WS, "--dir", str(one)], capsys)
    assert rc == 1 and "single-workbook manifest" in err
    rc, _, err = _run(client, ["pull", "--workspace", WS, "--workbook", WB_A, "--dir", str(tmp_path / "x")], capsys)
    assert rc == 1 and "do not combine" in err


@pytest.mark.parametrize("argv", [["status"], ["push"]])
def test_workspace_commands_need_a_ledger(tmp_path, capsys, argv):
    rc, _, err = _run(_client(_workspace()), [*argv, "--dir", str(tmp_path)], capsys)
    assert rc == 1 and ("ledger" in err or "d2b.json" in err)
