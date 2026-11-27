"""Generate `packaging/qsick.ico` from the artwork, so the icon can be rebuilt from source.

Committed alongside the .ico itself: the build reads the .ico directly and therefore needs neither
Pillow nor the original PNG, but keeping this script makes the icon reproducible instead of being an
opaque binary nobody can regenerate.

The source image is **not** in this repository and has no default path -- it lives on the machine
that holds the artwork, and baking that path in would make this script fail everywhere else.

    python packaging/make_icon.py <源图.png> [目标.ico]
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

DEFAULT_TARGET = Path(__file__).with_name("qsick.ico")

#: Sizes Windows actually uses: 16 (title bar), 20/24 (list views at 125%/150% DPI), 32 (taskbar),
#: 48 (large icons), 64/128 (extra-large views), 256 (thumbnails and the Alt+Tab switcher).
SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    if not arguments:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    source = Path(arguments[0])
    target = Path(arguments[1]) if len(arguments) > 1 else DEFAULT_TARGET

    if not source.is_file():
        print(f"源图不存在：{source}", file=sys.stderr)
        return 1

    image = Image.open(source)
    print(f"源图 {image.size[0]}x{image.size[1]} {image.mode}")

    # ICO entries are squares, so a non-square source would be squashed rather than cropped.
    if image.size[0] != image.size[1]:
        side = min(image.size)
        left = (image.size[0] - side) // 2
        top = (image.size[1] - side) // 2
        image = image.crop((left, top, left + side, top + side))
        print(f"已居中裁剪为 {side}x{side}")

    # RGBA throughout: a palette conversion loses the transparency that lets the icon sit cleanly on
    # both light and dark shells.
    image = image.convert("RGBA")

    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="ICO", sizes=[(size, size) for size in SIZES])
    print(f"已写入 {target}  {target.stat().st_size / 1024:.1f} KB  尺寸 {SIZES}")

    # Read it back: an .ico Windows cannot parse would only show up as a blank icon on the exe.
    check = Image.open(target)
    print(f"回读：{check.format} {check.size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
