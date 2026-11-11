"""Immutable timeline model. Positions are computed from durations, never stored."""

from dataclasses import dataclass
from itertools import accumulate

from toystudio.core.models import AudioMode, Reason, Track

MIN_SEGMENT_US = 40_000  # one frame at 25 fps


@dataclass(frozen=True)
class Segment:
    """A piece of a track.

    For TITLE segments src_in/src_out are an offset/length window (in us) measured from the
    start of the anchor video segment, and bound_in/bound_out are 0..anchor duration.
    """

    id: str
    track: Track
    media_id: str
    src_in: int
    src_out: int
    moment_id: str | None
    bound_in: int
    bound_out: int
    locked: bool = False
    manual: bool = False
    audio_mode: AudioMode = AudioMode.ORIGINAL
    crop_x: float = 0.5
    reasons: tuple[Reason, ...] = ()
    text: str | None = None
    anchor_id: str | None = None
    speed: float = 1.0  # 0.5 = slow motion: half a second of the source per second on screen

    def __post_init__(self) -> None:
        if not 0.1 <= self.speed <= 4.0:
            raise ValueError(f"Segment {self.id}: speed {self.speed} is out of 0.1..4")
        if not self.bound_in <= self.src_in < self.src_out <= self.bound_out:
            raise ValueError(
                f"Segment {self.id}: need bound_in <= src_in < src_out <= bound_out, got "
                f"{self.bound_in}, {self.src_in}, {self.src_out}, {self.bound_out}"
            )

    @property
    def duration(self) -> int:
        """Length on the timeline (the source window played at `speed`)."""
        return round((self.src_out - self.src_in) / self.speed)


@dataclass(frozen=True)
class Timeline:
    video: tuple[Segment, ...] = ()
    music: tuple[Segment, ...] = ()
    titles: tuple[Segment, ...] = ()
    # continuous reference sound (sound desk or main camera); when present it replaces the
    # cameras' own sound, and in every block its pieces add up to the video duration
    speech: tuple[Segment, ...] = ()

    def track_segments(self, track: Track) -> tuple[Segment, ...]:
        if track is Track.VIDEO:
            return self.video
        if track is Track.MUSIC:
            return self.music
        if track is Track.SPEECH:
            return self.speech
        return self.titles

    def duration(self) -> int:
        """Total length of the video track (music and titles do not extend it)."""
        return sum(s.duration for s in self.video)

    def starts(self, track: Track) -> list[int]:
        segs = self.track_segments(track)
        return [0, *accumulate(s.duration for s in segs)][: len(segs)]

    def index_of(self, seg_id: str) -> tuple[Track, int]:
        for track in Track:
            for i, s in enumerate(self.track_segments(track)):
                if s.id == seg_id:
                    return track, i
        raise KeyError(seg_id)

    def segment(self, seg_id: str) -> Segment:
        track, i = self.index_of(seg_id)
        return self.track_segments(track)[i]

    def map_position(self, old: "Timeline", pos: int) -> int:
        """Translate a video position of `old` into this layout.

        Stays at the same offset inside the same segment; if that segment is gone,
        goes to the start of the next surviving segment (or the end).
        """
        old_starts = old.starts(Track.VIDEO)
        new_starts = self.starts(Track.VIDEO)
        hit = len(old.video)
        for i, (seg, start) in enumerate(zip(old.video, old_starts, strict=True)):
            if pos < start + seg.duration:
                hit = i
                break
        if hit == len(old.video):
            return self.duration()
        seg = old.video[hit]
        src_time = seg.src_in + max(0, pos - old_starts[hit])
        # The segment covering this source time (the original or its split twin), else clamp
        # into the same-id segment, else the next surviving segment.
        same = next((j for j, c in enumerate(self.video) if c.id == seg.id), None)
        twin = self._covering(seg.media_id, src_time)
        if twin is None and same is not None:
            c = self.video[same]
            return new_starts[same] + max(c.src_in, min(src_time, c.src_out)) - c.src_in
        if twin is not None:
            return new_starts[twin] + src_time - self.video[twin].src_in
        new_index = {s.id: i for i, s in enumerate(self.video)}
        for later in old.video[hit + 1 :]:
            if later.id in new_index:
                return new_starts[new_index[later.id]]
        return self.duration()

    def _covering(self, media_id: str, src_time: int) -> int | None:
        for j, cand in enumerate(self.video):
            if cand.media_id == media_id and cand.src_in <= src_time < cand.src_out:
                return j
        return None
