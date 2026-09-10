"""``d2b init`` is host-agnostic: the AGENTS.md section + the canonical entry
by default, any file via --config, known hosts as a table — never a token."""
from __future__ import annotations

import json
import tomllib

from d2b.cli import INIT_HOSTS, main


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def test_default_writes_snippet_and_prints_the_canonical_entry(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("D2B_API_KEY", raising=False)
    monkeypatch.delenv("D2B_BASE_URL", raising=False)
    assert main(["init", "--dir", str(tmp_path)]) == 0
    out = _out(capsys)
    assert out["server"] == {"mcpServers": {"d2b": {
        "type": "http", "url": "https://d2b.dev/mcp/",
        "headers": {"Authorization": "Bearer ${D2B_API_KEY}"},
    }}}
    assert out["wrote"] == ["AGENTS.md"] and out["skipped"] == []
    assert (tmp_path / "AGENTS.md").read_text().startswith("## D2B")
    assert not (tmp_path / ".mcp.json").exists()


def test_config_path_merges_the_mcpservers_shape_anywhere(tmp_path, capsys):
    target = tmp_path / "tools" / "windsurf.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    assert main(["init", "--dir", str(tmp_path), "--config", "tools/windsurf.json",
                 "--no-snippet"]) == 0
    cfg = json.loads(target.read_text())
    assert set(cfg["mcpServers"]) == {"other", "d2b"}
    assert _out(capsys)["wrote"] == ["tools/windsurf.json"]


def test_known_hosts_are_a_table(tmp_path, capsys):
    assert main(["init", "--dir", str(tmp_path), "--host", "claude-code", "--host", "vscode",
                 "--no-snippet"]) == 0
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["d2b"]["url"] == "https://d2b.dev/mcp/"
    vs = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())
    assert vs["servers"]["d2b"]["headers"]["Authorization"] == "Bearer ${input:d2b_pat}"
    assert [i["id"] for i in vs["inputs"]] == ["d2b_pat"]
    assert _out(capsys)["wrote"] == [".mcp.json", ".vscode/mcp.json"]
    assert {"claude-code", "cursor", "vscode", "codex"} <= set(INIT_HOSTS)


def test_codex_toml_is_replaced_not_duplicated(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('model = "o3"\n\n[mcp_servers.d2b]\nurl = "https://old.example/mcp/"\n\n[mcp_servers.other]\ncommand = "x"\n')
    monkeypatch.setenv("D2B_BASE_URL", "https://staging.example.com")
    assert main(["init", "--dir", str(tmp_path), "--host", "codex", "--no-snippet"]) == 0
    data = tomllib.loads(cfg.read_text())
    assert data["model"] == "o3" and "other" in data["mcp_servers"]
    assert data["mcp_servers"]["d2b"] == {"url": "https://staging.example.com/mcp/",
                                          "bearer_token_env_var": "D2B_API_KEY"}
    assert cfg.read_text().count("[mcp_servers.d2b]") == 1
    assert _out(capsys)["wrote"] == [".codex/config.toml"]  # HOME is the repo root here


def test_codex_toml_preserves_indented_tables_after_replacement(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[mcp_servers.d2b]\nurl = "https://old.example/mcp/"\n\n'
        '  [mcp_servers.other]\ncommand = "preserve-me"\n\n'
        '  [shell_environment_policy]\ninherit = "all"\n'
    )

    assert main(["init", "--dir", str(tmp_path), "--config", "config.toml",
                 "--no-snippet"]) == 0

    data = tomllib.loads(cfg.read_text())
    assert data["mcp_servers"]["other"] == {"command": "preserve-me"}
    assert data["shell_environment_policy"] == {"inherit": "all"}
    assert cfg.read_text().count("[mcp_servers.d2b]") == 1
    assert _out(capsys)["wrote"] == ["config.toml"]


def test_never_writes_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("D2B_API_KEY", "d2b_pat_supersecret")
    assert main(["init", "--dir", str(tmp_path), "--host", "cursor",
                 "--config", "x.toml", "--no-snippet"]) == 0
    for p in (tmp_path / ".cursor" / "mcp.json", tmp_path / "x.toml"):
        assert "supersecret" not in p.read_text()


def test_snippet_added_once_and_invalid_json_refused(tmp_path, capsys):
    (tmp_path / "CLAUDE.md").write_text("# Project\n")
    assert main(["init", "--dir", str(tmp_path), "--snippet-file", "CLAUDE.md"]) == 0
    assert main(["init", "--dir", str(tmp_path), "--snippet-file", "CLAUDE.md"]) == 0
    assert (tmp_path / "CLAUDE.md").read_text().count("## D2B") == 1
    capsys.readouterr()
    (tmp_path / ".mcp.json").write_text("{not json")
    assert main(["init", "--dir", str(tmp_path), "--host", "claude-code"]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_init_rejects_paths_outside_the_repository(tmp_path, capsys):
    outside = tmp_path.parent / "outside.md"
    assert main(["init", "--dir", str(tmp_path), "--snippet-file", "../outside.md"]) == 2
    assert "escapes" in capsys.readouterr().err
    assert not outside.exists()

    assert main(["init", "--dir", str(tmp_path), "--config", str(outside),
                 "--no-snippet"]) == 2
    assert "must be relative" in capsys.readouterr().err
    assert not outside.exists()


def test_init_rejects_symlinked_outputs_and_parents(tmp_path, capsys):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("unchanged")
    (tmp_path / "AGENTS.md").symlink_to(outside)
    assert main(["init", "--dir", str(tmp_path)]) == 2
    assert "symbolic link" in capsys.readouterr().err
    assert outside.read_text() == "unchanged"

    (tmp_path / "AGENTS.md").unlink()
    outside_dir = tmp_path.parent / "outside-config"
    outside_dir.mkdir()
    (tmp_path / ".cursor").symlink_to(outside_dir, target_is_directory=True)
    assert main(["init", "--dir", str(tmp_path), "--host", "cursor",
                 "--no-snippet"]) == 2
    assert "symbolic link" in capsys.readouterr().err
    assert not (outside_dir / "mcp.json").exists()
