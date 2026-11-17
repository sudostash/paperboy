"""clean_scans without its optional scan extra (OpenCV): it says how to add it. Runs
whether OpenCV is installed or not (CI's mcp1 job runs without it)."""
from __future__ import annotations

import sys

import pytest
from PIL import Image

import paperboy.server as S


@pytest.fixture(autouse=True)
def sandbox(settings, monkeypatch):
    monkeypatch.setattr(S, "SETTINGS", settings)
    return settings


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "a.jpg"
    Image.new("RGB", (40, 40), "white").save(path)
    return path


def test_without_opencv_it_says_how_to_add_it(photo, monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)        # as if it weren't installed
    with pytest.raises(S.ToolError, match=r'pip install "paperboy-mcp\[scan\]"'):
        S.clean_scans([str(photo)])
    assert not photo.with_name("a_clean.pdf").exists()


def test_opencv_that_wont_load_is_named_as_such(photo, monkeypatch):
    import importlib.util

    from paperboy.engine import cleanscan as C
    monkeypatch.setitem(sys.modules, "cv2", None)        # installed, but its import fails
    monkeypatch.setattr(C.importlib.util, "find_spec",
                        lambda name: object() if name == "cv2" else importlib.util.find_spec(name))
    with pytest.raises(S.ToolError, match="installed but couldn't be loaded.*force-reinstall"):
        S.clean_scans([str(photo)])
