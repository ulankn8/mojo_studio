"""Timeline <-> JSON (versioned format)."""

import json
from typing import Any

from toystudio.core.models import AudioMode, Reason, Track
from toystudio.core.timeline import Segment, Timeline

FORMAT_VERSION = 1


def _seg_to_obj(s: Segment) -> dict[str, Any]:
    return {
        "id": s.id,
        "track": s.track.value,
        "media_id": s.media_id,
        "src_in": s.src_in,
        "src_out": s.src_out,
        "moment_id": s.moment_id,
        "bound_in": s.bound_in,
        "bound_out": s.bound_out,
        "locked": s.locked,
        "manual": s.manual,
        "audio_mode": s.audio_mode.value,
        "crop_x": s.crop_x,
        "reasons": [{"code": r.code, "ru": r.ru, "ky": r.ky} for r in s.reasons],
        "text": s.text,
        "anchor_id": s.anchor_id,
        "speed": s.speed,
        "move": s.move,
    }


def _seg_from_obj(o: dict[str, Any]) -> Segment:
    return Segment(
        id=o["id"],
        track=Track(o["track"]),
        media_id=o["media_id"],
        src_in=o["src_in"],
        src_out=o["src_out"],
        moment_id=o["moment_id"],
        bound_in=o["bound_in"],
        bound_out=o["bound_out"],
        locked=o["locked"],
        manual=o["manual"],
        audio_mode=AudioMode(o["audio_mode"]),
        crop_x=o["crop_x"],
        reasons=tuple(Reason(r["code"], r["ru"], r["ky"]) for r in o["reasons"]),
        text=o["text"],
        anchor_id=o["anchor_id"],
        speed=o.get("speed", 1.0),  # saved before slow motion existed
        move=o.get("move", ""),
    )


def timeline_to_json(t: Timeline) -> str:
    obj = {
        "v": FORMAT_VERSION,
        "video": [_seg_to_obj(s) for s in t.video],
        "music": [_seg_to_obj(s) for s in t.music],
        "titles": [_seg_to_obj(s) for s in t.titles],
        "speech": [_seg_to_obj(s) for s in t.speech],
    }
    return json.dumps(obj, ensure_ascii=False)


def timeline_from_json(s: str) -> Timeline:
    obj = json.loads(s)
    if obj.get("v") != FORMAT_VERSION:
        raise ValueError(f"Unsupported timeline format version: {obj.get('v')!r}")
    return Timeline(
        video=tuple(_seg_from_obj(o) for o in obj["video"]),
        music=tuple(_seg_from_obj(o) for o in obj["music"]),
        titles=tuple(_seg_from_obj(o) for o in obj["titles"]),
        speech=tuple(_seg_from_obj(o) for o in obj.get("speech", [])),  # absent before 0.1.2
    )
