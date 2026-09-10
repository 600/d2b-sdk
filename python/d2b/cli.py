"""``d2b`` — the command-line face of the SDK.

Designed for both humans and shell-driving agents:

- output is JSON on stdout (machine-parseable by default; binary
  endpoints write to ``-o FILE``);
- errors go to stderr WITH the API's ``suggested_fix`` — the same
  LLM-readable contract the HTTP layer carries, so an agent that runs
  ``d2b`` in a terminal can read the failure and self-correct;
- auth/config via env (``D2B_API_KEY`` / ``D2B_BASE_URL``) or saved login —
  twelve-factor, no hidden state and no secrets in argv.

Examples::

    export D2B_API_KEY=d2b_pat_... D2B_BASE_URL=https://d2b.dev
    d2b workbooks create --title monthly
    d2b upload sales.xlsx --workbook WB --wait
    d2b tables list --workbook WB
    d2b query 'SELECT count(*) FROM "売上明細"' --workbook WB
    d2b export --workbook WB --tables 売上明細 -o out.xlsx
    d2b sources render report.xlsx --workbook WB -o monthly.xlsx
    d2b pull --workbook WB            # transforms/sheets/charts → files + d2b.json
    d2b pull --data customers         # + a base table as data/customers.csv (export = branch)
    d2b push --commit "$(git rev-parse --short HEAD)"   # changed files → re-run / merge + version
    d2b github-workflow > .github/workflows/d2b.yml     # CI: push on merge, pull → PR on a schedule
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, NoReturn

import httpx

from ._version import __version__
from . import client as _client_mod
from .client import D2BClient, D2BError

# All CLI traffic announces itself as the CLI, not the bare SDK.
_client_mod.CLIENT_TAG = f"d2b-cli/{__version__}"
from .sync import (
    MANIFEST,
    SyncError,
    execute_push,
    load_manifest,
    plan_push,
    pull,
    resolve_workbook,
)
from .sync_workspace import (
    WORKBOOKS_DIR,
    load_ledger,
    pull_workspace,
    push_workspace,
    status_workspace,
)
from .workflow_template import GITHUB_WORKFLOW


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _write_binary(content: bytes, out: str) -> None:
    with open(out, "wb") as fh:
        fh.write(content)
    _emit({"written": out, "bytes": len(content)})




# ── credentials (browser login) ───────────────────────────────────────────────


def _config_dir() -> Path:
    base = os.environ.get("D2B_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "d2b",
    )
    return Path(base)


def _credentials_path() -> Path:
    return _config_dir() / "credentials.json"


def _load_credentials() -> dict:
    try:
        return json.loads(_credentials_path().read_text())
    except Exception:
        return {}


def _save_credentials(creds: dict) -> None:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(creds, ensure_ascii=False, indent=2))
    os.chmod(path, 0o600)  # the token is a bearer credential


def _entry_token(entry: dict, account: str | None) -> str | None:
    """Pick the credential for ``account`` out of a saved login.

    A login holds one token per approved Account (``accounts``: id ->
    credential) plus ``default_account``; ``--account`` / D2B_ACCOUNT_ID
    picks one, otherwise the default. Logins saved before per-account
    tokens carry a single ``token`` and ignore the selector.
    """
    accounts: dict = entry.get("accounts") or {}
    if not accounts:
        return entry.get("token") if account is None else None
    chosen = account or entry.get("default_account") or next(iter(accounts))
    picked = accounts.get(chosen)
    return picked["token"] if picked else None


def _resolve_auth(args: argparse.Namespace) -> tuple[str, str] | None:
    """env > saved login. Returns (api_key, base_url) or None."""
    base_url = args.base_url or os.environ.get("D2B_BASE_URL")
    api_key = os.environ.get("D2B_API_KEY")
    if api_key and base_url:
        return api_key, base_url
    account = getattr(args, "account", None) or os.environ.get("D2B_ACCOUNT_ID") or None
    creds = _load_credentials()
    if base_url:
        entry = creds.get(base_url.rstrip("/"))
        token = _entry_token(entry, account) if entry else None
        if token:
            return token, base_url
        if entry and account:
            print(
                f"error: no login for account {account} at {base_url} — "
                "run `d2b login` and tick that account, or drop --account.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return None
    if len(creds) == 1:  # the common case: one saved login
        url, entry = next(iter(creds.items()))
        token = _entry_token(entry, account)
        if token:
            return token, url
        if account:
            print(
                f"error: no login for account {account} — run `d2b login` and "
                "tick that account, or drop --account.",
                file=sys.stderr,
            )
            raise SystemExit(2)
    return None


def login_flow(base_url: str, http: httpx.Client, *, name: str = "cli",
               open_browser: bool = True, timeout: float = 600.0,
               scopes: list[str] | None = None) -> dict:
    """The device-auth dance. Returns the poll payload incl. the token.

    ``http`` is injectable for tests; production passes a plain client
    bound to ``base_url``.
    """
    start = http.post(
        "/api/cli/auth/start",
        json={"name": name, **({"scopes": scopes} if scopes else {})},
    )
    start.raise_for_status()
    info = start.json()
    print(f"確認コード: {info['user_code']}", file=sys.stderr)
    print(f"ブラウザで承認してください: {info['verify_url']}", file=sys.stderr)
    print("(ターミナルのコードと画面のコードが一致することを確認)", file=sys.stderr)
    if open_browser:
        try:
            webbrowser.open(info["verify_url"])
        except Exception:
            pass
    deadline = time.monotonic() + min(timeout, float(info.get("expires_in", 600)))
    interval = float(info.get("poll_interval", 2))
    while time.monotonic() < deadline:
        poll = http.get(f"/api/cli/auth/poll/{info['session_id']}")
        payload = poll.json()
        status = payload.get("status")
        if status == "approved":
            return payload
        if status in ("denied", "expired"):
            raise SystemExit(f"login {status}")
        time.sleep(interval)
    raise SystemExit("login timed out")


def _build_client(args: argparse.Namespace) -> D2BClient:
    resolved = _resolve_auth(args)
    if resolved is None:
        print(
            "error: not logged in — run `d2b login` "
            "(or set D2B_API_KEY and D2B_BASE_URL for non-interactive use).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    api_key, base_url = resolved
    return D2BClient(api_key=api_key, base_url=base_url)


# D2B credentials are ``d2b_…``-shaped (e.g. ``d2b_pat_…``). When a secret is
# passed as a flag, argparse quotes the offending argv straight back to stderr
# ("unrecognized arguments: …", "invalid choice: …") — leaking it. Redact the
# token shape from every parser error, whatever flag (or typo) carried it.
_SECRET_RE = re.compile(r"d2b_[A-Za-z0-9_]+")


class _RedactingParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that never echoes secret-shaped argv in errors.

    ``add_subparsers`` defaults ``parser_class`` to ``type(self)``, so every
    subcommand parser inherits this redaction without extra wiring.
    """

    def error(self, message: str) -> NoReturn:
        super().error(_SECRET_RE.sub("***", message))


def _add_base_url_everywhere(parser: argparse.ArgumentParser) -> None:
    """Accept ``--base-url`` after any (sub)command too — ``d2b login
    --base-url URL`` is the documented form, and argparse only honors
    root-level options BEFORE the subcommand. SUPPRESS keeps a subcommand
    parse from clobbering a value given in the root position.
    (Walks argparse's subparser actions; that private shape has been
    stable since 2.7 and the test suite pins the behavior.)
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in set(action.choices.values()):
                sub.add_argument(
                    "--base-url", default=argparse.SUPPRESS,
                    help="API origin (default: $D2B_BASE_URL)",
                )
                _add_base_url_everywhere(sub)


# ── d2b init ─────────────────────────────────────────────────────────────────
# The operating norms come from the server (MCP ``instructions`` /
# ``d2b://guide``), so a repo only needs the connection and the choice of
# path. ``init`` is host-agnostic on purpose: hosts keep appearing (Codex,
# OpenCode, Gemini CLI, Windsurf, Cline …) but their configs are one of
# three SHAPES — the ``mcpServers`` JSON most of them share, VS Code's
# ``servers`` + ``inputs``, and Codex's TOML — differing mainly in WHERE the
# file lives. So: the default writes the AGENTS.md section and prints the
# canonical entry (paste it anywhere); ``--config PATH`` merges it into any
# file (shape from the extension or ``--format``); ``--host`` is a data
# table of known locations, one row each, never a code path. The token is
# always an env / input reference, never written.

DEFAULT_PUBLIC_BASE_URL = "https://d2b.dev"
AGENTS_SNIPPET = """## D2B
- Data work goes through D2B. The MCP server `d2b` is connected — follow the norms in `d2b://guide` first.
- For git management (`d2b pull` / `d2b push`) and batch work use the CLI `uvx --from d2b-sdk d2b ...` (JSON output; auth from env D2B_API_KEY / D2B_BASE_URL).
- Destinations, how to bring files in and how versions work are in the guide. When unsure, ask the human.
"""
INIT_FORMATS = ("mcpservers", "vscode", "toml")
# host -> (config path: repo-relative, or ~-prefixed for user-level configs; format)
INIT_HOSTS: dict[str, tuple[str, str]] = {
    "claude-code": (".mcp.json", "mcpservers"),
    "cursor": (".cursor/mcp.json", "mcpservers"),
    "windsurf": ("~/.codeium/windsurf/mcp_config.json", "mcpservers"),
    "claude-desktop": ("~/Library/Application Support/Claude/claude_desktop_config.json", "mcpservers"),
    "vscode": (".vscode/mcp.json", "vscode"),
    "codex": ("~/.codex/config.toml", "toml"),
}


def _init_entry(fmt: str, mcp_url: str) -> dict:
    """The server entry in a given shape. The token is referenced through
    the host's own env / input mechanism."""
    if fmt == "mcpservers":
        return {"type": "http", "url": mcp_url,
                "headers": {"Authorization": "Bearer ${D2B_API_KEY}"}}
    if fmt == "vscode":
        return {"type": "http", "url": mcp_url,
                "headers": {"Authorization": "Bearer ${input:d2b_pat}"}}
    if fmt == "toml":
        return {"url": mcp_url, "bearer_token_env_var": "D2B_API_KEY"}
    raise ValueError(fmt)


def _init_format_for(path: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    if path.suffix == ".toml":
        return "toml"
    if ".vscode" in path.parts:
        return "vscode"
    return "mcpservers"


def _init_safe_target(base: Path, raw: str) -> Path:
    """Return an output path confined to *base*, rejecting symlink components."""
    requested = Path(raw).expanduser()
    if requested.is_absolute():
        raise ValueError(f"output path must be relative to {base}: {raw}")
    target = base / requested
    current = base
    for part in target.relative_to(base).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"output path contains a symbolic link: {current}")
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(base):
        raise ValueError(f"output path escapes {base}: {raw}")
    return target


_TOML_D2B_HEADER = re.compile(r"^\s*\[mcp_servers\.d2b\]\s*$", re.M)


def _init_write_toml(target: Path, entry: dict) -> None:
    """Replace or append the ``[mcp_servers.d2b]`` table textually (the
    stdlib reads TOML but does not write it), then check the result parses."""
    import tomllib

    current = target.read_text(encoding="utf-8") if target.exists() else ""
    if current:
        try:
            tomllib.loads(current)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{target} is not valid TOML — fix or remove it first ({exc})") from exc
    lines = current.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if _TOML_D2B_HEADER.match(line):
            skipping = True
            continue
        if skipping and re.match(r"^\s*\[", line):
            skipping = False
        if not skipping:
            out.append(line)
    block = ["[mcp_servers.d2b]"] + [
        f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in entry.items()
    ]
    text = "\n".join(out).rstrip("\n")
    text = (text + "\n\n" if text else "") + "\n".join(block) + "\n"
    tomllib.loads(text)  # never leave a file the host cannot read
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _init_write_json(target: Path, fmt: str, entry: dict) -> None:
    existing: dict = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise ValueError(f"{target} is not valid JSON — fix or remove it first ({exc})") from exc
        if not isinstance(existing, dict):
            raise ValueError(f"{target} must hold a JSON object")
    key = "servers" if fmt == "vscode" else "mcpServers"
    servers = existing.setdefault(key, {})
    if not isinstance(servers, dict):
        raise ValueError(f"{target}: '{key}' must be an object")
    servers["d2b"] = entry
    if fmt == "vscode":
        inputs = existing.setdefault("inputs", [])
        if not any(isinstance(i, dict) and i.get("id") == "d2b_pat" for i in inputs):
            inputs.append({"id": "d2b_pat", "type": "promptString", "password": True,
                           "description": "D2B PAT"})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    base = (args.base_url or os.environ.get("D2B_BASE_URL") or DEFAULT_PUBLIC_BASE_URL).rstrip("/")
    mcp_url = base + "/mcp/"

    # Resolve and validate every target before writing any of them. User-supplied
    # paths are repository-relative; known user-level hosts are confined to HOME.
    targets: list[tuple[Path, str, str]] = []  # (path, format, label)
    try:
        for host in args.host or []:
            rel, fmt = INIT_HOSTS[host]
            if rel.startswith("~/"):
                home = Path.home().resolve()
                path = _init_safe_target(home, rel[2:])
            else:
                path = _init_safe_target(root, rel)
            targets.append((path, fmt, host))
        for raw in args.config or []:
            path = _init_safe_target(root, raw)
            targets.append((path, _init_format_for(path, args.format), raw))
        snippet_path = None if args.no_snippet else _init_safe_target(root, args.snippet_file)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    wrote: list[str] = []
    skipped: list[str] = []
    for path, fmt, label in targets:
        entry = _init_entry(fmt, mcp_url)
        try:
            if fmt == "toml":
                _init_write_toml(path, entry)
            else:
                _init_write_json(path, fmt, entry)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        shown = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        wrote.append(shown)

    if snippet_path is not None:
        current = snippet_path.read_text(encoding="utf-8") if snippet_path.exists() else ""
        if "## D2B" in current:
            skipped.append(args.snippet_file)
        else:
            sep = "" if not current else ("\n" if current.endswith("\n") else "\n\n")
            snippet_path.write_text(current + sep + AGENTS_SNIPPET, encoding="utf-8")
            wrote.append(args.snippet_file)

    # The canonical entry is always in the output: any host not in the
    # table takes this JSON verbatim (or --config PATH writes it).
    _emit({
        "mcp_url": mcp_url,
        "server": {"mcpServers": {"d2b": _init_entry("mcpservers", mcp_url)}},
        "wrote": wrote,
        "skipped": skipped,
        "known_hosts": sorted(INIT_HOSTS),
    })
    if not targets:
        print(
            "hint: no config written — paste `server` into your host's MCP config, or re-run "
            "with --host <name> / --config PATH.",
            file=sys.stderr,
        )
    if not os.environ.get("D2B_API_KEY"):
        print(
            "next: export D2B_API_KEY=<PAT> (configs reference it; nothing was written to "
            "disk) — mint one with `d2b login` or in the console.",
            file=sys.stderr,
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    p = _RedactingParser(
        prog="d2b",
        description="D2B — Spreadsheets for AI Agents (typed, versioned, governed tables).",
        allow_abbrev=False,
    )
    p.add_argument("--version", action="version", version=f"d2b {__version__}")
    p.add_argument(
        "--base-url",
        help=f"API origin (default: $D2B_BASE_URL, else {DEFAULT_PUBLIC_BASE_URL})",
    )
    p.add_argument(
        "--account",
        help="account to act for when the login holds several "
             "(default: $D2B_ACCOUNT_ID, else the account marked default at login)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # auth
    lg = sub.add_parser("login", help="browser login (no raw API keys)")
    lg.add_argument("--name", default="cli", help="token label (default: cli)")
    lg.add_argument("--no-browser", action="store_true",
                    help="print the URL instead of opening a browser")
    lg.add_argument(
        "--scopes",
        help="comma-separated scopes for the token (server default: workbooks:read,"
             "workbooks:write,workbooks:delete,cloud-files:read — the whole workbooks family plus your own "
             "Drive / OneDrive, so the CLI "
             "can delete the workbooks it creates; pass a narrower list, e.g. "
             "workbooks:read, for a read-only login)",
    )
    sub.add_parser("logout", help="forget (and revoke) the saved login")
    init = sub.add_parser(
        "init",
        help="write the AGENTS.md snippet and print/merge the MCP server entry for any host "
             "(no token is written)",
    )
    init.add_argument(
        "--host", action="append", choices=sorted(INIT_HOSTS),
        help="also write a known host's config (repeatable): " + ", ".join(
            f"{h} → {p}" for h, (p, _f) in sorted(INIT_HOSTS.items())),
    )
    init.add_argument(
        "--config", action="append", metavar="PATH",
        help="also merge the entry into this file (repeatable; shape from the extension / "
             "location, or --format)",
    )
    init.add_argument("--format", choices=INIT_FORMATS,
                      help="shape for --config files (default: mcpservers; .toml → toml; "
                           ".vscode/ → vscode)")
    init.add_argument("--dir", default=".", help="repository root (default: current directory)")
    init.add_argument(
        "--snippet-file", default="AGENTS.md",
        help="where to add the D2B section (default: AGENTS.md; use CLAUDE.md for Claude Code)",
    )
    init.add_argument("--no-snippet", action="store_true", help="do not touch the snippet file")
    sub.add_parser("whoami", help="show the authenticated principal")
    ws = sub.add_parser("workspaces", help="the workspaces this credential can address")
    ws_sub = ws.add_subparsers(dest="sub", required=True)
    ws_sub.add_parser("list", help="list reachable workspaces by name (is_default = where "
                                   "`workbooks create` lands without --workspace-id)")

    # workbooks
    wb = sub.add_parser("workbooks", help="workbook container operations")
    wb_sub = wb.add_subparsers(dest="sub", required=True)
    wb_create = wb_sub.add_parser("create")
    wb_create.add_argument("--title", required=True, help="workbook display name")
    wb_create.add_argument(
        "--workspace-id",
        help="create inside this workspace (id, or 'personal' for your own)",
    )
    wb_list = wb_sub.add_parser("list")
    wb_list.add_argument(
        "--workspace-id",
        help="scope the listing to one workspace (id, or 'personal')",
    )
    wb_del = wb_sub.add_parser(
        "delete", help="delete a workbook you own (needs workbooks:delete — a default "
                       "`d2b login` holds it; not undoable)",
    )
    wb_del.add_argument("id", help="workbook id")

    # upload
    up = sub.add_parser("upload", help="upload a file into a workbook")
    up.add_argument("file")
    up.add_argument("--workbook", required=True)
    up.add_argument("--mode", choices=["auto", "staged"], default="auto")
    up_st = up.add_mutually_exclusive_group()
    up_st.add_argument(
        "--structure", action="store_true",
        help="auto mode: run D2B's LLM structuring inline and promote its output over the raw",
    )
    up_st.add_argument(
        "--no-structuring", action="store_true",
        help="auto mode: land the raw baseline only (~1s) — the default; reshape with your own LLM",
    )
    up_st.add_argument(
        "--defer-structuring", action="store_true",
        help="auto mode: return raw now; LLM structuring swaps in later (webhook-visible)",
    )
    up.add_argument("--wait", action="store_true",
                    help="async ingest + poll the job to completion")

    # file links: a local file under D2B version control
    tr = sub.add_parser(
        "track", help="put a local xlsx under version control (v1 = its current bytes)",
    )
    tr.add_argument("file")
    tr.add_argument("--workbook", required=True)
    tr.add_argument("--as", dest="as_name", help="register the source under another name")
    tr.add_argument(
        "--on-existing", choices=["auto", "seed", "replace", "refuse"], default="auto",
        help="same-name source already in the workbook: auto = keep D2B's copy as v1 when it looks "
             "like the same file, else refuse; replace = overwrite it; seed = force v1 from it",
    )
    wa = sub.add_parser(
        "watch",
        help="watch local xlsx files and push every change as a version "
             "(works on a OneDrive / Drive / Dropbox sync folder too)",
    )
    wa.add_argument("files", nargs="+")
    wa.add_argument("--workbook", required=True)
    wa.add_argument("--interval", type=float, default=2.0, help="seconds between checks")
    wa.add_argument(
        "--on-existing", choices=["auto", "seed", "replace", "refuse"], default="auto",
        help="as for `track`, applied when a watched file is not tracked yet",
    )
    wa.add_argument("--once", action="store_true", help="check once and exit (cron-friendly)")
    # tables
    tb = sub.add_parser("tables", help="the data plane")
    tb_sub = tb.add_subparsers(dest="sub", required=True)
    t_list = tb_sub.add_parser("list", help="tables and views in the workbook")
    t_list.add_argument("--workbook", required=True)
    t_list.add_argument(
        "--include-archived", action="store_true",
        help="also list archived raws (superseded at ingest; `tables unarchive` brings one back)",
    )
    t_schema = tb_sub.add_parser(
        "schema", help="columns + types (--json-schema: constrained-decoding row schema)",
    )
    t_schema.add_argument("name")
    t_schema.add_argument("--workbook", required=True)
    t_schema.add_argument(
        "--json-schema", action="store_true",
        help="emit a JSON Schema for one row (constrained-decoding target)")
    t_rows = tb_sub.add_parser(
        "rows", help="page through rows as JSON (returns edit_version for safe writes)",
    )
    t_rows.add_argument("name")
    t_rows.add_argument("--workbook", required=True)
    t_rows.add_argument("--limit", type=int, default=100)
    t_rows.add_argument("--offset", type=int, default=0)
    t_a1 = tb_sub.add_parser("a1", help="read a rectangular range by A1 address")
    t_a1.add_argument("name")
    t_a1.add_argument("range", help='e.g. "A1:D10" or "C3"')
    t_a1.add_argument("--workbook", required=True)
    t_un = tb_sub.add_parser("unarchive")
    t_un.add_argument("name")
    t_un.add_argument("--workbook", required=True)
    t_rn = tb_sub.add_parser("rename", help="rename a table/view's display name (id stays)")
    t_rn.add_argument("name")
    t_rn.add_argument("--workbook", required=True)
    t_rn.add_argument("--to", dest="new_name", required=True, help="new display name")
    # columns (schema evolution)
    c_add = tb_sub.add_parser("add-column")
    c_add.add_argument("name")
    c_add.add_argument("column")
    c_add.add_argument("--type", default="VARCHAR")
    c_add.add_argument("--default")
    c_add.add_argument("--workbook", required=True)
    c_add.add_argument("--expected-version", type=int)
    c_rn = tb_sub.add_parser("rename-column")
    c_rn.add_argument("name")
    c_rn.add_argument("column")
    c_rn.add_argument("new_name")
    c_rn.add_argument("--workbook", required=True)
    c_rn.add_argument("--expected-version", type=int)
    c_rt = tb_sub.add_parser("retype-column")
    c_rt.add_argument("name")
    c_rt.add_argument("column")
    c_rt.add_argument("type")
    c_rt.add_argument("--workbook", required=True)
    c_rt.add_argument("--expected-version", type=int)
    c_dr = tb_sub.add_parser("drop-column")
    c_dr.add_argument("name")
    c_dr.add_argument("column")
    c_dr.add_argument("--workbook", required=True)
    c_dr.add_argument("--expected-version", type=int)
    c_wa = tb_sub.add_parser(
        "write-a1", help="write values into an A1 range (optimistic-locked)",
    )
    c_wa.add_argument("name")
    c_wa.add_argument("range", help='A1 range, e.g. "B2:C3"')
    c_wa.add_argument("values", help='JSON 2-D array, e.g. "[[10],[20]]"')
    c_wa.add_argument("--workbook", required=True)
    c_wa.add_argument("--expected-version", type=int)
    # live formulas (deliver a column as a per-row Excel formula)
    f_set = tb_sub.add_parser(
        "set-formula", help="deliver a column as a per-row Excel formula")
    f_set.add_argument("name")
    f_set.add_argument("column")
    f_set.add_argument("expr", help='e.g. "{running} / {count}" — {col}, never A1')
    f_set.add_argument("--workbook", required=True)
    f_list = tb_sub.add_parser("formulas", help="list a table's formula columns")
    f_list.add_argument("name")
    f_list.add_argument("--workbook", required=True)
    f_clr = tb_sub.add_parser("clear-formula")
    f_clr.add_argument("name")
    f_clr.add_argument("column")
    f_clr.add_argument("--workbook", required=True)

    # query
    q = sub.add_parser("query", help="read-only SQL (governance applied)")
    q.add_argument("sql")
    q.add_argument("--workbook", required=True)
    q.add_argument("--limit", type=int, default=1000)

    # export
    ex = sub.add_parser("export", help="deliver tables as xlsx/csv")
    ex.add_argument("--workbook", required=True)
    ex.add_argument("--tables", help="comma-separated (default: all)")
    ex.add_argument("--format", choices=["xlsx", "csv"], default="xlsx")
    ex.add_argument("-o", "--out", required=True)

    # sources
    src = sub.add_parser("sources", help="originals and template write-back")
    src_sub = src.add_subparsers(dest="sub", required=True)
    s_list = src_sub.add_parser("list")
    s_an = src_sub.add_parser(
        "analyze", help="staged flow 1/3: detect structure, persist the parse spec",
    )
    s_an.add_argument("name", help="source name")
    s_an.add_argument("--workbook", required=True)
    s_ps = src_sub.add_parser(
        "parse-spec",
        help="staged flow 2/3: print the parse spec (--set-file FILE replaces its regions)",
    )
    s_ps.add_argument("name", help="source name")
    s_ps.add_argument("--workbook", required=True)
    s_ps.add_argument(
        "--set-file",
        help="JSON file — or the JSON itself — with {\"regions\": [...]} (or a bare regions array) to PUT",
    )
    s_mt = src_sub.add_parser(
        "materialize", help="staged flow 3/3: turn the analysed regions into typed tables",
    )
    s_mt.add_argument("name", help="source name")
    s_mt.add_argument("--workbook", required=True)
    s_mt.add_argument("--region-ids", help="comma-separated subset (default: all)")
    s_list.add_argument("--workbook", required=True)
    s_dl = src_sub.add_parser("download", help="L0: the byte-identical original")
    s_dl.add_argument("name")
    s_dl.add_argument("--workbook", required=True)
    s_dl.add_argument("-o", "--out", required=True)
    s_rd = src_sub.add_parser(
        "render", help="L1: original xlsx with data regions refreshed",
    )
    s_rd.add_argument("name")
    s_rd.add_argument("--workbook", required=True)
    s_rd.add_argument("-o", "--out", required=True)
    s_rv = src_sub.add_parser(
        "revise", help="L1: edit a region via a SQL transform, get the original xlsx back",
    )
    s_rv.add_argument("name", help="source name")
    s_rv.add_argument("--workbook", required=True)
    s_rv.add_argument("--transform-name", required=True, help="transform label (lineage)")
    s_rv_sql = s_rv.add_mutually_exclusive_group(required=True)
    s_rv_sql.add_argument(
        "--sql",
        help=('SQL template inline; must start with CREATE OR REPLACE VIEW "{{ artifact_name }}" AS '
              'and read the region as "{{ src }}" (keep every column the region has)'),
    )
    s_rv_sql.add_argument("--sql-file", help="path to the SQL template file")
    s_rv.add_argument("--range", help="A1 rectangle of the target region (e.g. A3:N8)")
    s_rv.add_argument("--sheet", help="sheet name for --range")
    s_rv.add_argument("--region-id", help="existing region id (instead of --range)")
    s_rv.add_argument("-o", "--out", required=True)

    # sheets
    sh = sub.add_parser("sheets", help="presentation-plane compositions")
    sh_sub = sh.add_subparsers(dest="sub", required=True)
    sh_list = sh_sub.add_parser("list")
    sh_list.add_argument("--workbook", required=True)
    sh_rd = sh_sub.add_parser("render")
    sh_rd.add_argument("name")
    sh_rd.add_argument("--workbook", required=True)
    sh_rd.add_argument("-o", "--out", required=True)
    sh_put = sh_sub.add_parser(
        "put", help="create or replace a sheet from a JSON file of blocks "
        "(heading / text / table_view / spacer)",
    )
    sh_put.add_argument("name")
    sh_put.add_argument("--workbook", required=True)
    sh_put.add_argument("--spec", required=True,
                        help='JSON file — or the JSON itself — with {"blocks": [...]} '
                             '(the REST {"spec": {...}} wrapper is accepted) or a bare list of blocks')
    sh_get = sh_sub.add_parser("get", help="one sheet's blocks")
    sh_get.add_argument("name")
    sh_get.add_argument("--workbook", required=True)

    # transforms (authored logic) + the git bridge
    tr = sub.add_parser("transforms", help="authored logic (SQL / Python transforms)")
    tr_sub = tr.add_subparsers(dest="sub", required=True)
    tr_list = tr_sub.add_parser("list", help="every transform with its template and binding")
    tr_list.add_argument("--workbook", required=True)
    tr_new = tr_sub.add_parser(
        "create",
        help="author a derived table ({{ arg }} placeholders keep lineage traceable)",
    )
    tr_new.add_argument("name", help="transform label (lineage)")
    tr_new.add_argument("--workbook", required=True)
    tr_new.add_argument("--artifact-name", help="output table name (default: the label)")
    tr_new.add_argument("--kind", choices=["sql", "python"], default="sql")
    tr_new_src = tr_new.add_mutually_exclusive_group(required=True)
    tr_new_src.add_argument("--sql", help="template inline")
    tr_new_src.add_argument("--sql-file", help="path to the template file")
    tr_new.add_argument(
        "--arg", action="append", default=[], metavar="NAME=TABLE",
        help="bind a {{ NAME }} placeholder to an upstream table (repeatable)",
    )
    tr_new.add_argument("--layer", help="'raw' | 'staging' | 'marts' (optional)")
    ch = sub.add_parser("charts", help="charts (read-only: config + recipe)")
    ch_sub = ch.add_subparsers(dest="sub", required=True)
    ch_list = ch_sub.add_parser("list")
    ch_list.add_argument("--workbook", required=True)
    pl = sub.add_parser(
        "pull",
        help="workbook → DIR/{transforms,sheets,charts,data}/… + DIR/d2b.json",
    )
    pl.add_argument("--workbook", help="required the first time; then read from d2b.json")
    pl.add_argument("--workspace", metavar="WS",
                    help="a whole workspace: every workbook into DIR/workbooks/<title>--<id>/ "
                         "(required the first time; then read from the root d2b.json)")
    pl.add_argument("--dir", default=".", help="repo directory (default: .)")
    pl.add_argument("--force", action="store_true",
                    help="overwrite local edits instead of refusing")
    pl.add_argument("--data", metavar="TABLE[,TABLE…]",
                    help="also track these base tables as data/*.csv (export = branch)")
    pl.add_argument("--prune", action="store_true",
                    help="--workspace: delete the directories of workbooks that left the workspace")
    pl.add_argument("--jobs", type=int, default=6, metavar="N",
                    help="--workspace: workbooks pulled in parallel (default 6)")
    ps = sub.add_parser(
        "push",
        help="changed files → transforms re-run, sheets put, data 3-way merged",
    )
    ps.add_argument("--workbook", help="default: the one in d2b.json")
    ps.add_argument("--dir", default=".", help="repo directory (default: .)")
    ps.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    ps.add_argument("--force", action="store_true",
                    help="push even where the server changed since the last pull")
    ps.add_argument("--prune", action="store_true",
                    help="delete on the server what was deleted here (transform outputs, "
                         "sheets; needs workbooks:delete for artifacts). Data tables are only "
                         "untracked")
    ps.add_argument("--commit", metavar="LABEL",
                    help="afterwards commit a named version (e.g. the git sha)")
    st = sub.add_parser(
        "status",
        help="workspace repository: what the ledger, the directories and the server disagree on",
    )
    st.add_argument("--dir", default=".", help="repo directory (default: .)")
    st.add_argument("--strict", action="store_true",
                    help="exit 2 when anything is wrong (the CI guard)")
    st.add_argument("--offline", action="store_true", help="do not ask the server")
    sub.add_parser(
        "github-workflow",
        help="print a GitHub Actions workflow: push on merge to main, pull → PR on a schedule",
    )

    # versions
    vr = sub.add_parser("versions", help="named workbook commits")
    vr_sub = vr.add_subparsers(dest="sub", required=True)
    v_commit = vr_sub.add_parser("commit")
    v_commit.add_argument("label")
    v_commit.add_argument("--workbook", required=True)
    v_commit.add_argument("--summary")
    v_list = vr_sub.add_parser("list")
    v_list.add_argument("--workbook", required=True)
    v_rev = vr_sub.add_parser("revert")
    v_rev.add_argument("label")
    v_rev.add_argument("--workbook", required=True)

    # jobs
    jb = sub.add_parser("jobs", help="async-operation handles")
    jb_sub = jb.add_subparsers(dest="sub", required=True)
    j_get = jb_sub.add_parser("get")
    j_get.add_argument("job_id")
    j_wait = jb_sub.add_parser("wait")
    j_wait.add_argument("job_id")
    j_wait.add_argument("--timeout", type=float, default=600.0)

    _add_base_url_everywhere(p)
    return p


def _reject_deprecated_api_key(argv: list[str] | None, parser: argparse.ArgumentParser) -> None:
    """Reject deprecated argv API keys without echoing secret values."""
    args = sys.argv[1:] if argv is None else argv
    if any(arg == "--api-key" or arg.startswith("--api-key=") for arg in args):
        parser.error(
            "--api-key is no longer supported; set D2B_API_KEY in the environment instead"
        )


def _dispatch(client: D2BClient, args: argparse.Namespace) -> None:
    cmd, sub = args.cmd, getattr(args, "sub", None)

    if cmd == "whoami":
        _emit(client.request("GET", "/me"))
    elif cmd == "workspaces" and sub == "list":
        _emit(client.workspaces.list())
    elif cmd == "workbooks" and sub == "create":
        _emit(client.workbooks.create(title=args.title, workspace_id=args.workspace_id))
    elif cmd == "workbooks" and sub == "list":
        _emit(client.workbooks.list(workspace_id=args.workspace_id))
    elif cmd == "workbooks" and sub == "delete":
        client.workbooks.delete(args.id)
        _emit({"deleted": args.id})
    elif cmd == "transforms" and sub == "create":
        template = args.sql if args.sql is not None else _read_text_arg(args.sql_file, "--sql-file")
        bound_args: dict[str, str] = {}
        for pair in args.arg:
            key, sep, value = pair.partition("=")
            if not sep or not key or not value:
                print(f"error: --arg expects NAME=TABLE, got '{pair}'", file=sys.stderr)
                raise SystemExit(2)
            bound_args[key] = value
        _emit(client.transforms.create(
            args.workbook, name=args.name, kind=args.kind, template=template,
            artifact_name=args.artifact_name or args.name, args=bound_args,
            layer=args.layer,
        ))
    elif cmd == "track":
        _emit(client.file_links.track_local(
            args.workbook, args.file, origin=str(Path(args.file).resolve()),
            as_name=args.as_name, on_existing=args.on_existing,
        ))
    elif cmd == "watch":
        _watch(client, args)
    elif cmd == "upload":
        structuring = (
            "skip" if args.no_structuring
            else "defer" if args.defer_structuring
            else "auto" if args.structure
            else None  # server default: raw only
        )
        up_res = client.sources.upload(
            args.workbook, args.file, mode=args.mode, wait=args.wait,
            structuring=structuring,
        )
        result_body = up_res.get("result") if isinstance(up_res.get("result"), dict) else up_res
        verdict = result_body.get("structuring") if isinstance(result_body, dict) else None
        reason = result_body.get("structuring_reason") if isinstance(result_body, dict) else None
        if reason == "unchanged":
            print(
                "note: LLM structuring ran and found nothing to reshape (or every "
                "transform failed) — the raw tables are final; no credits charged.",
                file=sys.stderr,
            )
        elif reason == "building":
            print(
                "note: LLM structuring is still building for this content — retry "
                "the upload later to pick up the structured version.",
                file=sys.stderr,
            )
        elif verdict == "raw_fallback":
            print(
                "warning: LLM structuring did not run for this upload — raw tables "
                "were served (merged headers may be unresolved). Retry the upload "
                "later to pick up the structured version, or use --mode staged to "
                "control the parse spec yourself.",
                file=sys.stderr,
            )
        elif verdict == "off":
            # The server has upfront structuring disabled: the raw baseline is
            # the final answer, not a transient. Say so, or the caller only
            # finds out from the column names.
            print(
                "warning: this server has upfront LLM structuring disabled — the "
                "raw tables are final (merged headers stay unresolved, all columns "
                "text). Use --mode staged (analyze → parse-spec → materialize) "
                "to shape the data yourself.",
                file=sys.stderr,
            )
        _emit(up_res)
    elif cmd == "tables" and sub == "list":
        _emit(client.tables.list(args.workbook, include_archived=args.include_archived))
    elif cmd == "tables" and sub == "schema":
        if args.json_schema:
            _emit(client.tables.row_json_schema(args.workbook, args.name))
        else:
            _emit(client.tables.schema(args.workbook, args.name))
    elif cmd == "tables" and sub == "rows":
        _emit(client.tables.rows(
            args.workbook, args.name, limit=args.limit, offset=args.offset,
        ))
    elif cmd == "tables" and sub == "a1":
        _emit(client.tables.a1(args.workbook, args.name, args.range))
    elif cmd == "tables" and sub == "unarchive":
        _emit(client.tables.unarchive(args.workbook, args.name))
    elif cmd == "tables" and sub == "rename":
        _emit(client.tables.rename(args.workbook, args.name, args.new_name))
    elif cmd == "tables" and sub == "add-column":
        _emit(client.tables.add_column(
            args.workbook, args.name, args.column, args.type,
            default=args.default, expected_version=args.expected_version))
    elif cmd == "tables" and sub == "rename-column":
        _emit(client.tables.rename_column(
            args.workbook, args.name, args.column, args.new_name,
            expected_version=args.expected_version))
    elif cmd == "tables" and sub == "retype-column":
        _emit(client.tables.retype_column(
            args.workbook, args.name, args.column, args.type,
            expected_version=args.expected_version))
    elif cmd == "tables" and sub == "drop-column":
        _emit(client.tables.drop_column(
            args.workbook, args.name, args.column,
            expected_version=args.expected_version))
    elif cmd == "tables" and sub == "write-a1":
        import json as _json
        _emit(client.tables.write_a1(
            args.workbook, args.name, args.range, _json.loads(args.values),
            expected_version=args.expected_version))
    elif cmd == "tables" and sub == "set-formula":
        _emit(client.tables.set_formula(
            args.workbook, args.name, args.column, args.expr))
    elif cmd == "tables" and sub == "formulas":
        _emit(client.tables.formulas(args.workbook, args.name))
    elif cmd == "tables" and sub == "clear-formula":
        _emit(client.tables.clear_formula(args.workbook, args.name, args.column))
    elif cmd == "query":
        _emit(client.query.sql(args.workbook, args.sql, limit=args.limit))
    elif cmd == "export":
        tables = args.tables.split(",") if args.tables else None
        _write_binary(
            client.export.tables(args.workbook, tables=tables, format=args.format),
            args.out,
        )
    elif cmd == "sources" and sub == "analyze":
        _emit(client.sources.analyze(args.workbook, args.name))
    elif cmd == "sources" and sub == "parse-spec":
        if args.set_file:
            spec = _read_json_arg(args.set_file, "--set-file")
            regions = spec["regions"] if isinstance(spec, dict) else spec
            _emit(client.sources.update_parse_spec(args.workbook, args.name, regions))
        else:
            _emit(client.sources.get_parse_spec(args.workbook, args.name))
    elif cmd == "sources" and sub == "materialize":
        ids = args.region_ids.split(",") if args.region_ids else None
        _emit(client.sources.materialize(args.workbook, args.name, region_ids=ids))
    elif cmd == "sources" and sub == "list":
        _emit(client.sources.list(args.workbook))
    elif cmd == "sources" and sub == "download":
        _write_binary(
            client.sources.download_original(args.workbook, args.name), args.out,
        )
    elif cmd == "sources" and sub == "render":
        _write_binary(
            client.sources.render_template(args.workbook, args.name), args.out,
        )
    elif cmd == "sources" and sub == "revise":
        template = _read_text_arg(args.sql_file, "--sql-file") if args.sql_file else args.sql
        region: dict | None = None
        if args.region_id:
            region = {"region_id": args.region_id}
        elif args.range:
            region = {"range": args.range}
            if args.sheet:
                region["sheet"] = args.sheet
        _write_binary(
            client.sources.revise(
                args.workbook, args.name,
                transform={"name": args.transform_name, "template": template},
                region=region,
            ),
            args.out,
        )
    elif cmd == "sheets" and sub == "list":
        _emit(client.sheets.list(args.workbook))
    elif cmd == "sheets" and sub == "render":
        _write_binary(client.sheets.render(args.workbook, args.name), args.out)
    elif cmd == "sheets" and sub == "put":
        loaded = _read_json_arg(args.spec, "--spec")
        # The REST body is {"spec": {"blocks": [...]}}; a file saved from
        # that shape works here too, as does a bare list of blocks.
        if isinstance(loaded, dict) and isinstance(loaded.get("spec"), dict):
            loaded = loaded["spec"]
        blocks = loaded.get("blocks") if isinstance(loaded, dict) else loaded
        if not isinstance(blocks, list):
            raise UsageError(
                '--spec: expected {"blocks": [...]} or a list of blocks '
                "(heading / text / table_view / spacer)"
            )
        _emit(client.sheets.put(args.workbook, args.name, blocks))
    elif cmd == "sheets" and sub == "get":
        _emit(client.sheets.get(args.workbook, args.name))
    elif cmd == "transforms" and sub == "list":
        _emit(client.transforms.list(args.workbook))
    elif cmd == "charts" and sub == "list":
        _emit(client.charts.list(args.workbook))
    elif cmd == "pull":
        _cmd_pull(client, args)
    elif cmd == "push":
        _cmd_push(client, args)
    elif cmd == "status":
        _cmd_status(client, args)
    elif cmd == "versions" and sub == "commit":
        _emit(client.versions.commit(args.workbook, args.label, summary=args.summary))
    elif cmd == "versions" and sub == "list":
        _emit(client.versions.list(args.workbook))
    elif cmd == "versions" and sub == "revert":
        _emit(client.versions.revert(args.workbook, args.label))
    elif cmd == "jobs" and sub == "get":
        _emit(client.jobs.get(args.job_id))
    elif cmd == "jobs" and sub == "wait":
        _emit(client.jobs.wait(args.job_id, timeout=args.timeout))
    else:  # pragma: no cover — argparse enforces the matrix
        raise SystemExit(2)


def _cmd_pull(client: D2BClient, args: argparse.Namespace) -> None:
    root = Path(args.dir)
    # A workspace repository (root ledger, or --workspace the first time)
    # pulls every workbook of the workspace; otherwise the directory is one
    # workbook, as before.
    if args.workspace or load_ledger(root) is not None:
        if args.workbook:
            raise SyncError("--workbook and --workspace do not combine: a workspace repository "
                            "takes every workbook of the workspace.")
        if args.data:
            raise SyncError("--data is per workbook: run `d2b pull --data TABLE` inside "
                            f"{WORKBOOKS_DIR}/<dir>/ (its d2b.json remembers the table).")
        report = pull_workspace(
            client, root, args.workspace, force=args.force, prune=args.prune, jobs=args.jobs,
        )
        _emit(report)
        if report["counts"]["refused"]:
            raise SystemExit(1)
        return
    # The workbook id must be known before the round trip; the manifest
    # supplies it after the first pull.
    wb = resolve_workbook(load_manifest(root), args.workbook)
    tables = [t.strip() for t in (args.data or "").split(",") if t.strip()]
    _emit(pull(client, root, wb, force=args.force, data_tables=tables))


def _cmd_status(client: D2BClient, args: argparse.Namespace) -> None:
    report = status_workspace(client, Path(args.dir), offline=args.offline)
    _emit(report)
    if args.strict and not report["clean"]:
        raise SystemExit(2)


def _cmd_push(client: D2BClient, args: argparse.Namespace) -> None:
    root = Path(args.dir)
    if load_ledger(root) is not None:
        if args.workbook:
            raise SyncError("--workbook does not apply to a workspace repository; run `d2b push` "
                            f"inside {WORKBOOKS_DIR}/<dir>/ for one workbook.")
        report = push_workspace(
            client, root, dry_run=args.dry_run, force=args.force, prune=args.prune, commit=args.commit,
        )
        _emit(report)
        if report["counts"]["refused"] or report["counts"]["error"]:
            raise SystemExit(1)
        return
    manifest = load_manifest(root)
    if manifest is None:
        raise SyncError(f"no {MANIFEST} in {root} — run `d2b pull --workbook WB` first.")
    wb = resolve_workbook(manifest, args.workbook)
    plan = plan_push(client, root, wb, force=args.force, prune=args.prune)
    out: dict[str, Any] = {**plan.as_dict(), "dry_run": args.dry_run}
    if args.dry_run:
        _emit(out)
        return
    pushed = execute_push(client, root, plan)
    for section in ("transforms", "sheets", "data"):
        out[section].pop("to_push", None)
        out[section]["pushed"] = pushed[section]
    if args.prune:
        out["prune"] = {"pruned": pushed["pruned"], "errors": pushed["prune_errors"]}
    if args.commit:
        n = sum(len(pushed[s]) for s in ("transforms", "sheets", "data"))
        out["version"] = client.versions.commit(
            wb, args.commit, summary=f"d2b push: {n} change(s)",
        )
    _emit(out)


def _cmd_login(args: argparse.Namespace) -> int:
    # `pipx install d2b-sdk && d2b login` has to reach the public service with
    # nothing else set; --base-url / $D2B_BASE_URL are for other deployments.
    base_url = (
        args.base_url or os.environ.get("D2B_BASE_URL") or DEFAULT_PUBLIC_BASE_URL
    ).rstrip("/")
    scopes = [s.strip() for s in (args.scopes or "").split(",") if s.strip()] or None
    with httpx.Client(base_url=base_url, timeout=30.0) as http:
        payload = login_flow(
            base_url, http, name=args.name, open_browser=not args.no_browser,
            scopes=scopes,
        )
    tokens = payload.get("tokens") or [{
        "token": payload["token"], "token_id": payload.get("token_id"),
        "account_id": payload.get("account_id"), "is_default": True,
        "expires_at": payload.get("expires_at"),
    }]
    accounts = {
        t["account_id"] or "default": {
            "token": t["token"], "token_id": t.get("token_id"),
            "name": t.get("account_name"), "expires_at": t.get("expires_at"),
        }
        for t in tokens
    }
    default_account = next(
        (t["account_id"] for t in tokens if t.get("is_default") and t.get("account_id")),
        tokens[0].get("account_id") or "default",
    )
    creds = _load_credentials()
    creds[base_url] = {
        # First credential kept flat for tools reading the old shape.
        "token": tokens[0]["token"],
        "token_id": tokens[0].get("token_id"),
        "expires_at": tokens[0].get("expires_at"),
        "accounts": accounts,
        "default_account": default_account,
    }
    _save_credentials(creds)
    _emit({
        "logged_in": base_url,
        "scopes": payload.get("scopes"),
        "expires_at": payload.get("expires_at"),
        "accounts": [
            {"id": t.get("account_id"), "name": t.get("account_name"),
             "is_default": bool(t.get("is_default"))}
            for t in tokens
        ],
    })
    return 0


def _cmd_logout(args: argparse.Namespace) -> int:
    creds = _load_credentials()
    base_url = (args.base_url or os.environ.get("D2B_BASE_URL") or "").rstrip("/")
    targets = [base_url] if base_url else list(creds)
    removed = []
    for url in targets:
        entry = creds.pop(url, None)
        if not entry:
            continue
        removed.append(url)
        # Best-effort server-side revocation of every credential the login
        # holds — forgetting locally is not the same as the credential dying.
        held = list((entry.get("accounts") or {}).values()) or [entry]
        for cred in held:
            if not cred.get("token_id"):
                continue
            try:
                client = D2BClient(api_key=cred["token"], base_url=url)
                client.request("DELETE", f"/me/tokens/{cred['token_id']}")
                client.close()
            except Exception:
                pass
    _save_credentials(creds)
    _emit({"logged_out": removed})
    return 0


def _watch(client: D2BClient, args: argparse.Namespace) -> None:
    """Poll the files' mtime/size; on a change push the bytes as a version.
    Tracks a file first when the workbook has no link for it. Cheap enough
    to leave running; the server dedupes identical bytes."""
    import hashlib
    import socket
    import time

    paths = [Path(p) for p in args.files]
    for p in paths:
        if not p.exists() or p.is_dir():
            raise SystemExit(f"error: file not found: {p}")
    links = {l["sourceName"]: l for l in client.file_links.list(args.workbook) if l.get("provider") == "local"}
    origin = f"{socket.gethostname()}"
    state: dict[Path, tuple[float, int, str]] = {}
    for p in paths:
        content = p.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        link = links.get(p.name)
        if link is None:
            link = client.file_links.track_local(
                args.workbook, content, filename=p.name, origin=str(p.resolve()),
                on_existing=args.on_existing,
            )
            links[p.name] = link
            _emit({"tracked": p.name, "link_id": link["id"], "version": 1})
        else:
            out = client.file_links.push(args.workbook, link["id"], content, filename=p.name, modified_by=origin)
            if out.get("changed"):
                _emit({"pushed": p.name, "version": out["version"]["n"]})
        st = p.stat()
        state[p] = (st.st_mtime, st.st_size, digest)
    if args.once:
        return
    print(f"watching {len(paths)} file(s) every {args.interval}s — Ctrl-C to stop", file=sys.stderr)
    try:
        while True:
            time.sleep(args.interval)
            for p in paths:
                try:
                    st = p.stat()
                except FileNotFoundError:
                    continue  # mid-save rename; next tick
                mtime, size, digest = state[p]
                if st.st_mtime == mtime and st.st_size == size:
                    continue
                try:
                    content = p.read_bytes()
                except OSError:
                    continue  # still being written
                new_digest = hashlib.sha256(content).hexdigest()
                state[p] = (st.st_mtime, st.st_size, new_digest)
                if new_digest == digest:
                    continue
                try:
                    out = client.file_links.push(
                        args.workbook, links[p.name]["id"], content, filename=p.name, modified_by=origin,
                    )
                except D2BError as exc:
                    print(f"error: {exc.cli_message()}", file=sys.stderr)
                    continue
                if out.get("changed"):
                    _emit({"pushed": p.name, "version": out["version"]["n"]})
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)


class UsageError(Exception):
    """A local mistake caught before (or instead of) a server round-trip:
    main() prints it as one `error:` line on stderr and exits 2 — the CLI
    contract (docs cli.md) — never a raw traceback with internal paths."""


def _read_text_arg(value: str, what: str) -> str:
    """The contents of a file argument (--sql-file ...), or a UsageError."""
    path = Path(value)
    try:
        if path.is_dir():
            raise UsageError(f"{what}: {value} is a directory — pass a file")
        if not path.is_file():
            raise UsageError(f"file not found: {value}")
        return path.read_text(encoding="utf-8")
    except OSError as exc:  # ENAMETOOLONG for a pasted document, EACCES, ...
        raise UsageError(f"{what}: cannot read {value[:80]!r}: {exc.strerror}") from exc


def _read_json_arg(value: str, what: str) -> Any:
    """A JSON file argument (--spec, --set-file) — or the JSON itself,
    pasted inline: a value that starts with `{` or `[` is parsed as JSON,
    not treated as a file name (an inline object used to surface as
    OSError 63 "File name too long", DX report 2026-09-09)."""
    text = value.strip()
    inline = text.startswith(("{", "["))
    if not inline:
        text = _read_text_arg(value, what)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        where = "the inline value" if inline else value
        raise UsageError(
            f"{what}: {where} is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})"
        ) from exc


def _validate_upload_args(args: argparse.Namespace) -> str | None:
    """Catch the classic local mistakes before any server round-trip.
    The CLI contract (docs cli.md) promises a one-line stderr message and
    exit 2 — never a raw traceback with internal paths (DX report §4)."""
    path = Path(args.file)
    if not path.exists():
        return f"file not found: {args.file}"
    if path.is_dir():
        return (
            f"{args.file} is a directory — pass a single file "
            "(CSV / Excel / Parquet / JSON)."
        )
    if args.mode == "staged" and args.wait:
        return (
            "--wait only applies to --mode auto (staged uploads land "
            "synchronously — drop --wait)."
        )
    if args.mode == "staged" and (args.no_structuring or args.defer_structuring or args.structure):
        return (
            "--no-structuring / --defer-structuring only apply to --mode auto "
            "(staged never structures)."
        )
    return None


def main(argv: list[str] | None = None, *, client: D2BClient | None = None) -> int:
    """Entry point. ``client`` is injectable for tests (the production
    path builds one from saved login / env / flags)."""
    parser = _parser()
    _reject_deprecated_api_key(argv, parser)
    args = parser.parse_args(argv)
    if args.cmd == "login":
        return _cmd_login(args)
    if args.cmd == "logout":
        return _cmd_logout(args)
    if args.cmd == "init":
        return _cmd_init(args)
    if args.cmd == "github-workflow":
        sys.stdout.write(GITHUB_WORKFLOW)
        return 0
    if args.cmd == "upload":
        usage_error = _validate_upload_args(args)
        if usage_error is not None:
            print(f"error: {usage_error}", file=sys.stderr)
            return 2
    own = client is None
    c = client or _build_client(args)
    try:
        _dispatch(c, args)
        return 0
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except SyncError as exc:
        # Refused pull/push: the report (conflicting files etc.) is the
        # JSON on stdout, the one-line reason goes to stderr.
        if exc.details:
            _emit(exc.details)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except D2BError as exc:
        # The suggested_fix travels to stderr — a shell-driving agent
        # reads it the same way an HTTP-driving one does, and the server's
        # CLI-vocabulary variant wins on this surface.
        print(f"error: {exc.cli_message()}", file=sys.stderr)
        if exc.status == 401 and not os.environ.get("D2B_API_KEY"):
            print(
                "hint: the saved login may have expired or been revoked — "
                "run `d2b login` again.",
                file=sys.stderr,
            )
        return 1
    finally:
        if own:
            c.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
