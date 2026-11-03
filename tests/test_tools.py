import json
import os
import stat
from pathlib import Path

import pytest

from strand.config import Config, load_config
from strand.tools import workspace
from strand.tools.workspace import (
    MAX_READ_BYTES,
    MAX_READ_LINES,
    WORKSPACE_TOOLS,
    dispatch_tool_call,
    grep_workspace,
    is_safe_path,
    list_directory,
    make_directory,
    move_file,
    patch_file,
    read_file,
    read_image,
    remove_file,
    restore_backup,
    write_file,
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
    assert names == {"read_file", "list_directory", "grep_workspace", "write_file", "patch_file", "restore_backup",
                     "make_directory", "move_file", "remove_file", "read_image"}
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


def test_systemd_cwd_outside_roots_relative_resolution(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "Projects"
    (root / "repo").mkdir(parents=True)
    (root / "repo" / "file.txt").write_text("hello")
    (root / "top.txt").write_text("top")
    (home / "outside.txt").write_text("secret")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)  # simulated systemd cwd, outside the approved root
    roots = [root.resolve()]
    ok, resolved = is_safe_path("repo/file.txt", roots)
    assert ok and resolved == (root / "repo" / "file.txt").resolve()
    assert read_file("repo/file.txt", roots)["content"] == "hello"
    assert read_file("top.txt", roots)["content"] == "top"
    names = [e["name"] for e in list_directory("repo", roots)["entries"]]
    assert names == ["file.txt"]
    assert read_file("~/Projects/repo/file.txt", roots)["content"] == "hello"
    assert list_directory("~/Projects", roots)["path"] == str(root.resolve())
    for target in ("../../etc/shadow", "../outside.txt", "~/outside.txt"):
        assert is_safe_path(target, roots)[0] is False
        assert read_file(target, roots)["error"] == "access_denied"
        assert list_directory(target, roots)["error"] == "access_denied"


def test_relative_path_accepts_cwd_inside_root(tmp_path, monkeypatch):
    first, second = _root(tmp_path), tmp_path / "other"
    (second / "pkg").mkdir(parents=True)
    (second / "pkg" / "m.py").write_text("z")
    monkeypatch.chdir(second)
    ok, resolved = is_safe_path("pkg/m.py", [first, second.resolve()])
    assert ok and resolved == (second / "pkg" / "m.py").resolve()
    # cwd outside every root contributes nothing
    monkeypatch.chdir(tmp_path)
    assert is_safe_path("../x", [first])[0] is False


def test_single_line_megabyte_file_is_read_with_bounded_buffer(tmp_path):
    root = _root(tmp_path)
    big = root / "bundle.min.js"
    big.write_text("a" * 1_000_000)
    result = read_file(str(big), [root])
    assert len(result["content"]) == MAX_READ_BYTES
    assert result["byte_truncated"] is True and result["truncated"] is True
    # Paging past an over-long first line must skip it in bounded chunks and continue cleanly.
    big.write_text("a" * 1_000_000 + "\nsecond\n")
    paged = read_file(str(big), [root], offset=2)
    assert paged["content"] == "second\n" and paged["truncated"] is False


def test_read_file_never_requests_more_than_cap_per_readline(tmp_path, monkeypatch):
    import builtins

    root = _root(tmp_path)
    big = root / "one.json"
    big.write_text("b" * 300_000)
    sizes: list[int] = []
    real_open = builtins.open

    class Spy:
        def __init__(self, fh):
            self._fh = fh

        def readline(self, size=-1):
            sizes.append(size)
            return self._fh.readline(size)

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

    monkeypatch.setattr("strand.tools.workspace.open", lambda *a, **k: Spy(real_open(*a, **k)), raising=False)
    read_file(str(big), [root])
    assert sizes and all(0 < s <= MAX_READ_BYTES + 1 for s in sizes)


# ---------------------------------------------------------------- Ring 1 / grep


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return h


@pytest.fixture(params=["python", "rg"])
def grep_engine(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(workspace, "_find_rg", lambda: None)
    elif workspace._find_rg() is None:
        pytest.skip("ripgrep not installed")
    return request.param


def test_grep_finds_matches_and_respects_limits(tmp_path, grep_engine):
    root = _root(tmp_path)
    (root / "a.md").write_text("alpha\nneedle one\nomega\n")
    (root / "sub").mkdir()
    (root / "sub" / "b.py").write_text("x = 1\n# needle two\n")
    (root / "many.txt").write_text("needle\n" * 100)
    (root / "bin.dat").write_bytes(b"\x00needle")
    result = grep_workspace("needle one|needle two", [root], str(root))
    assert {(Path(m["path"]).name, m["line_number"], m["line_text"]) for m in result["matches"]} == {
        ("a.md", 2, "needle one"),
        ("b.py", 2, "# needle two"),
    }
    assert result["engine"] == grep_engine and result["truncated"] is False
    capped = grep_workspace("needle", [root], str(root / "many.txt"), max_results=500)
    assert len(capped["matches"]) == 20 and capped["truncated"] is True
    assert len(grep_workspace("needle", [root], str(root / "many.txt"), max_results=3)["matches"]) == 3
    (root / "wide.txt").write_text(("needle " + "y" * 150 + "\n") * 30)
    wide = grep_workspace("needle", [root], str(root / "wide.txt"))
    assert len(json.dumps(wide["matches"])) <= 4096 and wide["truncated"] is True


def test_grep_sandbox_and_denied_files(tmp_path, grep_engine):
    root = _root(tmp_path)
    (root / ".env").write_text("needle=1\n")
    (root / "ok.txt").write_text("needle\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("needle\n")
    assert grep_workspace("needle", [root], str(outside))["error"] == "access_denied"
    assert grep_workspace("needle", [root], "../outside")["error"] == "access_denied"
    names = {Path(m["path"]).name for m in grep_workspace("needle", [root], str(root))["matches"]}
    assert names == {"ok.txt"}
    assert grep_workspace("(", [root], str(root))["error"] == "invalid_arguments"
    assert grep_workspace("", [root], str(root))["error"] == "invalid_arguments"
    assert grep_workspace("zzz", [root], str(root / "missing"))["error"] == "not_found"


def test_write_and_patch_snapshot_and_restore(tmp_path, home):
    root = _root(tmp_path)
    target = root / "note.md"
    created = write_file(str(target), "v1\n", [root])
    assert target.read_text() == "v1\n" and created["track"] == "snapshot"
    backups = home / ".local" / "share" / "strand" / "backups"
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700
    first = backups / created["backup"]
    assert first.read_bytes() == b"" and stat.S_IMODE(first.stat().st_mode) == 0o600
    assert created["backup"].endswith("_note.md")

    patched = patch_file(str(target), "v1", "v2", [root])
    assert target.read_text() == "v2\n"
    snap = backups / patched["backup"]
    assert snap.read_text() == "v1\n" and stat.S_IMODE(snap.stat().st_mode) == 0o600
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in backups.iterdir())

    restored = restore_backup(patched["backup"], [root])
    assert target.read_text() == "v1\n"
    # restoring is itself undoable
    restore_backup(restored["redo_backup"], [root])
    assert target.read_text() == "v2\n"
    # undoing a creation removes the file
    gone = restore_backup(created["backup"], [root])
    assert gone["deleted"] is True and not target.exists()


def test_patch_file_ambiguous_missing_untouched(tmp_path, home):
    root = _root(tmp_path)
    target = root / "f.txt"
    target.write_text("dup\ndup\nuniq\n")
    assert patch_file(str(target), "dup", "x", [root])["error"] == "search_ambiguous"
    assert patch_file(str(target), "nope", "x", [root])["error"] == "search_not_found"
    assert patch_file(str(target), "", "x", [root])["error"] == "invalid_arguments"
    assert patch_file(str(root / "none.txt"), "a", "b", [root])["error"] == "not_found"
    assert target.read_text() == "dup\ndup\nuniq\n"
    assert not (home / ".local").exists()  # failures never snapshot



def test_mutation_security_fails_closed(tmp_path, home):
    root = _root(tmp_path)
    cfg = home / ".config" / "strand"
    cfg.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    denied = (
        outside,
        cfg / "config.json",
        root / ".env",
        root / ".env.local",
        root / "a.key",
        root / "id_rsa",
        root / "id_rsa.pub",
    )
    for target in denied:
        assert write_file(str(target), "x", [root])["error"] == "access_denied", target
        assert patch_file(str(target), "a", "b", [root])["error"] == "access_denied", target
        assert not target.exists()
    assert write_file("../outside.txt", "x", [root])["error"] == "access_denied"
    # config dir denied even when it is an approved root
    assert write_file(str(cfg / "config.json"), "x", [home])["error"] == "access_denied"
    assert write_file(str(root / ".git" / "hooks" / "pre-commit"), "x", [root])["error"] == "access_denied"
    assert restore_backup("../../etc/passwd", [root])["error"] == "access_denied"
    assert restore_backup("missing_id", [root])["error"] == "not_found"
    big = "x" * (workspace.MAX_WRITE_BYTES + 1)
    assert write_file(str(root / "big"), big, [root])["error"] == "too_large"


def test_restore_cannot_target_outside_roots(tmp_path, home):
    root = _root(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    victim = other / "v.txt"
    victim.write_text("keep")
    backups = home / ".local" / "share" / "strand" / "backups"
    backups.mkdir(parents=True)
    (backups / "1_v.txt").write_text("evil")
    (backups / "1_v.txt.meta").write_text(json.dumps({"original": str(victim), "existed": True}))
    assert restore_backup("1_v.txt", [root])["error"] == "access_denied"
    assert victim.read_text() == "keep"


def test_git_track_requires_clean_status(tmp_path, home, monkeypatch):
    root = _root(tmp_path)
    (root / ".git").mkdir()
    target = root / "code.py"
    target.write_text("a = 1\n")
    monkeypatch.setattr(workspace, "_git_clean", lambda repo, path: "uncommitted changes in git")
    dirty = patch_file(str(target), "1", "2", [root])
    assert dirty["error"] == "git_dirty" and target.read_text() == "a = 1\n"
    assert write_file(str(target), "b", [root])["error"] == "git_dirty"
    monkeypatch.setattr(workspace, "_git_clean", lambda repo, path: None)
    ok = patch_file(str(target), "1", "2", [root])
    assert ok["track"] == "git" and ok["backup"] is None and target.read_text() == "a = 2\n"
    assert not (home / ".local").exists()  # git track writes no snapshot


def test_dispatch_mutation_tools(tmp_path, home):
    root = _root(tmp_path)
    target = root / "d.txt"
    out = dispatch_tool_call("write_file", {"path": str(target), "content": "hello", "junk": 1}, [root])
    assert target.read_text() == "hello"
    patch_args = {"path": str(target), "search_block": "hello", "replace_block": "bye"}
    assert dispatch_tool_call("patch_file", patch_args, [root])["bytes_written"] == 3
    assert dispatch_tool_call("restore_backup", {"backup": out["backup"]}, [root])["deleted"] is True
    assert dispatch_tool_call("write_file", {"path": str(target)}, [root])["error"] == "invalid_arguments"
    grep_args = {"pattern": "zzz", "path": str(root)}
    assert dispatch_tool_call("grep_workspace", grep_args, [root])["matches"] == []
    env_args = {"path": str(root / ".env"), "content": "x"}
    assert dispatch_tool_call("write_file", env_args, [root])["error"] == "access_denied"



def test_make_directory_and_nested_write(tmp_path, home):
    root = _root(tmp_path)
    made = make_directory(str(root / "a" / "b"), [root])
    assert made == {"path": str(root / "a" / "b"), "status": "created"} and (root / "a" / "b").is_dir()
    assert make_directory(str(root / "a" / "b"), [root])["status"] == "created"  # idempotent
    out = write_file(str(root / "sub" / "nested" / "new.txt"), "hi", [root])
    assert (root / "sub" / "nested" / "new.txt").read_text() == "hi" and "error" not in out
    (root / "f").write_text("x")
    assert make_directory(str(root / "f"), [root])["error"] == "not_a_directory"
    for bad in (str(tmp_path / "outside"), "../escape", "", str(home / ".config" / "strand" / "x")):
        assert make_directory(bad, [root])["error"] == "access_denied", bad
    assert make_directory(str(home / ".config" / "strand" / "x"), [home])["error"] == "access_denied"
    assert not (tmp_path / "outside").exists()


def test_move_file_and_restore(tmp_path, home):
    root = _root(tmp_path)
    src = root / "a.txt"
    src.write_text("data")
    dst = root / "new" / "dir" / "b.txt"
    moved = move_file(str(src), str(dst), [root])
    assert moved["status"] == "moved" and dst.read_text() == "data" and not src.exists()
    backups = home / ".local" / "share" / "strand" / "backups"
    assert stat.S_IMODE((backups / moved["backup"]).stat().st_mode) == 0o600
    back = restore_backup(moved["backup"], [root])
    assert src.read_text() == "data" and not dst.exists() and back["deleted"] is False
    restore_backup(back["redo_backup"], [root])  # redo moves it forward again
    assert dst.read_text() == "data" and not src.exists()


def test_move_file_guards(tmp_path, home):
    root = _root(tmp_path)
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    (root / ".env").write_text("k")
    assert move_file(str(root / "a.txt"), str(root / "b.txt"), [root])["error"] == "target_exists"
    assert move_file(str(root / "none"), str(root / "c"), [root])["error"] == "not_found"
    assert move_file(str(root / "a.txt"), str(tmp_path / "out.txt"), [root])["error"] == "access_denied"
    assert move_file(str(tmp_path / "out.txt"), str(root / "c"), [root])["error"] == "access_denied"
    assert move_file(str(root / ".env"), str(root / "e"), [root])["error"] == "access_denied"
    assert move_file(str(root / "a.txt"), str(root / ".env.x"), [root])["error"] == "access_denied"
    assert (root / "a.txt").read_text() == "a" and (root / ".env").exists()


def test_move_file_git_clean_gate(tmp_path, home, monkeypatch):
    root = _root(tmp_path)
    (root / ".git").mkdir()
    (root / "a.py").write_text("x")
    monkeypatch.setattr(workspace, "_git_clean", lambda repo, path: "uncommitted changes in git")
    assert move_file(str(root / "a.py"), str(root / "b.py"), [root])["error"] == "git_dirty"
    monkeypatch.setattr(workspace, "_git_clean", lambda repo, path: None)
    ok = move_file(str(root / "a.py"), str(root / "b.py"), [root])
    assert ok["track"] == "git" and ok["backup"] is None and (root / "b.py").exists()


def test_remove_file_and_restore(tmp_path, home):
    root = _root(tmp_path)
    target = root / "gone.txt"
    target.write_text("precious")
    os.chmod(target, 0o640)
    out = remove_file(str(target), [root])
    assert out["status"] == "removed" and not target.exists()
    snap = home / ".local" / "share" / "strand" / "backups" / out["backup_id"]
    assert snap.read_text() == "precious" and stat.S_IMODE(snap.stat().st_mode) == 0o600
    assert out["backup_id"].endswith("_gone.txt")
    restored = restore_backup(out["backup_id"], [root])
    assert target.read_text() == "precious" and restored["deleted"] is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_remove_file_guards(tmp_path, home):
    root = _root(tmp_path)
    cfg = home / ".config" / "strand"
    cfg.mkdir(parents=True)
    (cfg / "memory.db").write_text("db")
    (root / ".env").write_text("k")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref")
    (root / "d").mkdir()
    (tmp_path / "outside.txt").write_text("o")
    for bad in (cfg / "memory.db", root / ".env", root / ".git" / "HEAD", root / ".git", root):
        assert remove_file(str(bad), [root, home])["error"] == "access_denied", bad
    assert remove_file(str(cfg / "memory.db"), [root])["error"] == "access_denied"
    assert remove_file(str(tmp_path / "outside.txt"), [root])["error"] == "access_denied"
    assert remove_file(str(root), [root])["error"] == "access_denied"
    assert remove_file(str(root / "none"), [root])["error"] == "not_found"
    assert remove_file(str(root / "d"), [root])["error"] == "not_a_file"
    assert (cfg / "memory.db").exists() and (root / ".env").exists() and (root / ".git" / "HEAD").exists()


def test_dispatch_lifecycle_tools(tmp_path, home):
    root = _root(tmp_path)
    assert dispatch_tool_call("make_directory", {"path": str(root / "d")}, [root])["status"] == "created"
    (root / "d" / "f").write_text("1")
    mv = {"source_path": str(root / "d" / "f"), "target_path": str(root / "d" / "g")}
    assert dispatch_tool_call("move_file", mv, [root])["status"] == "moved"
    rm = dispatch_tool_call("remove_file", {"path": str(root / "d" / "g"), "junk": 1}, [root])
    assert rm["status"] == "removed"
    assert dispatch_tool_call("restore_backup", {"backup": rm["backup_id"]}, [root])["deleted"] is False
    assert dispatch_tool_call("move_file", {"source_path": "x"}, [root])["error"] == "invalid_arguments"


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x01" * 32


def test_read_image_encodes_valid_image(tmp_path):
    import base64

    root = _root(tmp_path)
    (root / "pic.png").write_bytes(PNG_BYTES)
    out = read_image(str(root / "pic.png"), [root])
    assert out["mime_type"] == "image/png" and out["bytes"] == len(PNG_BYTES)
    assert out["path"] == str(root / "pic.png")
    prefix = "data:image/png;base64,"
    assert out["data_uri"].startswith(prefix)
    assert base64.b64decode(out["data_uri"][len(prefix):]) == PNG_BYTES
    viaq = dispatch_tool_call("read_image", {"path": str(root / "pic.png")}, [root])
    assert viaq["data_uri"] == out["data_uri"]


def test_read_image_fails_closed(tmp_path, monkeypatch):
    root = _root(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    (root / "notes.txt").write_text("x")
    (root / ".env.png").write_bytes(PNG_BYTES)
    (root / "fake.png").write_bytes(b"not an image")
    (root / "pic.jpg").write_bytes(PNG_BYTES)  # content/extension mismatch
    assert read_image(str(outside), [root])["error"] == "access_denied"
    assert read_image(str(root / ".env.png"), [root])["error"] == "access_denied"
    assert read_image(str(root / "notes.txt"), [root])["error"] == "unsupported_extension"
    assert read_image(str(root / "missing.png"), [root])["error"] == "not_found"
    assert read_image(str(root / "fake.png"), [root])["error"] == "invalid_image"
    assert read_image(str(root / "pic.jpg"), [root])["error"] == "invalid_image"
    monkeypatch.setattr(workspace, "MAX_IMAGE_BYTES", 8)
    (root / "big.png").write_bytes(PNG_BYTES)
    assert read_image(str(root / "big.png"), [root])["error"] == "file_too_large"


def test_read_file_rejects_binary_images(tmp_path):
    root = _root(tmp_path)
    (root / "pic.png").write_bytes(PNG_BYTES)
    (root / "doc.pdf").write_bytes(b"%PDF-1.4 text only")
    for name in ("pic.png", "doc.pdf"):
        out = read_file(str(root / name), [root])
        assert out["error"] == "binary_file"
        assert out["message"] == "File appears to be binary. Use read_image for visual media."
