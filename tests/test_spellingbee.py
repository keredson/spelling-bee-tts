import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from spellingbee import SpellingBeeApp


def test_natural_key_sorts_grades():
    app = SpellingBeeApp()
    items = ["Grade 2", "Grade 10", "Grade 1"]
    ordered = sorted(items, key=app.natural_key)
    assert ordered == ["Grade 1", "Grade 2", "Grade 10"]


def test_compare_versions():
    app = SpellingBeeApp()
    assert app.compare_versions("1.2.0", "1.1.9") > 0
    assert app.compare_versions("1.2.0", "1.2.0") == 0
    assert app.compare_versions("1.2.0", "1.2.1") < 0


def test_get_builtin_dir_finds_repo_lists():
    app = SpellingBeeApp()
    builtin_dir = app.get_builtin_dir()
    assert builtin_dir is not None
    assert builtin_dir.name == "word_lists"
