#!/usr/bin/env python3
"""Render the project's original CC0 system-button glyph artwork."""

from pathlib import Path

from PIL import Image, ImageDraw


SCALE = 8
SIZE = (128, 64)


def point(value: int) -> int:
    return value * SCALE


def render(kind: str) -> Image.Image:
    image = Image.new("RGBA", (SIZE[0] * SCALE, SIZE[1] * SCALE), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    white = (255, 255, 255, 255)
    width = point(4)

    # Original vertical button silhouette, intentionally drawn from simple geometry.
    draw.rounded_rectangle(
        (point(52), point(29), point(76), point(61)),
        radius=point(12),
        outline=white,
        width=width,
    )

    if kind == "create":
        for start, end in (
            ((51, 22), (44, 7)),
            ((64, 20), (64, 3)),
            ((77, 22), (84, 7)),
        ):
            draw.line(
                (point(start[0]), point(start[1]), point(end[0]), point(end[1])),
                fill=white,
                width=width,
            )
    elif kind == "options":
        for y in (6, 14, 22):
            draw.rounded_rectangle(
                (point(51), point(y), point(77), point(y + 4)),
                radius=point(2),
                fill=white,
            )
    else:
        raise ValueError(kind)

    return image.resize(SIZE, Image.Resampling.LANCZOS)


def main() -> None:
    root = Path(__file__).resolve().parent
    for name in ("create", "options"):
        render(name).save(root / f"{name}.png", optimize=True)


if __name__ == "__main__":
    main()
