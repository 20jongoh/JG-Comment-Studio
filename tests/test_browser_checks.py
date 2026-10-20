"""Pure result-verification guards; no real login or posting is performed."""

from urllib.parse import parse_qs, urlparse

import pytest

from app.browser import BrowserManager, normalized_comment, normalized_handle


VIDEO_ID = "abcdEFG1234"


def test_permalink_accepts_the_same_video_and_retains_one_comment_identifier():
    for href in (
        "/watch?v=abcdEFG1234&lc=UgxComment.abc123",
        "https://www.youtube.com/watch?v=abcdEFG1234&lc=UgxComment.abc123&feature=share",
        "https://youtube.com/watch?lc=UgxComment.abc123&v=abcdEFG1234",
    ):
        result = BrowserManager._valid_permalink(href, VIDEO_ID)
        assert result is not None
        parsed = urlparse(result)
        assert parsed.scheme == "https"
        assert parsed.hostname == "www.youtube.com"
        assert parsed.path == "/watch"
        assert parse_qs(parsed.query) == {"v": [VIDEO_ID], "lc": ["UgxComment.abc123"]}


@pytest.mark.parametrize(
    "href",
    [
        None,
        "https://www.youtube.com/watch?v=OTHER_VIDEO&lc=UgxComment",
        "https://www.youtube.com.evil.example/watch?v=abcdEFG1234&lc=UgxComment",
        "http://www.youtube.com/watch?v=abcdEFG1234&lc=UgxComment",
        "https://www.youtube.com/watch?v=abcdEFG1234",
        "https://www.youtube.com/watch?v=abcdEFG1234&lc=",
        "https://www.youtube.com/redirect?v=abcdEFG1234&lc=UgxComment",
        "https://www.youtube.com/watch?v=abcdEFG1234&lc=First&lc=Second",
        "https://www.youtube.com/watch?v=abcdEFG1234&v=OTHER_VIDEO&lc=UgxComment",
    ],
)
def test_permalink_rejects_wrong_target_or_ambiguous_comment(href):
    assert BrowserManager._valid_permalink(href, VIDEO_ID) is None


def test_encoded_permalink_cannot_introduce_extra_query_parameters():
    href = "https://www.youtube.com/watch?v=abcdEFG1234&lc=Comment%26v%3DOTHER_VIDEO"
    result = BrowserManager._valid_permalink(href, VIDEO_ID)
    # Reject a malformed identifier or preserve it as a single encoded value.
    # Never interpolate the decoded ampersand into a navigable query string.
    if result is not None:
        query = parse_qs(urlparse(result).query)
        assert query == {"v": [VIDEO_ID], "lc": ["Comment&v=OTHER_VIDEO"]}


def test_handle_comparison_decodes_urls_without_equating_different_accounts():
    assert normalized_handle("  @PocketNori  ") == normalized_handle("@pocketnori")
    assert normalized_handle("@%EA%B9%80%EC%A2%85%EC%98%A4") == normalized_handle("@김종오")
    assert normalized_handle("@PocketNori2") != normalized_handle("@PocketNori")


def test_comment_comparison_allows_layout_whitespace_but_requires_whole_text():
    expected = "[과제 시연] 영상의 실제 표현을 확인합니다. 운명원"
    assert normalized_comment("\n[과제 시연]  영상의 실제 표현을\n확인합니다. 운명원\t") == normalized_comment(expected)
    assert normalized_comment(expected + " 다른 내용") != normalized_comment(expected)
    assert normalized_comment(expected.replace("확인합니다", "보장합니다")) != normalized_comment(expected)
    assert normalized_comment(expected.replace(".", "!")) != normalized_comment(expected)
