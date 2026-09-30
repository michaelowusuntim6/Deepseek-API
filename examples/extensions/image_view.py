"""Render local images inline in the terminal with rich-pixels."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich_pixels import Pixels

from deepseek import tool


@tool
def show_image(path: str, width: int = 40) -> str:
    """Display a local image inline in the terminal.

    Args:
        path: Path to the image file (PNG, JPEG, GIF, WebP).
        width: Target width in terminal cells (default 40).
    """
    image_path = Path(path).expanduser()
    if not image_path.exists():
        return f"Error: image '{path}' does not exist."
    if not image_path.is_file():
        return f"Error: '{path}' is not a file."
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            src_w, src_h = image.size
        target_w = max(1, int(width))
        target_h = max(2, int(target_w * src_h / max(1, src_w)))
        if target_h % 2:
            target_h += 1
        pixels = Pixels.from_image_path(image_path, resize=(target_w, target_h))
        Console().print(pixels)
        return f"Rendered {image_path} at {target_w}x{target_h} pixels."
    except ImportError as exc:
        return f"Error: rich-pixels/Pillow are not installed: {exc}"
    except Exception as exc:
        return f"Error rendering image '{path}': {type(exc).__name__}: {exc}"


def register():
    return [show_image]
