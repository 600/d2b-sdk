"""--base-url must work in BOTH positions: before the subcommand (argparse's
native root-option placement) and after it (`d2b login --base-url URL`, the
form the docs and the CLI's own error message teach)."""
import json

import pytest
from d2b.cli import _parser


@pytest.mark.parametrize("argv", [
    ["--base-url", "http://x", "login"],
    ["login", "--base-url", "http://x"],
    ["workbooks", "list", "--base-url", "http://x"],
    ["tables", "rows", "t", "--workbook", "w", "--base-url", "http://x"],
])
def test_base_url_accepted_in_any_position(argv):
    args = _parser().parse_args(argv)
    assert args.base_url == "http://x"


def test_subcommand_position_wins_and_root_value_survives():
    # Both given: the later (subcommand) one wins.
    args = _parser().parse_args(["--base-url", "http://root", "login",
                                 "--base-url", "http://sub"])
    assert args.base_url == "http://sub"
    # Only the root position given: the subcommand parse must not clobber
    # it back to a default (the SUPPRESS contract).
    args = _parser().parse_args(["--base-url", "http://root", "login"])
    assert args.base_url == "http://root"
    # Neither: root default None still present on the namespace.
    assert _parser().parse_args(["whoami"]).base_url is None


class TestUploadLocalGuards:
    """`d2b upload` local-mistake paths: one-line stderr + exit 2, never a
    raw traceback (docs cli.md contract; DX report 2026-08-29 §4)."""

    def _run(self, argv, capsys):
        from d2b.cli import main

        rc = main(argv)
        err = capsys.readouterr().err
        assert "Traceback" not in err
        return rc, err

    def test_missing_file_is_exit_2(self, capsys):
        rc, err = self._run(
            ["upload", "/no/such/file.xlsx", "--workbook", "w"], capsys,
        )
        assert rc == 2
        assert "file not found: /no/such/file.xlsx" in err

    def test_directory_is_exit_2(self, tmp_path, capsys):
        rc, err = self._run(
            ["upload", str(tmp_path), "--workbook", "w"], capsys,
        )
        assert rc == 2
        assert "is a directory" in err

    def test_staged_plus_wait_rejected_before_any_request(self, tmp_path, capsys):
        f = tmp_path / "x.csv"
        f.write_text("a,b\n1,2\n")
        rc, err = self._run(
            ["upload", str(f), "--workbook", "w", "--mode", "staged", "--wait"],
            capsys,
        )
        assert rc == 2
        assert "--wait only applies to --mode auto" in err


class TestStructuringFlags:
    def test_flags_are_mutually_exclusive(self, capsys):
        from d2b.cli import _parser

        with pytest.raises(SystemExit):
            _parser().parse_args([
                "upload", "x.csv", "--workbook", "w",
                "--no-structuring", "--defer-structuring", "--structure",
            ])

    def test_staged_plus_structuring_flag_is_exit_2(self, tmp_path, capsys):
        from d2b.cli import main

        f = tmp_path / "x.csv"
        f.write_text("a\n1\n")
        rc = main(["upload", str(f), "--workbook", "w",
                   "--mode", "staged", "--no-structuring"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "only apply to --mode auto" in err


def test_track_and_watch_parse():
    args = _parser().parse_args(["track", "sales.xlsx", "--workbook", "wb"])
    assert args.cmd == "track" and args.file == "sales.xlsx" and args.workbook == "wb"
    args = _parser().parse_args(["watch", "a.xlsx", "b.xlsx", "--workbook", "wb", "--interval", "5", "--once"])
    assert args.cmd == "watch" and args.files == ["a.xlsx", "b.xlsx"]
    assert args.interval == 5.0 and args.once is True


def test_login_scopes_and_account_flags_parse():
    args = _parser().parse_args(["login", "--base-url", "http://x", "--scopes",
                                 "workbooks:read,workbooks:write,workbooks:delete"])
    assert args.scopes == "workbooks:read,workbooks:write,workbooks:delete"
    args = _parser().parse_args(["--account", "acc-dev", "whoami"])
    assert args.account == "acc-dev"
    args = _parser().parse_args(["workspaces", "list"])
    assert args.cmd == "workspaces" and args.sub == "list"


class TestPerAccountCredentials:
    """One token per approved account (2026-09-05): the saved login keeps a
    credential per account, `--account` / D2B_ACCOUNT_ID picks one, and the
    default account's token is used otherwise."""

    def _entry(self):
        return {
            "token": "d2b_pat_default", "token_id": "t1",
            "accounts": {
                "acc-default": {"token": "d2b_pat_default", "token_id": "t1"},
                "acc-dev": {"token": "d2b_pat_dev", "token_id": "t2"},
            },
            "default_account": "acc-default",
        }

    def test_default_account_without_selector(self):
        from d2b.cli import _entry_token

        assert _entry_token(self._entry(), None) == "d2b_pat_default"

    def test_selector_picks_that_accounts_token(self):
        from d2b.cli import _entry_token

        assert _entry_token(self._entry(), "acc-dev") == "d2b_pat_dev"
        assert _entry_token(self._entry(), "acc-unknown") is None

    def test_legacy_single_token_login_ignores_selector(self):
        from d2b.cli import _entry_token

        legacy = {"token": "d2b_pat_old", "token_id": "t0"}
        assert _entry_token(legacy, None) == "d2b_pat_old"
        assert _entry_token(legacy, "acc-dev") is None

    def test_env_selector_is_honoured(self, tmp_path, monkeypatch):
        import json

        from d2b.cli import _parser, _resolve_auth

        monkeypatch.setenv("D2B_CONFIG_DIR", str(tmp_path))
        monkeypatch.delenv("D2B_API_KEY", raising=False)
        monkeypatch.delenv("D2B_BASE_URL", raising=False)
        (tmp_path / "credentials.json").write_text(json.dumps({"http://x": self._entry()}))
        monkeypatch.setenv("D2B_ACCOUNT_ID", "acc-dev")
        assert _resolve_auth(_parser().parse_args(["whoami"])) == ("d2b_pat_dev", "http://x")
        monkeypatch.delenv("D2B_ACCOUNT_ID")
        assert _resolve_auth(_parser().parse_args(["whoami"])) == ("d2b_pat_default", "http://x")


class TestVersionAndDefaults:
    """Two things a freshly installed CLI owes its user: it can say what it
    is, and `d2b login` reaches the public service with nothing else set."""

    def test_version_flag_prints_the_single_source(self, capsys):
        from d2b import __version__
        from d2b.cli import _parser

        with pytest.raises(SystemExit) as exc:
            _parser().parse_args(["--version"])
        assert exc.value.code == 0
        assert capsys.readouterr().out.strip() == f"d2b {__version__}"

    def test_client_tags_follow_the_version(self):
        """The version lives in one file; the attribution headers must not
        carry a second copy that silently goes stale."""
        from d2b import __version__
        from d2b import client as client_mod

        # cli.py rewrites the tag at import time — importing it here is what
        # makes that assignment run.
        from d2b import cli  # noqa: F401

        assert client_mod.CLIENT_TAG == f"d2b-cli/{__version__}"

    def test_login_defaults_to_the_public_service(self, monkeypatch, tmp_path):
        from d2b import cli

        monkeypatch.delenv("D2B_BASE_URL", raising=False)
        monkeypatch.setenv("D2B_CONFIG_DIR", str(tmp_path))  # never touch ~/.config
        seen = {}

        def fake_flow(base_url, http, **kw):
            seen["base_url"] = base_url
            return {"token": "d2b_pat_x", "token_id": "t1", "account_id": "a1"}

        monkeypatch.setattr(cli, "login_flow", fake_flow)
        assert cli.main(["login"]) == 0
        assert seen["base_url"] == cli.DEFAULT_PUBLIC_BASE_URL


class _Recorder:
    """A stand-in client: records the call main() dispatches to it."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.sheets = self
        self.transforms = self

    def put(self, *a, **kw):
        self.calls.append(("put", a, kw))
        return {"ok": True}

    def create(self, *a, **kw):
        self.calls.append(("create", a, kw))
        return {"ok": True}


class TestFileArguments:
    """--spec / --set-file / --sql-file: a missing file, a pasted document
    or malformed JSON is a one-line `error:` on stderr and exit 2 — never a
    traceback (bug report 2026-09-09: an inline --spec value was opened as
    a file name and surfaced OSError 63 "File name too long")."""

    def _run(self, argv, capsys):
        from d2b.cli import main

        rec = _Recorder()
        rc = main(argv, client=rec)
        out = capsys.readouterr()
        assert "Traceback" not in out.err
        return rc, out.err, rec

    def test_spec_accepts_inline_json(self, capsys):
        blocks = [{"kind": "heading", "text": "Monthly"}]
        rc, err, rec = self._run(
            ["sheets", "put", "月次レポート", "--workbook", "w", "--spec", json.dumps({"blocks": blocks})],
            capsys,
        )
        assert rc == 0 and err == ""
        assert rec.calls == [("put", ("w", "月次レポート", blocks), {})]

    def test_spec_accepts_rest_wrapper_and_bare_list(self, tmp_path, capsys):
        blocks = [{"kind": "table_view", "table": "sales"}]
        wrapped = tmp_path / "sheet.json"
        wrapped.write_text(json.dumps({"spec": {"blocks": blocks}}))
        rc, _, rec = self._run(["sheets", "put", "r", "--workbook", "w", "--spec", str(wrapped)], capsys)
        assert rc == 0 and rec.calls[0][1] == ("w", "r", blocks)
        rc, _, rec = self._run(["sheets", "put", "r", "--workbook", "w", "--spec", json.dumps(blocks)], capsys)
        assert rc == 0 and rec.calls[0][1] == ("w", "r", blocks)

    def test_spec_missing_file_is_one_line_exit_2(self, capsys):
        rc, err, rec = self._run(["sheets", "put", "r", "--workbook", "w", "--spec", "/no/such/sheet.json"], capsys)
        assert rc == 2 and rec.calls == []
        assert err.strip() == "error: file not found: /no/such/sheet.json"

    def test_spec_pasted_document_is_one_line_exit_2(self, capsys):
        # Not JSON, not a path: a long pasted paragraph used to raise
        # ENAMETOOLONG from Path.read_text as a raw traceback.
        rc, err, _ = self._run(["sheets", "put", "r", "--workbook", "w", "--spec", "blocks: heading " * 40], capsys)
        assert rc == 2
        assert err.startswith("error: ") and len(err.strip().splitlines()) == 1

    def test_spec_invalid_json_and_wrong_shape(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text('{"blocks": [')
        rc, err, _ = self._run(["sheets", "put", "r", "--workbook", "w", "--spec", str(bad)], capsys)
        assert rc == 2 and "is not valid JSON" in err and str(bad) in err
        rc, err, _ = self._run(["sheets", "put", "r", "--workbook", "w", "--spec", '{"title": "x"}'], capsys)
        assert rc == 2 and 'expected {"blocks": [...]}' in err

    def test_sql_file_missing_is_one_line_exit_2(self, capsys):
        rc, err, rec = self._run(
            ["transforms", "create", "agg", "--workbook", "w", "--artifact-name", "a", "--sql-file", "/no/such.sql"],
            capsys,
        )
        assert rc == 2 and rec.calls == []
        assert err.strip() == "error: file not found: /no/such.sql"
