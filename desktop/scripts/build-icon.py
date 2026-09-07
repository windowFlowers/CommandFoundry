"""Build a multi-size Windows ICO from the ImageGen-produced transparent PNG."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


SIZES = (16, 32, 48, 64, 128, 256)


def main() -> None:
    assets = Path(__file__).resolve().parents[1] / "assets"
    source = assets / "icon.png"
    output = assets / "icon.ico"
    image = Image.open(source).convert("RGBA")
    if image.getextrema()[3][0] == 255:
        raise ValueError("icon.png must have a transparent background")
    if image.size != (1024, 1024):
        image = image.resize((1024, 1024), Image.Resampling.LANCZOS)
        image.save(source, "PNG", optimize=True)
    image.save(output, format="ICO", sizes=[(size, size) for size in SIZES])
    print(f"Generated {output} from ImageGen PNG with sizes: {', '.join(map(str, SIZES))}")


if __name__ == "__main__":
    main()
