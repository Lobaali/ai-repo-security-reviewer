"""
Tests for selector.py 

Each test builds a tiny repository in a temporary folder (pytest's tmp_path),
so the exact contents and sizes are known.
"""

from pathlib import Path

from secreview.config import Limits
from secreview.selector import FileSelector, render_slice

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"


def make_repo(root: Path, files: dict[str, str | bytes]) -> Path:
    """Create files under root. Values are text (str) or raw bytes."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    return root


def limits(**overrides) -> Limits:
    """Generous defaults so each test only tightens the limit it is about."""
    values = dict(max_file_bytes=100_000, max_files=100, max_total_chars=1_000_000, chunk_chars=50_000)
    values.update(overrides)
    return Limits(**values)


# ------------------------------------------------------------------- scope

def test_selects_only_python_files_outside_ignored_folders(tmp_path):
    make_repo(tmp_path, {
        "app/main.py": "print('main')\n",
        "README.md": "# docs\n",
        ".git/config.py": "x = 1\n",
        "venv/lib/thing.py": "x = 1\n",
        "tests/test_main.py": "x = 1\n",
        "node_modules/pkg/a.py": "x = 1\n",
        "pkg.egg-info/b.py": "x = 1\n",
    })
    selection = FileSelector(limits()).select(tmp_path)

    assert selection.selected_paths == ["app/main.py"]
    assert selection.files_in_scope == 1
    assert selection.out_of_scope_files == 1      # README.md (ignored folders are not counted)
    assert selection.skipped == []


def test_empty_files_are_not_in_scope(tmp_path):
    make_repo(tmp_path, {"pkg/__init__.py": "", "pkg/core.py": "x = 1\n"})
    selection = FileSelector(limits()).select(tmp_path)
    assert selection.selected_paths == ["pkg/core.py"]
    assert selection.files_in_scope == 1


def test_symlinks_are_ignored(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n")
    repo = make_repo(tmp_path / "repo", {"main.py": "x = 1\n"})
    (repo / "link.py").symlink_to(outside)
    selection = FileSelector(limits()).select(repo)
    assert selection.selected_paths == ["main.py"]


# ----------------------------------------------------------------- ranking

def test_risky_files_are_ranked_first(tmp_path):
    make_repo(tmp_path, {
        "helpers/strings.py": "x = 1\n",
        "app/auth/login_views.py": "x = 1\n",
        "app/db.py": "x = 1\n",
    })
    selection = FileSelector(limits()).select(tmp_path)
    assert selection.selected_paths[0] == "app/auth/login_views.py"
    assert selection.selected_paths[-1] == "helpers/strings.py"


def test_order_is_deterministic(tmp_path):
    make_repo(tmp_path, {f"mod{i}.py": "x = 1\n" for i in range(10)})
    first = FileSelector(limits()).select(tmp_path).selected_paths
    second = FileSelector(limits()).select(tmp_path).selected_paths
    assert first == second


# ------------------------------------------------------------------ limits

def test_max_files_limit_is_recorded(tmp_path):
    make_repo(tmp_path, {"a.py": "x = 1\n", "b.py": "x = 1\n", "c.py": "x = 1\n"})
    selection = FileSelector(limits(max_files=2)).select(tmp_path)

    assert len(selection.selected_paths) == 2
    assert selection.files_in_scope == 3
    assert [s.reason for s in selection.skipped] == ["max_files limit reached"]


def test_large_file_is_skipped(tmp_path):
    make_repo(tmp_path, {"big.py": "x = 1\n" * 100, "small.py": "x = 1\n"})
    selection = FileSelector(limits(max_file_bytes=50)).select(tmp_path)

    assert selection.selected_paths == ["small.py"]
    assert selection.skipped[0].path == "big.py"
    assert "max_file_bytes" in selection.skipped[0].reason


def test_non_utf8_file_is_skipped(tmp_path):
    make_repo(tmp_path, {"latin.py": b"name = '\xff\xfe'\n", "ok.py": "x = 1\n"})
    selection = FileSelector(limits()).select(tmp_path)

    assert selection.selected_paths == ["ok.py"]
    assert selection.skipped[0].reason == "not valid UTF-8 text"


def test_total_chars_limit_skips_but_smaller_files_can_still_fit(tmp_path):
    make_repo(tmp_path, {
        "auth.py": "a" * 60 + "\n",     # highest rank, 61 chars
        "db.py": "b" * 60 + "\n",       # second rank, does not fit after auth.py
        "z.py": "c\n",                  # lowest rank, 2 chars, still fits
    })
    selection = FileSelector(limits(max_total_chars=70, chunk_chars=70)).select(tmp_path)

    assert selection.selected_paths == ["auth.py", "z.py"]
    assert [(s.path, s.reason) for s in selection.skipped] == [("db.py", "max_total_chars limit reached")]


# ---------------------------------------------------------------- chunking

def test_small_files_share_one_chunk(tmp_path):
    make_repo(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    selection = FileSelector(limits()).select(tmp_path)

    assert len(selection.chunks) == 1
    assert selection.chunks[0].paths == {"a.py", "b.py"}


def test_render_slice_format():
    text = render_slice("app/x.py", ["a = 1", "b = 2", "c = 3"], 2, 3)
    assert text == "=== FILE: app/x.py (lines 2-3 of 3) ===\n2| b = 2\n3| c = 3\n\n"


def test_fixture_repo_is_selected():
    selection = FileSelector(limits()).select(FIXTURE_REPO)
    assert set(selection.selected_paths) == {"app/utils.py", "app/routes.py"}
    assert '7|     return subprocess.run(f"ping -c 1 {host}", shell=True, capture_output=True)' in (
        selection.chunks[0].text
    )