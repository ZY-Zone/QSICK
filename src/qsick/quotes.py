"""One line of perspective per launch, read from `sentences/meaningfulsentences.txt`.

Two lines per entry: the sentence, then its attribution. Parsed once at startup and chosen once at startup --
"每次启动随机选取一句" means it should not change while you are looking at it, because a dashboard that reshuffles
its own text every five seconds is a distraction, not a greeting.

A missing or malformed file is not an error: the dashboard simply omits the tile. This is decoration; it must never
be the reason QSICK fails to start or a page fails to render.
"""

from __future__ import annotations

import random
from pathlib import Path

#: Where the file lives, relative to the repository root.
SENTENCE_FILE = Path("sentences") / "meaningfulsentences.txt"

#: One pair per entry: (sentence, attribution). The second line is optional.
Quote = tuple[str, str]


def parse(text: str) -> list[Quote]:
    """Turn the file into ``[(sentence, attribution)]``, two non-empty lines at a time.

    Blank lines are skipped rather than treated as separators: the file is hand-edited, and a trailing newline or a
    stray blank should not silently drop the sentence that follows it.
    """
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    quotes: list[Quote] = []
    for index in range(0, len(lines) - 1, 2):
        quotes.append((lines[index], lines[index + 1]))
    # A file with an odd final line still has a usable sentence; it just has no attribution.
    if len(lines) % 2 == 1:
        quotes.append((lines[-1], ""))
    return quotes


def load(path: Path | None = None) -> list[Quote]:
    """Read the file, or return an empty list when it cannot be read."""
    target = path or _default_path()
    try:
        return parse(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []


def _default_path() -> Path:
    # `src/qsick/quotes.py` → repository root is three levels up.
    return Path(__file__).resolve().parents[2] / SENTENCE_FILE


def pick(quotes: list[Quote] | None = None, *, rng: random.Random | None = None) -> Quote | None:
    """Choose one entry. Returns None when there is nothing to choose from."""
    items = quotes if quotes is not None else load()
    if not items:
        return None
    return (rng or random).choice(items)
