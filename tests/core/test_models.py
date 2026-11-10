import pytest

from toystudio.core.models import (
    FORMAT_ROLES,
    US_PER_S,
    AudioMode,
    Mark,
    MediaKind,
    MediaRole,
    Moment,
    MomentType,
    ProjectFormat,
    Reason,
    ToiKind,
    Track,
    format_toi_kind,
)


def test_moment_duration() -> None:
    m = Moment(1, 1, MomentType.TOAST, 10 * US_PER_S, 40 * US_PER_S, 0.9)
    assert m.duration == 30 * US_PER_S
    assert m.mark is Mark.NONE


@pytest.mark.parametrize("end", [10, 5])
def test_moment_invalid_range(end: int) -> None:
    with pytest.raises(ValueError):
        Moment(1, 1, MomentType.DANCE, 10, end, 0.5)


def test_enum_values() -> None:
    assert MomentType.GIFTS == "gifts"
    assert Track.VIDEO == "video"
    assert AudioMode.DUCK == "duck"
    assert ToiKind.KYZ_UZATUU == "kyz_uzatuu"
    assert Reason("c", "ру", "ky").code == "c"


# --- edit formats and track roles ---


def test_role_kind_audio_roles_are_audio_files():
    assert MediaRole.SPEECH.kind is MediaKind.MUSIC
    assert MediaRole.MUSIC.kind is MediaKind.MUSIC
    for role in (MediaRole.PRE, MediaRole.MAIN, MediaRole.CUTAWAY):
        assert role.kind is MediaKind.VIDEO


def test_default_role_of_files_before_roles():
    assert MediaRole.default_for(MediaKind.VIDEO) is MediaRole.MAIN
    assert MediaRole.default_for(MediaKind.MUSIC) is MediaRole.MUSIC


def test_every_format_has_a_video_track_and_music():
    for fmt in ProjectFormat:
        roles = FORMAT_ROLES[fmt]
        assert any(r.kind is MediaKind.VIDEO for r in roles), fmt
        assert roles[-1] is MediaRole.MUSIC
        assert len(set(roles)) == len(roles)


def test_full_wedding_tracks_and_toi_as_before():
    assert FORMAT_ROLES[ProjectFormat.WEDDING_FULL] == (
        MediaRole.PRE,
        MediaRole.MAIN,
        MediaRole.CUTAWAY,
        MediaRole.SPEECH,
        MediaRole.MUSIC,
    )
    assert FORMAT_ROLES[ProjectFormat.WEDDING_EXPRESS] == (MediaRole.PRE, MediaRole.MUSIC)
    assert FORMAT_ROLES[ProjectFormat.TOI] == (MediaRole.MAIN, MediaRole.MUSIC)


def test_format_toi_kind():
    assert format_toi_kind(ProjectFormat.WEDDING_FULL, ToiKind.JUBILEE) is ToiKind.WEDDING
    assert format_toi_kind(ProjectFormat.PODCAST, ToiKind.WEDDING) is ToiKind.OTHER
    assert format_toi_kind(ProjectFormat.TOI, ToiKind.JUBILEE) is ToiKind.JUBILEE
