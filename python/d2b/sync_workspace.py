"""``d2b pull --workspace`` / ``push`` / ``status`` — a whole workspace as a
repository, hundreds or thousands of workbooks at a time.

The rules (the design note lives with the repository's internal docs):

* One repository is one workspace. The root ``d2b.json`` (the *ledger*)
  carries ``workspace_id`` and the directory of every workbook that has
  been pulled. A different workspace is refused — there is no override.
* Membership is the server's fact: ``pull`` lists the workspace's
  workbooks and pulls each one into ``workbooks/<slug>--<id8>/``, where
  the per-workbook ``d2b.json`` and sections live exactly as in the
  single-workbook mode (``sync.py``). Nothing outside the ledger is ever
  written, and nothing outside it is ever pushed.
* A workbook's directory is fixed at its first pull (the ledger maps
  id -> dir); a retitled workbook keeps its directory. Its id is in the
  directory name and at the top of every transform file
  (``-- d2b ws=… wb=… transform=…``), so a file seen on its own — in a PR
  diff, in grep — still says where it belongs, and a file copied into the
  wrong workbook's directory is refused at push.
* ``status --strict`` is the guard for hand-made mistakes: directories
  the ledger does not know, ledger entries with no directory, a
  per-workbook manifest bound to another id, a header naming another
  workbook, workbooks gone from the workspace (stale) and workbooks the
  ledger has not pulled yet. Exit 2 under ``--strict``.
"""
from __future__ import annotations

import json
import re
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .sync import (
    MANIFEST,
    SyncError,
    _local_files,
    _manifest_present,
    _read_manifest,
    _write_manifest,
    execute_push,
    load_manifest,
    parse_header,
    plan_push,
    pull,
    strip_header,
)

WORKBOOKS_DIR = "workbooks"
_SLUG_UNSAFE = re.compile(r"[^\w\-]+", re.UNICODE)
_ID8 = 8


# ── ledger ────────────────────────────────────────────────────────────────────


def load_ledger(root: Path) -> dict[str, Any] | None:
    """The root ledger, or ``None`` when ``root`` is not a workspace
    repository (no ``d2b.json``, or a single-workbook manifest)."""
    p = root / MANIFEST
    raw = _read_manifest(root)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SyncError(f"{p}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict) or "workspace_id" not in data:
        return None
    if not isinstance(data.get("workbooks", {}), dict):
        raise SyncError(f"{p}: malformed ledger ('workbooks' must be an object)")
    data.setdefault("workbooks", {})
    for d, entry in data["workbooks"].items():
        if not _valid_dir(d) or not isinstance(entry, dict) or not entry.get("id"):
            raise SyncError(f"{p}: malformed ledger entry {d!r}")
    return data


def save_ledger(root: Path, ledger: dict[str, Any]) -> None:
    ordered = {
        "workspace_id": ledger["workspace_id"],
        "workspace_name": ledger.get("workspace_name"),
        "workbooks": dict(sorted(ledger.get("workbooks", {}).items())),
    }
    root.mkdir(parents=True, exist_ok=True)
    _write_manifest(root, json.dumps(ordered, ensure_ascii=False, indent=2) + "\n")


def resolve_workspace(ledger: dict[str, Any] | None, workspace_id: str | None) -> str:
    bound = (ledger or {}).get("workspace_id")
    if workspace_id and bound and workspace_id != bound:
        raise SyncError(
            f"this repository is bound to workspace {bound}; {workspace_id} would mix two "
            "workspaces in one repository — use another --dir for it.",
        )
    ws = workspace_id or bound
    if not ws:
        raise SyncError("--workspace is required the first time (afterwards it is read from d2b.json).")
    return ws


def _valid_dir(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")


def slug(title: str) -> str:
    s = _SLUG_UNSAFE.sub("-", title or "").strip("-")
    return s[:40].rstrip("-") or "workbook"


def dir_for(title: str, workbook_id: str, claimed: set[str]) -> str:
    """``<slug>--<id8>``, case-insensitively unique among ``claimed``."""
    base = f"{slug(title)}--{workbook_id[:_ID8]}"
    candidate, n = base, 2
    while candidate.casefold() in claimed:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _validate_workspace_path(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise SyncError(f"unsafe workspace path is a symbolic link: {path}")
    if not stat.S_ISDIR(mode):
        raise SyncError(f"unsafe workspace path is not a directory: {path}")


def workbook_root(root: Path, d: str) -> Path:
    if not _valid_dir(d):
        raise SyncError(f"unsafe workbook directory name {d!r}")
    base = root / WORKBOOKS_DIR
    wb_root = base / d
    for path in (base, wb_root):
        _validate_workspace_path(path)
    return wb_root


# ── identity headers ──────────────────────────────────────────────────────────


def header_line(kind_ext: str, workspace_id: str, workbook_id: str, name: str) -> str:
    prefix = "#" if kind_ext == ".py" else "--"
    return f"{prefix} d2b ws={workspace_id} wb={workbook_id} transform={name}\n"


def stamp_headers(wb_root: Path, workspace_id: str, workbook_id: str) -> None:
    """Put the identity line at the top of every tracked transform file
    (idempotent: the reader strips it, so re-stamping replaces it)."""
    manifest = load_manifest(wb_root) or {}
    for rel, entry in manifest.get("transforms", {}).items():
        p = wb_root / "transforms" / rel
        if not p.is_file() or p.is_symlink():
            continue
        body = strip_header(p.read_text(encoding="utf-8"))
        name = str(entry.get("name") or rel.rsplit(".", 1)[0])
        p.write_text(header_line(p.suffix, workspace_id, workbook_id, name) + body, encoding="utf-8")


def header_mismatches(wb_root: Path, workspace_id: str, workbook_id: str) -> list[dict[str, str]]:
    """Transform files whose header names another workspace or workbook."""
    out: list[dict[str, str]] = []
    base = wb_root / "transforms"
    if not base.exists():
        return out
    for p in sorted(base.rglob("*")):
        if p.suffix not in (".sql", ".py") or not p.is_file() or p.is_symlink():
            continue
        fields = parse_header(p.read_text(encoding="utf-8"))
        if not fields:
            continue
        if fields.get("wb", workbook_id) != workbook_id or fields.get("ws", workspace_id) != workspace_id:
            out.append({
                "file": p.relative_to(wb_root).as_posix(),
                "header": f"ws={fields.get('ws')} wb={fields.get('wb')}",
            })
    return out


# ── pull ──────────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_workspace_workbooks(client: Any, workspace_id: str) -> list[dict[str, Any]]:
    """Every workbook of the workspace (the client follows the cursor). A
    workspace the credential cannot reach is the server's 403, verbatim."""
    rows = client.workbooks.list(workspace_id=workspace_id)
    out = []
    for r in rows:
        if r.get("workspace_id") not in (None, workspace_id):
            # Defensive: the server filtered by workspace; a stray row from
            # another workspace must never become a directory here.
            continue
        out.append({"id": str(r["id"]), "title": str(r.get("title") or "")})
    return out


def _workspace_name(client: Any, workspace_id: str) -> str | None:
    try:
        for w in client.workspaces.list():
            if w.get("id") == workspace_id:
                return w.get("name")
    except Exception:  # the name is decoration; the id is the binding
        return None
    return None


def pull_workspace(
    client: Any,
    root: Path,
    workspace_id: str | None = None,
    *,
    force: bool = False,
    prune: bool = False,
    jobs: int = 6,
) -> dict[str, Any]:
    """Every workbook of the workspace into ``workbooks/<dir>/``. One
    workbook's conflict refuses that workbook only; the others land and
    the report says which were refused (exit 1 in the CLI)."""
    ledger = load_ledger(root)
    if ledger is None and _manifest_present(root):
        raise SyncError(
            f"{root / MANIFEST} is a single-workbook manifest — a workspace repository needs "
            "its own directory (use --dir).",
        )
    ws = resolve_workspace(ledger, workspace_id)
    ledger = ledger or {"workspace_id": ws, "workspace_name": None, "workbooks": {}}
    ledger["workspace_name"] = _workspace_name(client, ws) or ledger.get("workspace_name")

    remote = list_workspace_workbooks(client, ws)
    entries: dict[str, dict[str, Any]] = ledger["workbooks"]
    dir_by_id = {e["id"]: d for d, e in entries.items()}
    claimed = {d.casefold() for d in entries}

    targets: list[tuple[str, dict[str, Any]]] = []
    for wbk in remote:
        d = dir_by_id.get(wbk["id"])
        if d is None:
            d = dir_for(wbk["title"], wbk["id"], claimed)
            claimed.add(d.casefold())
            entries[d] = {"id": wbk["id"], "title": wbk["title"]}
        entries[d]["title"] = wbk["title"]
        targets.append((d, wbk))
    remote_ids = {w["id"] for w in remote}
    stale = sorted(d for d, e in entries.items() if e["id"] not in remote_ids)

    # Validate every ledger-derived path before starting any worker. A bad
    # repository must not allow some workbooks to land before another path is
    # found to escape through a symlink.
    for d, _ in targets:
        workbook_root(root, d)

    def one(d: str, wbk: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        wb_root = workbook_root(root, d)
        try:
            res = pull(client, wb_root, wbk["id"], force=force)
            stamp_headers(wb_root, ws, wbk["id"])
            entries[d]["pulled_at"] = _now()
            return d, {"status": "ok", "id": wbk["id"], "title": wbk["title"], "sections": res}
        except SyncError as exc:
            return d, {"status": "refused", "id": wbk["id"], "title": wbk["title"],
                       "error": str(exc), "details": exc.details}
        except Exception as exc:  # the API's problem+json travels in str(exc)
            return d, {"status": "error", "id": wbk["id"], "title": wbk["title"], "error": str(exc)}

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for d, res in pool.map(lambda t: one(*t), targets):
            results[d] = res

    pruned: list[str] = []
    for d in stale:
        if prune:
            _remove_tree(workbook_root(root, d))
            entries.pop(d, None)
            pruned.append(d)
    save_ledger(root, ledger)

    refused = sorted(d for d, r in results.items() if r["status"] != "ok")
    return {
        "workspace_id": ws,
        "workspace_name": ledger.get("workspace_name"),
        "workbooks": results,
        "counts": {
            "total": len(targets),
            "ok": len(targets) - len(refused),
            "refused": len(refused),
            "stale": len([d for d in stale if d not in pruned]),
            "pruned": len(pruned),
        },
        "stale": [d for d in stale if d not in pruned],
        "pruned": pruned,
    }


def _remove_tree(p: Path) -> None:
    if not p.exists():
        return
    if p.is_symlink():
        raise SyncError(f"refusing to prune a symbolic link: {p}")
    for child in sorted(p.rglob("*"), reverse=True):
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    p.rmdir()


# ── push ──────────────────────────────────────────────────────────────────────


def _unlisted_dirs(root: Path, ledger: dict[str, Any]) -> list[str]:
    base = root / WORKBOOKS_DIR
    # Validate the shared parent even when the ledger is empty.
    _validate_workspace_path(base)
    if not base.exists():
        return []
    known = {d.casefold() for d in ledger["workbooks"]}
    return sorted(p.name for p in base.iterdir() if p.is_dir() and p.name.casefold() not in known)


def push_workspace(
    client: Any,
    root: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
    prune: bool = False,
    commit: str | None = None,
) -> dict[str, Any]:
    """Every ledger workbook, one after another; only the changed ones send
    anything. Refused before any request when the repository holds a
    directory the ledger does not know, or a file whose header names
    another workbook — the ledger is the only thing that may be pushed."""
    ledger = load_ledger(root)
    if ledger is None:
        raise SyncError(f"no workspace ledger in {root} — run `d2b pull --workspace WS` first.")
    ws = ledger["workspace_id"]
    problems: list[str] = []
    unlisted = _unlisted_dirs(root, ledger)
    if unlisted:
        problems.append("directories the ledger does not know: " + ", ".join(unlisted)
                        + " (a workbook joins the repository through `d2b pull`, never by hand)")
    for d, entry in ledger["workbooks"].items():
        wb_root = workbook_root(root, d)
        if not wb_root.exists():
            continue
        manifest = load_manifest(wb_root)
        if manifest and manifest.get("workbook_id") not in (None, entry["id"]):
            problems.append(f"{WORKBOOKS_DIR}/{d}/{MANIFEST} is bound to {manifest['workbook_id']}, "
                            f"the ledger says {entry['id']}")
        for mm in header_mismatches(wb_root, ws, entry["id"]):
            problems.append(f"{WORKBOOKS_DIR}/{d}/{mm['file']}: header says {mm['header']}")
    if problems:
        raise SyncError("push refused: " + "; ".join(problems), {"problems": problems})

    results: dict[str, dict[str, Any]] = {}
    for d, entry in sorted(ledger["workbooks"].items()):
        wb_root = workbook_root(root, d)
        if not _manifest_present(wb_root):
            results[d] = {"status": "skipped", "id": entry["id"], "reason": "not pulled here"}
            continue
        try:
            plan = plan_push(client, wb_root, entry["id"], force=force, prune=prune)
        except SyncError as exc:
            results[d] = {"status": "refused", "id": entry["id"], "error": str(exc), "details": exc.details}
            continue
        changed = bool(plan.transforms or plan.sheets or plan.data or plan.prune_artifacts or plan.prune_sheets)
        if not changed:
            results[d] = {"status": "unchanged", "id": entry["id"]}
            continue
        if dry_run:
            results[d] = {"status": "would_push", "id": entry["id"], "plan": plan.as_dict()}
            continue
        try:
            pushed = execute_push(client, wb_root, plan)
        except Exception as exc:
            results[d] = {"status": "error", "id": entry["id"], "error": str(exc)}
            continue
        out: dict[str, Any] = {"status": "pushed", "id": entry["id"], "pushed": {
            s: pushed[s] for s in ("transforms", "sheets", "data")}}
        if prune:
            out["prune"] = {"pruned": pushed["pruned"], "errors": pushed["prune_errors"]}
        if commit:
            n = sum(len(pushed[s]) for s in ("transforms", "sheets", "data"))
            out["version"] = client.versions.commit(entry["id"], commit, summary=f"d2b push: {n} change(s)")
        stamp_headers(wb_root, ws, entry["id"])
        results[d] = out
    counts = {k: sum(1 for r in results.values() if r["status"] == k)
              for k in ("pushed", "would_push", "unchanged", "refused", "error", "skipped")}
    return {"workspace_id": ws, "dry_run": dry_run, "workbooks": results, "counts": counts}


# ── status ────────────────────────────────────────────────────────────────────


def status_workspace(client: Any, root: Path, *, offline: bool = False) -> dict[str, Any]:
    """What is wrong, if anything. Each list is a reason for ``--strict`` to
    exit 2; an empty report is a repository that holds exactly the
    workspace's workbooks and nothing else."""
    ledger = load_ledger(root)
    if ledger is None:
        raise SyncError(f"no workspace ledger in {root} — run `d2b pull --workspace WS` first.")
    ws = ledger["workspace_id"]
    report: dict[str, Any] = {
        "workspace_id": ws,
        "workspace_name": ledger.get("workspace_name"),
        "unlisted_dirs": _unlisted_dirs(root, ledger),
        "missing_dirs": [],
        "bound_elsewhere": [],
        "header_mismatches": [],
        "stale": [],
        "not_pulled": [],
        "local_changes": [],
    }
    for d, entry in sorted(ledger["workbooks"].items()):
        wb_root = workbook_root(root, d)
        if not wb_root.exists():
            report["missing_dirs"].append(d)
            continue
        manifest = load_manifest(wb_root)
        if manifest and manifest.get("workbook_id") not in (None, entry["id"]):
            report["bound_elsewhere"].append({"dir": d, "manifest": manifest["workbook_id"], "ledger": entry["id"]})
        for mm in header_mismatches(wb_root, ws, entry["id"]):
            report["header_mismatches"].append({"dir": d, **mm})
        if manifest:
            changed = _changed_files(wb_root, manifest)
            if changed:
                report["local_changes"].append({"dir": d, "files": changed})
    if not offline:
        remote = {w["id"]: w["title"] for w in list_workspace_workbooks(client, ws)}
        ids = {e["id"] for e in ledger["workbooks"].values()}
        report["stale"] = sorted(d for d, e in ledger["workbooks"].items() if e["id"] not in remote)
        report["not_pulled"] = sorted(
            ({"id": i, "title": t} for i, t in remote.items() if i not in ids),
            key=lambda x: x["id"],
        )
    report["clean"] = not any(report[k] for k in (
        "unlisted_dirs", "missing_dirs", "bound_elsewhere", "header_mismatches", "stale", "not_pulled",
    ))
    return report


def _changed_files(wb_root: Path, manifest: dict[str, Any]) -> list[str]:
    """Files whose content differs from the last sync (informational)."""
    from .sync import _CANON, _SUFFIXES, digest
    out: list[str] = []
    for section in ("transforms", "sheets", "data"):
        local = _local_files(wb_root, section, _SUFFIXES[section])
        for rel, raw in local.items():
            entry = manifest.get(section, {}).get(rel)
            if entry is None:
                out.append(f"{section}/{rel} (untracked)")
            elif entry.get("hash") and digest(_CANON[section](raw)) != entry["hash"]:
                out.append(f"{section}/{rel}")
    return out
