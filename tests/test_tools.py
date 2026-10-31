import os
from pathlib import Path

from strand.config import Config, load_config
from strand.tools.workspace import (
    MAX_READ_BYTES,
    MAX_READ_LINES,
    WORKSPACE_TOOLS,
    dispatch_tool_call,
    is_safe_path,
    list_directory,
    read_file,
)


def _root(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


def test_config_defaults_and_overrides(tmp_path):
    cfg = Config()
    assert cfg.workspace_roots == ["~/Projects"] and cfg.tools_enabled is True
    assert cfg.resolved_workspace_roots() == [Path("~/Projects").expanduser().resolve()]
    env = {"STRAND_WORKSPACE_ROOTS": f"{tmp_path}{os.pathsep}/tmp", "STRAND_TOOLS_ENABLED": "false"}
    cfg = load_config(env, tmp_path / "none.json")
    assert cfg.workspace_roots == [str(tmp_path), "/tmp"] and cfg.tools_enabled is False
    path = tmp_path / "c.json"
    path.write_text('{"workspace_roots": ["~/x"], "tools_enabled": false}')
    cfg = load_config({}, path)
    assert cfg.workspace_roots == ["~/x"] and cfg.tools_enabled is False


def test_resolved_roots_follow_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert Config(workspace_roots=[str(link)]).resolved_workspace_roots() == [real.resolve()]


def test_traversal_and_outside_denied(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "secret.txt").write_text("x")
    for target in ("../../etc/passwd", "~/.ssh", str(root / ".." / "secret.txt"), "/etc/passwd", ""):
        ok, reason = is_safe_path(target, [root])
        assert ok is False and isinstance(reason, str)
        assert read_file(target, [root])["error"] == "access_denied"
        assert list_directory(target, [root])["error"] == "access_denied"


def test_symlink_escape_denied(tmp_path):
    root = _root(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "evil").symlink_to(outside)
    assert read_file(str(root / "evil"), [root])["error"] == "access_denied"


def test_sibling_prefix_not_confused(tmp_path):
    root = _root(tmp_path)
    sibling = tmp_path / "ws-evil"
    sibling.mkdir()
    assert is_safe_path(sibling, [root])[0] is False


def test_read_valid_file(tmp_path):
    root = _root(tmp_path)
    (root / "a.txt").write_text("one\ntwo\nthree\n")
    ok, resolved = is_safe_path(root / "a.txt", [root])
    assert ok and resolved == root / "a.txt"
    result = read_file(str(root / "a.txt"), [root])
    assert result["content"] == "one\ntwo\nthree\n"
    assert result["truncated"] is False and result["next_offset"] is None


def test_pagination(tmp_path):
    root = _root(tmp_path)
    (root / "n.txt").write_text("".join(f"l{i}\n" for i in range(1, 11)))
    page = read_file(str(root / "n.txt"), [root], offset=3, limit=2)
    assert page["content"] == "l3\nl4\n" and page["truncated"] is True and page["next_offset"] == 5
    last = read_file(str(root / "n.txt"), [root], offset=9, limit=5)
    assert last["content"] == "l9\nl10\n" and last["truncated"] is False
    assert read_file(str(root / "n.txt"), [root], offset=50)["content"] == ""
    assert read_file(str(root / "n.txt"), [root], offset=0)["error"] == "invalid_arguments"
    assert read_file(str(root / "n.txt"), [root], limit="x")["error"] == "invalid_arguments"


def test_line_cap(tmp_path):
    root = _root(tmp_path)
    (root / "big.txt").write_text("a\n" * 1000)
    result = read_file(str(root / "big.txt"), [root], limit=10_000)
    assert result["lines_returned"] == MAX_READ_LINES and result["truncated"] is True


def test_byte_cap(tmp_path):
    root = _root(tmp_path)
    (root / "wide.txt").write_text(("x" * 99 + "\n") * 150)
    result = read_file(str(root / "wide.txt"), [root], limit=200)
    assert len(result["content"].encode()) <= MAX_READ_BYTES
    assert result["byte_truncated"] is True and result["truncated"] is True
    (root / "one.txt").write_text("y" * 50_000)
    single = read_file(str(root / "one.txt"), [root])
    assert len(single["content"]) == MAX_READ_BYTES and single["byte_truncated"] is True


def test_read_errors(tmp_path):
    root = _root(tmp_path)
    (root / "d").mkdir()
    (root / "bin").write_bytes(b"ab\x00cd")
    assert read_file(str(root / "missing"), [root])["error"] == "not_found"
    assert read_file(str(root / "d"), [root])["error"] == "not_a_file"
    assert read_file(str(root / "bin"), [root])["error"] == "binary_file"
    assert read_file(123, [root])["error"] == "invalid_arguments"  # type: ignore[arg-type]


def test_list_directory_sorted_and_typed(tmp_path):
    root = _root(tmp_path)
    (root / "b.txt").write_text("12345")
    (root / "a").mkdir()
    (root / "C.txt").write_text("")
    result = list_directory(str(root), [root])
    assert [e["name"] for e in result["entries"]] == ["C.txt", "a", "b.txt"]
    assert result["entries"][1] == {"name": "a", "type": "directory", "size": 0}
    assert result["entries"][2] == {"name": "b.txt", "type": "file", "size": 5}
    assert result == list_directory(str(root), [root])


def test_list_directory_errors(tmp_path):
    root = _root(tmp_path)
    (root / "f").write_text("x")
    assert list_directory(str(root / "nope"), [root])["error"] == "not_found"
    assert list_directory(str(root / "f"), [root])["error"] == "not_a_directory"


def test_schemas_and_dispatch(tmp_path):
    root = _root(tmp_path)
    (root / "f.txt").write_text("hi\n")
    names = {t["function"]["name"] for t in WORKSPACE_TOOLS}
    assert names == {"read_file", "list_directory"}
    assert all(t["type"] == "function" for t in WORKSPACE_TOOLS)
    assert dispatch_tool_call("read_file", {"path": str(root / "f.txt")}, [root])["content"] == "hi\n"
    assert dispatch_tool_call("list_directory", {"path": str(root), "junk": 1}, [root])["entries"]
    assert dispatch_tool_call("rm", {"path": "x"}, [root])["error"] == "unknown_tool"
    assert dispatch_tool_call("read_file", {}, [root])["error"] == "invalid_arguments"
    assert dispatch_tool_call("read_file", "no", [root])["error"] == "invalid_arguments"  # type: ignore[arg-type]
    assert dispatch_tool_call("read_file", {"path": "~/.ssh"}, [root])["error"] == "access_denied"



def test_tilde_path_resolves_inside_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "Projects"
    root.mkdir(parents=True)
    (root / "f.py").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    ok, resolved = is_safe_path("~/Projects/f.py", [root.resolve()])
    assert ok and resolved == (root / "f.py").resolve()
    assert is_safe_path("~/other/f.py", [root.resolve()])[0] is False


def test_relative_path_resolves_against_root_not_cwd(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "file.py").write_text("x")
    monkeypatch.chdir(tmp_path.parent)
    ok, resolved = is_safe_path("sub/file.py", [root.resolve()])
    assert ok and resolved == (root / "sub" / "file.py").resolve()
    assert read_file("sub/file.py", [root.resolve()])["content"] == "x"
    ok, resolved = is_safe_path("sub/missing.py", [root.resolve()])
    assert ok and resolved == (root / "sub" / "missing.py").resolve()
    assert read_file("sub/missing.py", [root.resolve()])["error"] == "not_found"


def test_relative_path_prefers_root_where_it_exists(tmp_path):
    first, second = _root(tmp_path), tmp_path / "ws2"
    second.mkdir()
    (second / "only.txt").write_text("y")
    ok, resolved = is_safe_path("only.txt", [first, second.resolve()])
    assert ok and resolved == (second / "only.txt").resolve()


def test_relative_traversal_rejected(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.chdir(tmp_path)
    for target in ("../../etc/passwd", "../workspace-evil", "sub/../../x"):
        ok, reason = is_safe_path(target, [root.resolve()])
        assert ok is False and isinstance(reason, str)
        assert read_file(target, [root.resolve()])["error"] == "access_denied"
