"""Unit tests for app.tagtools — run with: .venv/bin/python -m pytest tests/ -q

No pytest? These also run standalone: .venv/bin/python tests/test_tagtools.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import tagtools  # noqa: E402
from app.tagtools import normalize_genre, fill_album_artist, is_compilation  # noqa: E402


def test_split_compound():
    assert normalize_genre("Rock/Pop") == ["Rock", "Pop"]


def test_case_insensitive_alias():
    assert normalize_genre("soundtracks") == ["Soundtrack"]


def test_junk_dropped():
    assert normalize_genre("Music") == []


def test_multivalue_list_input():
    assert normalize_genre(["Rock", "rock", "Alternative"]) == ["Rock", "Alternative"]


def test_protected_slash_genre():
    # A canonical genre containing a slash must not be split.
    assert normalize_genre("R&B/Soul") == ["R&B/Soul"]
    assert normalize_genre("Christian/Gospel") == ["Christian/Gospel"]


def test_decade_tag_dropped():
    assert normalize_genre("80s") == []
    assert normalize_genre("1990s") == []


def test_vocal_alone_dropped():
    assert normalize_genre("Vocal") == []
    # but Vocal alongside another genre is kept
    assert normalize_genre(["Vocal", "Jazz"]) == ["Vocal", "Jazz"]


def test_keyword_fallback():
    assert normalize_genre("Soft Rock") == ["Rock"]
    assert normalize_genre("Death Metal") == ["Metal"]


def test_unknown_dropped():
    assert normalize_genre("Zydecabop") == []


def test_empty_and_none():
    assert normalize_genre(None) == []
    assert normalize_genre("") == []
    assert normalize_genre([]) == []


def test_is_compilation():
    assert is_compilation("/music/Various Artists/Now 50/01 Song.flac") is True
    assert is_compilation("/music/The Beatles/Abbey Road/01 Come Together.flac") is False


def test_fill_album_artist():
    assert fill_album_artist({"artist": "Prince"}, "/music/Prince/1999/01 x.flac") == "Prince"
    assert fill_album_artist({"artist": ["a", "b"]}, "/music/Compilation/x.flac") == "Various Artists"
    assert fill_album_artist({}, "/music/Artist/Album/x.flac") is None


# ── M3: genre review + unify ────────────────────────────────────────────────

from app.tagtools import artist_key, is_sole_holiday, run_genre_review  # noqa: E402


def test_artist_key():
    assert artist_key("The Cure", "The Cure") == "The Cure"
    # Compilation → group by track artist, not "Various Artists"
    assert artist_key("Various Artists", "Wham!") == "Wham!"


def test_is_sole_holiday():
    assert is_sole_holiday(["Holiday"]) is True
    assert is_sole_holiday(["Holiday", "Pop"]) is False
    assert is_sole_holiday(["Pop"]) is False


def _rows(*triples):
    # (albumartist, artist, genre_str)
    return [{"path": f"/m/{i}.flac", "albumartist": a, "artist": ar, "genre": g}
            for i, (a, ar, g) in enumerate(triples)]


def test_review_curated_wins():
    rows = _rows(("The Cure", "The Cure", "Rock"), ("The Cure", "The Cure", "Pop"))
    r = run_genre_review(rows, use_online=False, progress_cb=lambda _: None, should_cancel=lambda: False)
    cure = [a for a in r["artists"] if a["key"] == "the cure"][0]
    assert cure["source"] == "curated"
    assert cure["canonical"] == ["Alternative", "New Wave"]
    assert cure["changes"] == 2  # both tracks differ from canonical


def test_review_majority_vote():
    rows = _rows(("Nobody Band", "Nobody Band", "Jazz"),
                 ("Nobody Band", "Nobody Band", "Jazz"),
                 ("Nobody Band", "Nobody Band", "Blues"))
    r = run_genre_review(rows, use_online=False, progress_cb=lambda _: None, should_cancel=lambda: False)
    band = [a for a in r["artists"] if a["key"] == "nobody band"][0]
    assert band["source"] == "majority"
    assert band["canonical"] == ["Jazz"]
    assert band["changes"] == 1  # the Blues track


def test_review_holiday_preserved_and_unresolved():
    rows = _rows(("Mystery Act", "Mystery Act", ""),          # blank, unknown
                 ("Mystery Act", "Mystery Act", "Holiday"))   # sole Holiday
    r = run_genre_review(rows, use_online=False, progress_cb=lambda _: None, should_cancel=lambda: False)
    act = [a for a in r["artists"] if a["key"] == "mystery act"][0]
    assert act["source"] == "unresolved"
    assert act["holiday_preserved"] == 1


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  ok  {name}")
            except AssertionError as e:
                failed += 1
                print(f" FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
