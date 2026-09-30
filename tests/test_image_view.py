#!/usr/bin/env python3
"""Offline regression tests for the rich-pixels image viewer extension."""

from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import deepseek.extensions as ext_mod
from deepseek.extensions import load_extensions, reload_extensions
from PIL import Image


def make_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10), color=(32, 128, 224)).save(path)


def test_image_view_extension() -> None:
    fixture = ROOT / "tests" / "fixtures" / "test_image.png"
    make_fixture(fixture)

    with tempfile.TemporaryDirectory() as tmpdir:
        ext_dir = Path(tmpdir)
        shutil.copy2(ROOT / "examples" / "extensions" / "image_view.py",
                     ext_dir / "image_view.py")
        reload_extensions()
        with patch.object(ext_mod, "_search_paths", return_value=[ext_dir]):
            tools = load_extensions()
            show_image = next(t for t in tools if t.name == "show_image")
            assert show_image.deferred is True
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = show_image.fn(path=str(fixture), width=20)
            rendered = output.getvalue()
            assert "▄" in rendered or "▀" in rendered, rendered[:500]
            assert "Rendered" in result

    reload_extensions()
    print("all image view tests passed")


if __name__ == "__main__":
    test_image_view_extension()
