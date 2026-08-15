from __future__ import annotations

import pytest

from s3_glacier_restore.filters import KeyFilter, compile_pattern, load_pattern_file


@pytest.mark.parametrize(
    "pattern,key,expected",
    [
        # Bare pattern -> basename match at any depth.
        (".DS_Store", "a/b/.DS_Store", True),
        (".DS_Store", ".DS_Store", True),
        (".DS_Store", "a/.DS_Store_backup", False),
        ("*.tmp", "deep/nested/file.tmp", True),
        ("*.tmp", "deep/nested/file.tmpx", False),
        # Bare pattern -> also matches a directory component.
        (".@__thumb", "photos/.@__thumb/x.jpg", True),
        ("@eaDir", "@eaDir/thumb.jpg", True),
        ("@eaDir*", "photos/@eaDir_v2/thumb.jpg", True),
        # The regression that motivated moving off substring matching.
        ("raw", "brawl/file.txt", False),
        ("raw", "raw/file.txt", True),
        # Scoped patterns anchor at the start of the key.
        ("logs/*.gz", "logs/a.gz", True),
        ("logs/*.gz", "logs/2024/a.gz", False),
        ("logs/**", "logs/2024/deep/a.gz", True),
        ("logs/*", "other/logs/a.gz", False),
        # Trailing slash -> directory and everything under it.
        ("tmp/", "a/tmp/b/c.txt", True),
        ("tmp/", "a/tmp.txt", False),
        ("a/tmp/", "a/tmp/b.txt", True),
        ("a/tmp/", "z/a/tmp/b.txt", False),
        # Character classes.
        ("file[0-9].txt", "d/file7.txt", True),
        ("file[0-9].txt", "d/filex.txt", False),
        ("file[!0-9].txt", "d/filex.txt", True),
        # '?' does not cross a separator.
        ("a?c", "x/abc", True),
        ("a?c", "x/a/c", False),
    ],
)
def test_pattern_matching(pattern, key, expected):
    assert compile_pattern(pattern).matches(key) is expected


def test_case_sensitivity():
    assert compile_pattern("Thumbs.db").matches("a/thumbs.db") is False
    assert compile_pattern("Thumbs.db", ignore_case=True).matches("a/thumbs.db") is True


def test_empty_pattern_rejected():
    with pytest.raises(ValueError):
        compile_pattern("   ")
    with pytest.raises(ValueError):
        compile_pattern("///")


def test_exclude_only():
    f = KeyFilter(excludes=["*.tmp", ".DS_Store"])
    assert f.keeps("a/b.txt")
    keep, reason = f.match("a/b.tmp")
    assert keep is False
    assert "*.tmp" in reason


def test_include_narrows_scope():
    f = KeyFilter(includes=["*.tif"])
    assert f.keeps("scans/page1.tif")
    keep, reason = f.match("scans/page1.jpg")
    assert keep is False
    assert "--include" in reason


def test_exclude_beats_include():
    f = KeyFilter(excludes=["draft/"], includes=["*.tif"])
    assert f.keeps("final/page.tif")
    assert not f.keeps("draft/page.tif")


def test_empty_filter_is_falsey_and_keeps_everything():
    f = KeyFilter()
    assert not f
    assert f.keeps("anything/at/all")
    assert f.describe() == "none"


def test_load_pattern_file(tmp_path):
    path = tmp_path / "skip.txt"
    path.write_text("# comment\n\n.DS_Store\n  *.tmp  \n", encoding="utf-8")
    assert load_pattern_file(str(path)) == [".DS_Store", "*.tmp"]


def test_missing_pattern_file_raises(tmp_path):
    # A silently-ignored typo would restore everything the operator meant to skip.
    with pytest.raises(FileNotFoundError):
        load_pattern_file(str(tmp_path / "nope.txt"))


def test_shipped_skiplist_compiles():
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    patterns = load_pattern_file(os.path.join(root, "skiplist.txt"))
    assert patterns
    f = KeyFilter(excludes=patterns)
    assert not f.keeps("photos/2019/.DS_Store")
    assert not f.keeps("photos/@eaDir/x.jpg")
    assert f.keeps("photos/2019/holiday.jpg")
