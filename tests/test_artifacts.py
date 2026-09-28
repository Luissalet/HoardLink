import pytest

from hoard_link.artifacts import ArtifactRef, parse_ref, revision_for


def test_round_trip_reference_with_special_characters():
    ref = ArtifactRef("scheherazade", "session", "mundo 1/capítulo 2")
    assert parse_ref(ref.uri) == ref
    assert ref.uri == "hoard://scheherazade/session/mundo%201%2Fcap%C3%ADtulo%202"


def test_invalid_reference_is_rejected():
    for uri in ("https://example.com/a/b", "hoard://app/a", "hoard://app/a/b?token=secret"):
        with pytest.raises(ValueError):
            parse_ref(uri)


def test_revision_is_stable_for_text_and_bytes():
    assert revision_for("á") == revision_for("á".encode("utf-8"))
    assert revision_for("a") != revision_for("b")
