"""Light overlays of a clip (waves, lines, leaks, bokeh, a sweep), placed as an editor would.

The overlays are short videos of light on black (tools/overlays, Remotion), laid over the
film in "screen" mode: black disappears, light adds. Placement: a flash out of the teaser, a warm leak over the
story's opening, light lines over a few dissolves (at most 3, 15 s apart), a sweep on the climax's first beat, bokeh over the
first slow shot of the middle, waves over the ending; each starts on a beat and none overlaps
another.
"""

from __future__ import annotations

from pathlib import Path

from toystudio.engine.edit.day import DayDraft
from toystudio.engine.edit.restaurant import Item

S = 1_000_000
ASSET_DIR = Path(__file__).resolve().parents[2] / "assets" / "overlays"
# name -> (media id in layouts, length of the asset)
OVERLAYS: dict[str, tuple[int, int]] = {
    "leak": (-1, 4 * S),
    "lines": (-2, 3 * S),
    "waves": (-3, 5 * S),
    "bokeh": (-4, 4 * S),
    "sweep": (-5, 1_600_000),
}
OPACITY = {"leak": 0.8, "lines": 0.8, "waves": 0.9, "bokeh": 0.85, "sweep": 0.85}
LINES_MAX = 3  # light lines over dissolves: a few, or they become wallpaper
LINES_GAP_US = 15 * S


def asset(media_id: int) -> Path:
    name = next(n for n, (i, _) in OVERLAYS.items() if i == media_id)
    return ASSET_DIR / f"{name}.mp4"


def place(draft: DayDraft) -> list[Item]:
    """V2 overlay items for a clip draft (none for the film or an empty draft)."""
    if not draft.fade or not draft.shots:
        return []
    beat = draft.beat_us or 600_000
    total = sum(s.length for s in draft.shots)
    starts: list[int] = []
    pos = 0
    for s in draft.shots:
        starts.append(pos)
        pos += s.length

    def first(part: str, after: int = 0) -> int | None:
        return next((t for s, t in zip(draft.shots, starts, strict=True)
                     if s.part == part and t >= after), None)  # fmt: skip

    story = next((t for s, t in zip(draft.shots, starts, strict=True) if s.part != "teaser"), 0)
    sweep_len = OVERLAYS["sweep"][1]
    # with a teaser: a flash out of it, the warm leak over the story's opening
    wanted: list[tuple[str, int]] = (
        [("sweep", story - sweep_len // 2), ("leak", story + sweep_len // 2)]
        if story
        else [("leak", 0)]
    )
    climax = first("climax")
    if climax is not None:
        wanted.append(("sweep", climax))
    outro = first("outro")
    if outro is not None:
        wanted.append(("waves", outro))
    slow = next((t for s, t in zip(draft.shots, starts, strict=True)
                 if s.speed != 1.0 and t >= total * 0.3), None)  # fmt: skip
    if slow is not None:
        wanted.append(("bokeh", slow))
    lines: list[int] = []
    for cut in draft.dissolves:
        if len(lines) < LINES_MAX and (not lines or cut - lines[-1] >= LINES_GAP_US):
            lines.append(cut)
            wanted.append(("lines", cut - OVERLAYS["lines"][1] // 3))
    items: list[Item] = []
    for name, at in wanted:  # earlier rules win a clash
        media_id, length = OVERLAYS[name]
        start = max(0, round(at / beat) * beat) if name != "sweep" else at
        end = min(start + length, total)
        if end - start < length // 2:
            continue
        if any(start < i.end and i.start < end for i in items):
            continue
        items.append(Item("V2", media_id, 0, end - start, start, end, 1.0, "screen",
                          OPACITY[name]))  # fmt: skip
    return sorted(items, key=lambda i: i.start)
