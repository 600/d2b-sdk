"""``d2b pull`` / ``d2b push`` — a workbook as files in your repository.

D2B already versions a workbook server-side (content-addressed transform
files, snapshots, named versions, an op log, export=branch / merge). What
this module adds is the copy in *your* repository: files you can
``git diff``, review in a pull request, and push back. It owns only the
sync mechanics — D2B stays the executor (a push re-runs a transform or
3-way-merges rows; lineage, snapshots and conflicts are recorded there),
git stays the reviewer.

Layout (``DIR`` defaults to the current directory)::

    DIR/
      d2b.json                 # manifest: workbook + per-file sync metadata
      transforms/              # SQL / Python transforms   (pull + push)
        agg/monthly.sql
        cf_runway.py
      sheets/                  # presentation sheets, {"blocks": […]}  (pull + push)
        summary.json
      charts/                  # chart config + recipe     (pull only — read-only)
        sales_trend.json
      data/                    # opted-in base tables as CSV (pull = branch, push = 3-way merge)
        customers.csv

``d2b.json``::

    {
      "workbook_id": "…",
      "transforms": {
        "agg/monthly.sql": {
          "name": "agg/monthly",          # transform label (defaults to the path)
          "artifact_name": "商品別売上",   # the output — POST /transforms shape
          "args": {"src": "売上明細"},      # {{ arg }} → input table
          "layer": null,
          "hash": "…"                      # digest at the last sync (the merge base)
        }
      },
      "sheets":  {"summary.json":     {"name": "summary", "hash": "…"}},
      "charts":  {"sales_trend.json": {"name": "sales_trend", "readonly": true, "hash": "…"}},
      "data":    {"customers.csv":    {"table": "customers", "branch_id": "…", "hash": "…"}}
    }

A template applied to several outputs carries ``"outputs": [{artifact_name,
args, layer}, …]`` instead of the flat trio.

Conflict rule for text sections, both directions (the same contract as
the row API's optimistic lock — never overwrite silently): when both
sides moved since the last sync the operation is refused with the file
list, and ``--force`` takes the caller's side. Data is different by
design: a pushed CSV goes through the server's row-id-keyed 3-way merge
(``export = branch``), so concurrent edits are arbitrated per cell and
the both-changed cells land in the workbook's conflict queue instead of
being refused here.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

MANIFEST = "d2b.json"
SECTIONS = ("transforms", "sheets", "charts", "data")
ROW_ID_COL = "__d2b_row_id"

# The identity line a workspace-level pull writes at the top of a transform
# file (``-- d2b ws=… wb=… transform=…`` / ``# d2b …``): read back, it is
# not content — the server never sees it and it never counts as a change.
HEADER_RE = re.compile(r"^(?:--|#) d2b (?P<fields>(?:\w+=\S+ ?)+)\n?")


def strip_header(text: str) -> str:
    return HEADER_RE.sub("", text, count=1)


def parse_header(text: str) -> dict[str, str]:
    """The header's ``key=value`` pairs, or ``{}`` when there is none."""
    m = HEADER_RE.match(text)
    if not m:
        return {}
    return dict(f.split("=", 1) for f in m.group("fields").split())

_EXT = {"sql": ".sql", "python": ".py"}
_KIND_BY_EXT = {".sql": "sql", ".py": "python"}
_CLEAN_SEGMENT = re.compile(r"^\w+$")
_UNSAFE = re.compile(r"[^\w]")


class SyncError(Exception):
    """A refused sync. ``message`` is the human line (stderr); ``details``
    is the machine-readable report (stdout) so an agent can act on it."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


# ── normal forms ──────────────────────────────────────────────────────────────


def normalize(text: str) -> str:
    """The form that is hashed and sent: CRLF folded, outer whitespace
    stripped — the server strips too, so a trailing newline an editor adds
    never counts as a change."""
    return text.replace("\r\n", "\n").strip()


def digest(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)


def _canon_json_text(raw: str) -> str:
    """A local JSON file in its canonical form — formatting is not a change."""
    try:
        return canonical_json(json.loads(raw))
    except ValueError:
        return normalize(raw)


def csv_canonical(text: str) -> str:
    """A CSV in its canonical form: rows sorted by row-id, LF line endings,
    minimal quoting, no BOM — stable ``git diff`` whatever order the server
    or a spreadsheet wrote the rows in. Cell values are untouched."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return ""
    header, body = rows[0], [r for r in rows[1:] if any(c != "" for c in r)]
    if ROW_ID_COL in header:
        i = header.index(ROW_ID_COL)

        def key(r: list[str]) -> tuple[int, int]:
            try:
                return (0, int(r[i]))
            except (IndexError, ValueError):
                return (1, 0)

        body.sort(key=key)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(body)
    return out.getvalue()


def _canon_csv_text(raw: str) -> str:
    try:
        return csv_canonical(raw)
    except csv.Error:
        return normalize(raw)


def path_for(name: str, kind_or_ext: str) -> str:
    """Label → relative POSIX path under the section directory.

    ``agg/monthly`` nests (``agg/monthly.sql``) when every segment is a
    plain word (any script); anything else collapses to a single
    sanitised segment the way the server derives its own file stems
    (``../../evil`` → ``evil.sql``), so a label can never escape the dir.
    """
    ext = _EXT.get(kind_or_ext, kind_or_ext)
    segments = [s for s in name.split("/") if s]
    if segments and all(_CLEAN_SEGMENT.fullmatch(s) for s in segments):
        return "/".join(segments) + ext
    stem = _UNSAFE.sub("_", name).strip("_") or "item"
    return stem[:80] + ext


def _name_from_path(rel: str) -> str:
    return rel.rsplit(".", 1)[0]


def _kind_from_path(rel: str) -> str | None:
    return _KIND_BY_EXT.get(Path(rel).suffix)


def _unique_path(candidate: str, claimed: set[str]) -> str:
    """Case-insensitive uniqueness (macOS / Windows file systems)."""
    if candidate.casefold() not in claimed:
        return candidate
    stem, ext = candidate.rsplit(".", 1)
    n = 2
    while f"{stem}_{n}.{ext}".casefold() in claimed:
        n += 1
    return f"{stem}_{n}.{ext}"


def _validate_rel_path(rel: str, section: str) -> None:
    """Reject manifest keys that are not normalized paths within a section."""
    posix = PurePosixPath(rel)
    windows = PureWindowsPath(rel)
    if (
        not rel
        or "\\" in rel
        # ``.`` parses to no parts at all — it names the section directory
        # itself, which is not a file the sync may write or unlink.
        or not posix.parts
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in ("", ".", "..") for part in posix.parts)
        or posix.as_posix() != rel
    ):
        raise SyncError(
            f"{MANIFEST}: unsafe '{section}' path {rel!r} "
            "(expected a normalized relative path within the section directory)",
        )


def _safe_path(root: Path, section: str, rel: str) -> Path:
    """Resolve a section path while refusing containment escapes and symlinks."""
    _validate_rel_path(rel, section)
    base = root / section
    if base.is_symlink():
        raise SyncError(f"refusing symbolic link for section directory: {base}")
    # Walk the components first: a symlink is the usual cause of an escape,
    # and naming it beats reporting the (perfectly ordinary) manifest key.
    current = base
    for part in PurePosixPath(rel).parts:
        current /= part
        if current.is_symlink():
            raise SyncError(f"refusing symbolic link in {section}: {rel}")
    candidate = base / rel
    resolved_base = base.resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if resolved_base not in resolved.parents:
        raise SyncError(f"refusing path outside {base}: {rel!r}")
    return candidate


def _read_file(root: Path, section: str, rel: str) -> str:
    p = _safe_path(root, section, rel)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(p, flags)
    except OSError as exc:
        raise SyncError(f"cannot safely read {p}: {exc}") from exc
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise SyncError(f"refusing non-regular file: {p}")
        return strip_header(f.read()) if section == "transforms" else f.read()


def _write_file(root: Path, section: str, rel: str, text: str) -> None:
    p = _safe_path(root, section, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Re-check after creating parents, then prevent following a final-component
    # symlink at open time where the platform provides O_NOFOLLOW.
    p = _safe_path(root, section, rel)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(p, flags, 0o666)
    except OSError as exc:
        raise SyncError(f"cannot safely write {p}: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


# ── manifest ──────────────────────────────────────────────────────────────────


def _refuse_manifest_symlink(p: Path) -> None:
    """``d2b.json`` is written in place on every sync — a link would make the
    sync write through it, so it is refused the way a section file is
    (``lstat`` sees a dangling link, which ``exists()`` reports as absent)."""
    if p.is_symlink():
        raise SyncError(f"refusing symbolic link for {MANIFEST}: {p}")


def _manifest_present(root: Path) -> bool:
    """Whether ``root`` has a ``d2b.json`` to speak of. ``lexists`` so that a
    symlink counts as present and is reported by the reader, rather than
    passing for an un-synced directory the way ``exists()`` would."""
    return os.path.lexists(root / MANIFEST)


def _read_manifest(root: Path) -> str | None:
    """The raw ``d2b.json``, or ``None`` when there is none."""
    p = root / MANIFEST
    _refuse_manifest_symlink(p)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(p, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:  # ELOOP: a link swapped in since the check above
        raise SyncError(f"cannot safely read {p}: {exc}") from exc
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise SyncError(f"refusing non-regular file: {p}")
        return f.read()


def _write_manifest(root: Path, text: str) -> None:
    """Replace ``d2b.json`` atomically. ``os.replace`` renames onto the path
    itself rather than following it, and a push that dies between two of its
    saves leaves the previous manifest rather than a truncated one.

    ``O_CREAT | O_EXCL`` with an explicit mode rather than ``tempfile``: the
    manifest is committed like any other file in the repository, so it wants
    the umask's permissions, and reading the umask is not thread-safe — a
    workspace pull saves a manifest per worker."""
    p = root / MANIFEST
    _refuse_manifest_symlink(p)
    tmp = p.with_name(f".{MANIFEST}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o666)
    except OSError as exc:
        raise SyncError(f"cannot safely write {p}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SyncError(f"cannot safely write {p}: {exc}") from exc
    except BaseException:                    # Ctrl-C mid-write leaves no litter
        tmp.unlink(missing_ok=True)
        raise


def load_manifest(root: Path) -> dict[str, Any] | None:
    p = root / MANIFEST
    raw = _read_manifest(root)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SyncError(f"{p}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise SyncError(f"{p}: malformed manifest (expected a JSON object)")
    for section in SECTIONS:
        if not isinstance(data.get(section, {}), dict):
            raise SyncError(f"{p}: malformed manifest ('{section}' must be an object)")
        data.setdefault(section, {})
        for rel in data[section]:
            _validate_rel_path(rel, section)
    return data


def save_manifest(root: Path, manifest: dict[str, Any]) -> None:
    ordered = dict(manifest)
    for section in SECTIONS:
        entries = dict(sorted(manifest.get(section, {}).items()))
        if entries or section == "transforms":
            ordered[section] = entries
        else:
            ordered.pop(section, None)   # keep the file small until a section is used
    _write_manifest(root, json.dumps(ordered, ensure_ascii=False, indent=2) + "\n")


def resolve_workbook(manifest: dict[str, Any] | None, workbook_id: str | None) -> str:
    bound = (manifest or {}).get("workbook_id")
    if workbook_id and bound and workbook_id != bound:
        raise SyncError(
            f"this directory is bound to workbook {bound}; use another --dir for {workbook_id}.",
        )
    wb = workbook_id or bound
    if not wb:
        raise SyncError("--workbook is required the first time (afterwards it is read from d2b.json).")
    return wb


def _empty_manifest(wb: str) -> dict[str, Any]:
    return {"workbook_id": wb, **{s: {} for s in SECTIONS}}


def _entry_outputs(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat form (artifact_name / args / layer on the entry) or ``outputs``."""
    raw = entry["outputs"] if isinstance(entry.get("outputs"), list) else [entry]
    outs: list[dict[str, Any]] = []
    for o in raw:
        if not isinstance(o, dict) or not o.get("artifact_name"):
            continue
        outs.append({
            "artifact_name": o["artifact_name"],
            "args": dict(o.get("args") or {}),
            "layer": o.get("layer"),
        })
    return outs


def _entry_fields(name: str, outputs: list[dict[str, Any]]) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name}
    if len(outputs) == 1:
        entry.update(outputs[0])
    else:
        entry["outputs"] = outputs
    return entry


def _entry_name(entry: dict[str, Any], rel: str) -> str:
    return str(entry.get("name") or entry.get("table") or _name_from_path(rel))


# ── remote view ───────────────────────────────────────────────────────────────


@dataclass
class RemoteTransform:
    name: str
    kind: str
    template: str
    outputs: list[dict[str, Any]] = field(default_factory=list)


def group_remote(entries: list[dict[str, Any]]) -> dict[str, RemoteTransform]:
    """``GET /transforms`` is one row per output; a file is one per name."""
    by_name: dict[str, RemoteTransform] = {}
    for e in entries:
        rt = by_name.get(e["name"])
        if rt is None:
            rt = by_name[e["name"]] = RemoteTransform(e["name"], e["kind"], e["template"])
        rt.outputs.append({
            "artifact_name": e["artifact_name"],
            "args": dict(e.get("args") or {}),
            "layer": e.get("layer"),
        })
    return by_name


def _chart_doc(chart: dict[str, Any]) -> dict[str, Any]:
    doc = {k: chart.get(k) for k in ("chart_type", "title", "config", "source_table", "recipe")}
    if chart.get("config_omitted"):
        doc["config_omitted"] = chart["config_omitted"]
    return doc


def _local_files(root: Path, subdir: str, suffixes: tuple[str, ...]) -> dict[str, str]:
    """rel path → raw content for every matching file under ``DIR/subdir``."""
    base = root / subdir
    if not base.exists():
        return {}
    out: dict[str, str] = {}
    for p in sorted(base.rglob("*")):
        # Only entries the sync would actually read are its business: a
        # symlinked README or an archive dir the section never touches is
        # not a reason to refuse the whole pull.
        if p.suffix not in suffixes:
            continue
        rel = p.relative_to(base).as_posix()
        if p.is_symlink():
            raise SyncError(f"refusing symbolic link in {subdir}: {rel}")
        if p.is_file():
            out[rel] = _read_file(root, subdir, rel)
    return out


_SUFFIXES = {
    "transforms": (".sql", ".py"),
    "sheets": (".json",),
    "charts": (".json",),
    "data": (".csv",),
}
_CANON: dict[str, Callable[[str], str]] = {
    "transforms": normalize,
    "sheets": _canon_json_text,
    "charts": _canon_json_text,
    "data": _canon_csv_text,
}


# ── pull ──────────────────────────────────────────────────────────────────────


@dataclass
class SectionReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    local_only: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            k: getattr(self, k) for k in (
                "added", "updated", "unchanged", "removed",
                "local_only", "untracked", "conflicts", "errors",
            )
        }


@dataclass
class Incoming:
    name: str
    ext: str
    text: str                    # canonical content (what the file will hold)
    entry: dict[str, Any]        # manifest fields besides ``hash``


def _pull_section(
    root: Path,
    section: str,
    old_entries: dict[str, dict[str, Any]],
    incoming: dict[str, Incoming],
    *,
    force: bool,
    keep_names: set[str] = frozenset(),   # type: ignore[assignment]
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str]], list[str], SectionReport]:
    """Decide one section's files. Returns (new entries, writes, removes,
    report); nothing touches the disk here."""
    canon = _CANON[section]
    local = _local_files(root, section, _SUFFIXES[section])
    report = SectionReport()
    # Keep a file where it already lives, whatever the naming rule says.
    path_by_name = {_entry_name(e, rel): rel for rel, e in old_entries.items()}
    claimed = {rel.casefold() for rel in old_entries}
    new_entries: dict[str, dict[str, Any]] = {}
    writes: list[tuple[str, str]] = []
    removes: list[str] = []

    for name in sorted(incoming):
        inc = incoming[name]
        rel = path_by_name.get(name)
        if rel is None or not rel.endswith(inc.ext):
            rel = _unique_path(path_for(name, inc.ext), claimed)
            claimed.add(rel.casefold())
        incoming_hash = digest(inc.text)
        base = (old_entries.get(rel) or {}).get("hash")
        if rel not in local:
            writes.append((rel, inc.text))
            report.added.append(rel)
        else:
            local_hash = digest(canon(local[rel]))
            if local_hash == incoming_hash:
                report.unchanged.append(rel)
            elif not force and (base is None or local_hash != base):
                report.conflicts.append({
                    "file": rel,
                    "reason": (
                        "never synced from here — the server has an item with this name"
                        if base is None else
                        "modified locally since the last pull, and the server changed too"
                    ),
                })
            else:
                writes.append((rel, inc.text))
                report.updated.append(rel)
        new_entries[rel] = {**inc.entry, "hash": incoming_hash}

    # Tracked here, gone on the server (or not fetched this time).
    for rel, entry in old_entries.items():
        if rel in new_entries:
            continue
        if _entry_name(entry, rel) in keep_names or entry.get("hash") is None:
            new_entries[rel] = entry
            if entry.get("hash") is None:
                report.local_only.append(rel)
            continue
        if rel in local and digest(canon(local[rel])) != entry["hash"] and not force:
            report.conflicts.append({
                "file": rel,
                "reason": "modified locally, but it no longer exists on the server",
            })
            new_entries[rel] = entry
            continue
        removes.append(rel)
        report.removed.append(rel)

    report.untracked = [rel for rel in local if rel not in new_entries and rel not in old_entries]
    return new_entries, writes, removes, report


def _fetch_table(
    client: Any, wb: str, table: str, entry: dict[str, Any] | None,
) -> tuple[str, str]:
    """The table as canonical CSV plus the branch it can merge back against.

    A cheap plain export first: when the rows still match the last sync the
    existing branch point stays valid and no new one is cut. Otherwise
    ``export = branch`` — the rows are frozen server-side as the 3-way base.
    """
    if entry and entry.get("branch_id") and entry.get("hash"):
        content = client.export.tables(
            wb, tables=[table], format="csv", include_row_ids=True,
        )
        text = csv_canonical(content.decode("utf-8-sig"))
        if digest(text) == entry["hash"]:
            return text, str(entry["branch_id"])
    content, branch_id = client.export.branch(wb, [table], format="csv")
    return csv_canonical(content.decode("utf-8-sig")), branch_id


def pull(
    client: Any,
    root: Path,
    workbook_id: str | None = None,
    *,
    force: bool = False,
    data_tables: list[str] | None = None,
) -> dict[str, Any]:
    """Server → files, every section. Nothing is written when any section
    has a conflict. ``data_tables`` opts base tables into the ``data``
    section (they stay tracked afterwards)."""
    manifest = load_manifest(root)
    wb = resolve_workbook(manifest, workbook_id)
    manifest = manifest or _empty_manifest(wb)
    result: dict[str, Any] = {"workbook_id": wb}
    new_sections: dict[str, dict[str, dict[str, Any]]] = {}
    writes: list[tuple[str, str, str]] = []
    removes: list[tuple[str, str]] = []
    conflicts = 0

    def collect(section: str, incoming: dict[str, Incoming], *, keep: set[str] = set()) -> SectionReport:
        nonlocal conflicts
        entries, w, r, report = _pull_section(
            root, section, manifest[section], incoming, force=force, keep_names=keep,
        )
        new_sections[section] = entries
        writes.extend((section, rel, text) for rel, text in w)
        removes.extend((section, rel) for rel in r)
        conflicts += len(report.conflicts)
        result[section] = report.as_dict()
        return report

    remote = group_remote(client.transforms.list(wb))
    collect("transforms", {
        name: Incoming(name, _EXT[rt.kind], normalize(rt.template), _entry_fields(name, rt.outputs))
        for name, rt in remote.items()
    })
    collect("sheets", {
        s["name"]: Incoming(s["name"], ".json", canonical_json(s["spec"]), {"name": s["name"]})
        for s in client.sheets.list(wb)
    })
    collect("charts", {
        c["name"]: Incoming(
            c["name"], ".json", canonical_json(_chart_doc(c)), {"name": c["name"], "readonly": True},
        )
        for c in client.charts.list(wb)
    })

    old_data = manifest["data"]
    tracked = {_entry_name(e, rel): e for rel, e in old_data.items()}
    tables = sorted(set(tracked) | set(data_tables or []))
    incoming: dict[str, Incoming] = {}
    errors: list[dict[str, str]] = []
    keep: set[str] = set()
    for table in tables:
        try:
            text, branch_id = _fetch_table(client, wb, table, tracked.get(table))
        except Exception as exc:  # the API's problem+json travels in str(exc)
            errors.append({"table": table, "error": str(exc)})
            keep.add(table)
            continue
        incoming[table] = Incoming(table, ".csv", text, {"table": table, "branch_id": branch_id})
    report = collect("data", incoming, keep=keep)
    report.errors = errors
    result["data"] = report.as_dict()

    if conflicts:
        raise SyncError(
            "pull refused: local changes would be overwritten "
            "(commit or stash them, or re-run with --force).",
            result,
        )

    for section, rel, text in writes:
        _write_file(root, section, rel, text.rstrip("\n") + "\n")
    for section, rel in removes:
        p = _safe_path(root, section, rel)
        if p.exists():
            p.unlink()
    manifest["workbook_id"] = wb
    for section, entries in new_sections.items():
        manifest[section] = entries
    root.mkdir(parents=True, exist_ok=True)
    save_manifest(root, manifest)
    return result


# ── push ──────────────────────────────────────────────────────────────────────


@dataclass
class PushItem:
    file: str
    name: str
    kind: str
    template: str                     # normalised, what is sent
    outputs: list[dict[str, Any]]     # the outputs that need a POST
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file, "name": self.name, "kind": self.kind,
            "artifacts": [o["artifact_name"] for o in self.outputs],
            "reason": self.reason,
        }


@dataclass
class SheetPush:
    file: str
    name: str
    spec: dict[str, Any]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "name": self.name, "reason": self.reason}


@dataclass
class DataPush:
    file: str
    table: str
    branch_id: str
    text: str                         # canonical CSV, what is merged

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "table": self.table, "branch_id": self.branch_id}


@dataclass
class PushPlan:
    workbook_id: str
    prune: bool = False
    transforms: list[PushItem] = field(default_factory=list)
    sheets: list[SheetPush] = field(default_factory=list)
    data: list[DataPush] = field(default_factory=list)
    prune_artifacts: list[tuple[str, str]] = field(default_factory=list)   # (file, artifact)
    prune_sheets: list[tuple[str, str]] = field(default_factory=list)      # (file, sheet)
    untrack: list[tuple[str, str]] = field(default_factory=list)           # (section, file)
    report: dict[str, dict[str, Any]] = field(default_factory=dict)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"workbook_id": self.workbook_id}
        for section in SECTIONS:
            out[section] = dict(self.report.get(section, {}))
        out["transforms"]["to_push"] = [i.as_dict() for i in self.transforms]
        out["sheets"]["to_push"] = [i.as_dict() for i in self.sheets]
        out["data"]["to_push"] = [i.as_dict() for i in self.data]
        if self.prune:
            out["prune"] = {
                "artifacts": [a for _, a in self.prune_artifacts],
                "sheets": [s for _, s in self.prune_sheets],
            }
        out["conflicts"] = self.conflicts
        return out


def _same_output(local: dict[str, Any], remote: dict[str, Any]) -> bool:
    if local["args"] != remote["args"]:
        return False
    # A local ``layer: null`` means "don't care"; a set one must match.
    return local["layer"] is None or local["layer"] == remote["layer"]


def _topo_order(items: list[PushItem]) -> list[PushItem]:
    """Upstream first: an item whose ``args`` name another item's output
    waits for it, so the intermediate server state is always consistent."""
    produced_by: dict[str, PushItem] = {}
    for it in items:
        for o in it.outputs:
            produced_by.setdefault(o["artifact_name"], it)
    deps: dict[str, set[str]] = {it.file: set() for it in items}
    for it in items:
        for o in it.outputs:
            for value in o["args"].values():
                producer = produced_by.get(value)
                if producer is not None and producer is not it:
                    deps[it.file].add(producer.file)
    by_file = {it.file: it for it in items}
    ordered: list[PushItem] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(f for f, d in remaining.items() if not d & set(remaining))
        if not ready:                      # cycle — fall back to name order
            ready = sorted(remaining)
        for f in ready:
            ordered.append(by_file[f])
            remaining.pop(f)
    return ordered


def _both_moved(
    plan: PushPlan, rel: str, *, base: str | None, remote_hash: str | None, force: bool,
) -> bool:
    """The text-section conflict rule. True = refuse this file."""
    if remote_hash is None or force:
        return False
    if base is None:
        plan.conflicts.append({"file": rel, "reason": "exists on the server but was never pulled here"})
        return True
    if remote_hash != base:
        plan.conflicts.append({
            "file": rel, "reason": "the server changed since the last pull (pull, merge, then push)",
        })
        return True
    return False


def plan_push(
    client: Any,
    root: Path,
    workbook_id: str | None = None,
    *,
    force: bool = False,
    prune: bool = False,
) -> PushPlan:
    """Files → what needs to reach the server. Raises on anything that
    would make the push partial or lossy; ``--force`` overrides only the
    text-section conflict rule, never a missing manifest entry."""
    manifest = load_manifest(root)
    if manifest is None:
        raise SyncError(f"no {MANIFEST} in {root} — run `d2b pull --workbook WB` first.")
    wb = resolve_workbook(manifest, workbook_id)
    plan = PushPlan(workbook_id=wb, prune=prune)

    # ── transforms ──
    entries = manifest["transforms"]
    local = _local_files(root, "transforms", _SUFFIXES["transforms"])
    remote = group_remote(client.transforms.list(wb))
    rep: dict[str, Any] = {"unchanged": [], "deleted_locally": [], "missing_entry": []}
    for rel, raw in local.items():
        entry = entries.get(rel)
        if entry is None:
            rep["missing_entry"].append(rel)
            continue
        name = _entry_name(entry, rel)
        kind = _kind_from_path(rel) or "sql"
        outputs = _entry_outputs(entry)
        if not outputs:
            plan.problems.append(f"transforms/{rel}: manifest entry needs artifact_name (and args) to push")
            continue
        template = normalize(raw)
        local_hash = digest(template)
        rt = remote.get(name)
        remote_hash = digest(rt.template) if rt is not None else None
        template_changed = rt is None or remote_hash != local_hash or rt.kind != kind
        if template_changed and _both_moved(
            plan, f"transforms/{rel}", base=entry.get("hash"), remote_hash=remote_hash, force=force,
        ):
            continue
        if template_changed:
            reason = "new" if rt is None else ("kind changed" if rt.kind != kind else "template changed")
            plan.transforms.append(PushItem(rel, name, kind, template, outputs, reason))
            continue
        assert rt is not None
        remote_outputs = {o["artifact_name"]: o for o in rt.outputs}
        pending = [
            o for o in outputs
            if o["artifact_name"] not in remote_outputs
            or not _same_output(o, remote_outputs[o["artifact_name"]])
        ]
        if pending:
            plan.transforms.append(PushItem(rel, name, kind, template, pending, "binding changed"))
        else:
            rep["unchanged"].append(rel)
    for rel, entry in entries.items():
        if rel in local:
            continue
        rep["deleted_locally"].append(rel)
        if prune:
            for o in _entry_outputs(entry):
                plan.prune_artifacts.append((rel, o["artifact_name"]))
            plan.untrack.append(("transforms", rel))
    plan.transforms = _topo_order(plan.transforms)
    if rep["missing_entry"]:
        plan.problems.append(
            "transforms without a manifest entry: " + ", ".join(rep["missing_entry"])
            + f' — add {{"artifact_name": …, "args": {{…}}}} under "transforms" in {MANIFEST}'
        )
    plan.report["transforms"] = rep

    # ── sheets ──
    entries = manifest["sheets"]
    local = _local_files(root, "sheets", _SUFFIXES["sheets"])
    remote_sheets = {s["name"]: canonical_json(s["spec"]) for s in client.sheets.list(wb)}
    rep = {"unchanged": [], "deleted_locally": [], "errors": []}
    for rel, raw in local.items():
        entry = entries.get(rel) or {}
        name = _entry_name(entry, rel)
        try:
            spec = json.loads(raw)
        except ValueError as exc:
            rep["errors"].append({"file": rel, "error": f"not valid JSON: {exc}"})
            plan.problems.append(f"sheets/{rel}: not valid JSON")
            continue
        if not isinstance(spec, dict) or not isinstance(spec.get("blocks"), list):
            rep["errors"].append({"file": rel, "error": 'expected {"blocks": [...]}'})
            plan.problems.append(f'sheets/{rel}: expected {{"blocks": [...]}}')
            continue
        text = canonical_json(spec)
        local_hash = digest(text)
        remote_text = remote_sheets.get(name)
        remote_hash = digest(remote_text) if remote_text is not None else None
        if local_hash == remote_hash:
            rep["unchanged"].append(rel)
            continue
        if _both_moved(plan, f"sheets/{rel}", base=entry.get("hash"), remote_hash=remote_hash, force=force):
            continue
        plan.sheets.append(SheetPush(rel, name, spec, "new" if remote_hash is None else "changed"))
    for rel, entry in entries.items():
        if rel in local:
            continue
        rep["deleted_locally"].append(rel)
        if prune:
            plan.prune_sheets.append((rel, _entry_name(entry, rel)))
            plan.untrack.append(("sheets", rel))
    plan.report["sheets"] = rep

    # ── charts (read-only) ──
    entries = manifest["charts"]
    local = _local_files(root, "charts", _SUFFIXES["charts"])
    rep = {"readonly_modified": [], "deleted_locally": []}
    for rel, raw in local.items():
        entry = entries.get(rel)
        if entry is not None and entry.get("hash") not in (None, digest(_canon_json_text(raw))):
            rep["readonly_modified"].append(rel)
    rep["deleted_locally"] = [rel for rel in entries if rel not in local]
    plan.report["charts"] = rep

    # ── data (3-way merge on the server) ──
    entries = manifest["data"]
    local = _local_files(root, "data", _SUFFIXES["data"])
    rep = {"unchanged": [], "deleted_locally": [], "missing_entry": []}
    for rel, raw in local.items():
        entry = entries.get(rel)
        if entry is None or not entry.get("branch_id"):
            rep["missing_entry"].append(rel)
            continue
        text = _canon_csv_text(raw)
        if digest(text) == entry.get("hash"):
            rep["unchanged"].append(rel)
            continue
        plan.data.append(DataPush(rel, _entry_name(entry, rel), str(entry["branch_id"]), text))
    for rel in entries:
        if rel not in local:
            rep["deleted_locally"].append(rel)
            plan.untrack.append(("data", rel))          # stop tracking; the table stays
    if rep["missing_entry"]:
        plan.problems.append(
            "data files without a branch: " + ", ".join(rep["missing_entry"])
            + " — a table joins the data section through `d2b pull --data TABLE` (export = branch)"
        )
    plan.report["data"] = rep

    if plan.conflicts and not force:
        plan.problems.append("conflicts: " + ", ".join(c["file"] for c in plan.conflicts))
    if plan.problems:
        raise SyncError("push refused: " + "; ".join(plan.problems), plan.as_dict())
    return plan


def execute_push(client: Any, root: Path, plan: PushPlan) -> dict[str, Any]:
    """Run the plan, recording each success in the manifest as it lands — a
    failure mid-way leaves a manifest the next push resumes from.

    Order: data merges first (transforms then re-run against merged rows),
    transforms upstream-first, sheets, then prune. Every transform POST
    re-runs on the server — that is the point: the artifact is rebuilt from
    the reviewed logic."""
    manifest = load_manifest(root)
    assert manifest is not None
    wb = plan.workbook_id
    out: dict[str, Any] = {"data": [], "transforms": [], "sheets": [], "pruned": [], "prune_errors": []}

    for item in plan.data:
        result = client.tables.merge(
            wb, item.table, item.text.encode("utf-8"),
            filename=f"{item.table}.csv", branch_id=item.branch_id,
        )
        # Re-branch: the file now reflects the merged server state (their
        # values where D2B also moved, plus D2B-only changes) and a fresh base.
        text, branch_id = _fetch_table(client, wb, item.table, None)
        _write_file(root, "data", item.file, text.rstrip("\n") + "\n")
        entry = manifest["data"].setdefault(item.file, {"table": item.table})
        entry.update({"branch_id": branch_id, "hash": digest(text)})
        save_manifest(root, manifest)
        out["data"].append({
            "file": item.file, "table": item.table,
            "applied": result.get("applied"), "conflicts": result.get("conflicts", []),
        })

    for item in plan.transforms:
        for o in item.outputs:
            client.transforms.create(
                wb, name=item.name, kind=item.kind, template=item.template,
                artifact_name=o["artifact_name"], args=o["args"], layer=o["layer"],
            )
        entry = manifest["transforms"].setdefault(item.file, {"name": item.name})
        entry["hash"] = digest(item.template)
        save_manifest(root, manifest)
        out["transforms"].append(item.as_dict())

    for sheet in plan.sheets:
        client.sheets.put(wb, sheet.name, sheet.spec["blocks"])
        entry = manifest["sheets"].setdefault(sheet.file, {"name": sheet.name})
        entry["hash"] = digest(canonical_json(sheet.spec))
        save_manifest(root, manifest)
        out["sheets"].append(sheet.as_dict())

    for rel, artifact in plan.prune_artifacts:
        try:
            client.tables.delete(wb, artifact)
            out["pruned"].append({"kind": "artifact", "name": artifact, "file": rel})
        except Exception as exc:
            out["prune_errors"].append({"kind": "artifact", "name": artifact, "error": str(exc)})
    for rel, name in plan.prune_sheets:
        try:
            client.sheets.delete(wb, name)
            out["pruned"].append({"kind": "sheet", "name": name, "file": rel})
        except Exception as exc:
            out["prune_errors"].append({"kind": "sheet", "name": name, "error": str(exc)})
    failed = {(e["kind"], e["name"]) for e in out["prune_errors"]}
    for section, rel in plan.untrack:
        entry = manifest[section].get(rel) or {}
        if section == "transforms" and any(
            ("artifact", o["artifact_name"]) in failed for o in _entry_outputs(entry)
        ):
            continue
        if section == "sheets" and ("sheet", _entry_name(entry, rel)) in failed:
            continue
        manifest[section].pop(rel, None)
    save_manifest(root, manifest)
    return out
