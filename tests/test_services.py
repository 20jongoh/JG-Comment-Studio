"""Offline service boundary tests. No API keys, HTTP traffic, or sockets needed.

Run only these tests with: python -m pytest tests/test_services.py -q
Live API compatibility and actual YouTube posting still require user verification.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import Mock

import httpx
import pytest

from app.drafting import make_draft, validate_comment
from app.youtube import (
    CHANNELS,
    ServiceError,
    YouTubeService,
    parse_video_id,
    sample_context,
)


KEY_SENTINEL = "TEST_ONLY_KEY_MUST_NOT_APPEAR_IN_ERRORS"


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("This service test must not make real HTTP requests.")

    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)


@pytest.fixture
def context():
    return {
        "video": {
            "id": "abcdefghijk",
            "title": "답장이 늦을 때 마음을 전하는 법",
            "description": "API 경계 검사용으로 작성한 가상 원문입니다.",
            "source": "youtube",
        },
        "transcript": "답장 속도보다 약속을 지키는 태도를 살펴보세요.",
        "transcript_segments": [{
            "text": "답장 속도보다 약속을 지키는 태도를 살펴보세요.",
            "start": 12.5,
            "duration": 7.0,
        }],
        "transcript_source": "youtube_captions",
        "comments": [],
        "warnings": [],
    }


@pytest.fixture
def valid_draft():
    return {
        "worry": "답장이 늦을 때 상대의 마음을 무엇으로 판단할지 고민합니다.",
        "evidence_quote": "약속을 지키는 태도",
        "evidence_source": "transcript",
        "evidence_timestamp": None,
        "comment": "[과제 시연] ‘약속을 지키는 태도’를 고민의 단서로 삼았어요. 운명원 연애 사주에서도 나의 관계 패턴을 어떤 질문으로 풀어볼까요?",
    }


def openai_response(draft):
    return httpx.Response(200, json={
        "status": "completed",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": json.dumps(draft, ensure_ascii=False)}],
        }],
    })


@pytest.mark.parametrize("value", [
    "https://youtube.com.evil.example/watch?v=abcdefghijk",
    "https://someone:password@youtube.com/watch?v=abcdefghijk",
    "https://youtube.com:443/watch?v=abcdefghijk",
    "https://youtube.com:/watch?v=abcdefghijk",
    "https://youtube.com/watch?v=abcdefghijk&v=12345678901",
    "https://youtube.com/redirect?q=abcdefghijk",
    "https://youtube.com\\@evil.example/watch?v=abcdefghijk",
])
def test_hostile_video_urls_are_rejected(value):
    with pytest.raises(ServiceError) as error:
        parse_video_id(value)
    assert error.value.code == "invalid_video_url"


@pytest.mark.parametrize("video_id", ["sample_1", "sample_2", "sample_3"])
def test_sample_drafts_are_explicitly_synthetic_and_offline(video_id):
    source = sample_context(video_id)
    result = make_draft(source, "gemini", KEY_SENTINEL, "unused-model")
    assert source["video"]["source"] == "sample"
    assert source["transcript_source"] == "sample"
    assert result["source_mode"] == "sample"
    assert result["ai_provider"] is None
    assert result["warnings"]
    assert 40 <= len(result["comment"]) <= 120
    assert result["comment"].startswith("[과제 시연]")
    assert result["comment"].count("운명원") == 1
    assert result["evidence_quote"] in source["transcript"]
    assert result["evidence_quote"] in result["comment"]


def test_keyless_real_context_is_a_template_not_ai(context):
    result = make_draft(context, "openai", "", "unused-model")
    assert result["source_mode"] == "template"
    assert result["ai_provider"] is None
    assert result["warnings"]


def test_absent_source_quote_rejected_after_only_one_repair(monkeypatch, context, valid_draft):
    invalid = copy.deepcopy(valid_draft)
    invalid["evidence_quote"] = "자료에 없는 인용"
    invalid["comment"] = "[과제 시연] ‘자료에 없는 인용’을 질문의 단서로 삼아 운명원 연애 사주에서 어떤 고민을 다룰지 정리하는 예시입니다."
    # This fixture meets the text-format rules; its defect is missing evidence.
    assert validate_comment(invalid["comment"], invalid["evidence_quote"])["valid"]
    post = Mock(return_value=openai_response(invalid))
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(ServiceError) as error:
        make_draft(context, "openai", KEY_SENTINEL, "test-model")
    assert error.value.code == "draft_validation_failed"
    assert post.call_count == 2


def test_one_repair_may_succeed_but_does_not_skip_validation(monkeypatch, context, valid_draft):
    invalid = {**valid_draft, "comment": valid_draft["comment"].replace("[과제 시연]", "")}
    post = Mock(side_effect=[openai_response(invalid), openai_response(valid_draft)])
    monkeypatch.setattr(httpx, "post", post)
    result = make_draft(context, "openai", KEY_SENTINEL, "test-model")
    assert post.call_count == 2
    assert result["source_mode"] == "ai"
    assert result["valid"]
    assert result["comment"].startswith("[과제 시연]")


@pytest.mark.parametrize(("source", "expected"), [
    ("youtube_captions", 12.5),
    ("manual", None),
])
def test_timestamp_comes_from_available_captions_not_model_guess(monkeypatch, context, valid_draft, source, expected):
    context["transcript_source"] = source
    guessed_time = {**valid_draft, "evidence_timestamp": 999.0}
    monkeypatch.setattr(httpx, "post", Mock(return_value=openai_response(guessed_time)))
    result = make_draft(context, "openai", KEY_SENTINEL, "test-model")
    assert result["evidence_timestamp"] == expected


@pytest.mark.parametrize("failure", ["network", "api_response"])
def test_youtube_errors_do_not_expose_keys_or_raw_diagnostics(monkeypatch, failure):
    if failure == "network":
        get = Mock(side_effect=httpx.ConnectError("URL failed: https://example.test/?key=" + KEY_SENTINEL))
    else:
        get = Mock(return_value=httpx.Response(403, json={
            "error": {"errors": None, "message": "private diagnostic: " + KEY_SENTINEL},
        }))
    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(ServiceError) as error:
        YouTubeService(KEY_SENTINEL).resolve_video("abcdefghijk")
    assert KEY_SENTINEL not in str(error.value)
    assert "private diagnostic" not in str(error.value)
    assert "https://" not in str(error.value)


def test_ai_authorization_error_is_safe_and_never_template_success(monkeypatch, context):
    post = Mock(return_value=httpx.Response(401, json={
        "error": {"message": "private diagnostic: " + KEY_SENTINEL},
    }))
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(ServiceError) as error:
        make_draft(context, "openai", KEY_SENTINEL, "test-model")
    assert error.value.code == "ai_authorization"
    assert KEY_SENTINEL not in str(error.value)
    assert "private diagnostic" not in str(error.value)
    assert post.call_count == 1


@pytest.mark.parametrize("handle", ["sample-owner", "@sample-owner"])
def test_handle_resolves_via_official_channel_identity(monkeypatch, handle):
    channel_id = "UCxlc0Ky0aWoD8uI09ad44Tg"
    get = Mock(return_value=httpx.Response(200, json={"items": [{"id": channel_id}]}))
    monkeypatch.setattr(httpx, "get", get)
    assert YouTubeService(KEY_SENTINEL).channel_id_for_handle(handle) == channel_id
    request = get.call_args
    assert request.args[0] == "https://www.googleapis.com/youtube/v3/channels"
    assert request.kwargs["params"]["forHandle"] == "@sample-owner"
    assert request.kwargs["params"]["part"] == "id"


def test_unknown_handle_does_not_produce_a_guessed_channel_id(monkeypatch):
    monkeypatch.setattr(httpx, "get", Mock(return_value=httpx.Response(200, json={"items": []})))
    with pytest.raises(ServiceError) as error:
        YouTubeService(KEY_SENTINEL).channel_id_for_handle("@missing-owner")
    assert error.value.code == "handle_not_found"


def test_one_failed_channel_preserves_four_successes_and_actual_video_dates(monkeypatch):
    records = {
        f"playlist_{index}": {"channel": channel, "video_id": f"video{index:06d}"}
        for index, channel in enumerate(CHANNELS, start=1)
    }
    failed_playlist = "playlist_3"
    failed_channel = records[failed_playlist]["channel"]

    def fake_get(url, **kwargs):
        resource = url.rsplit("/", 1)[-1]
        params = kwargs["params"]
        if resource == "channels":
            return httpx.Response(200, json={"items": [{
                "id": record["channel"]["id"],
                "contentDetails": {"relatedPlaylists": {"uploads": playlist}},
            } for playlist, record in records.items()]})
        if resource == "playlistItems":
            playlist = params["playlistId"]
            if playlist == failed_playlist:
                raise httpx.TimeoutException("request timed out; key=" + KEY_SENTINEL)
            return httpx.Response(200, json={"items": [{
                "contentDetails": {"videoId": records[playlist]["video_id"]},
                "snippet": {"publishedAt": "2001-01-01T00:00:00Z"},
            }]})
        if resource == "videos":
            requested = set(params["id"].split(","))
            return httpx.Response(200, json={"items": [{
                "id": record["video_id"],
                "snippet": {
                    "title": "API 응답 테스트용 창작 제목",
                    "channelId": record["channel"]["id"],
                    "channelTitle": record["channel"]["name"],
                    "publishedAt": "2026-10-19T09:00:00Z",
                    "description": "가상 HTTP 응답 자료입니다.",
                },
            } for record in records.values() if record["video_id"] in requested]})
        raise AssertionError("Unexpected API resource in the offline test.")

    monkeypatch.setattr(httpx, "get", fake_get)
    result = YouTubeService(KEY_SENTINEL).latest_videos()
    assert len(result["videos"]) == 4
    assert {video["channel_id"] for video in result["videos"]} == {
        channel["id"] for channel in CHANNELS if channel["id"] != failed_channel["id"]
    }
    assert all(video["published_at"] == "2026-10-19T09:00:00Z" for video in result["videos"])
    assert any(failed_channel["name"] in warning for warning in result["warnings"])
    assert KEY_SENTINEL not in json.dumps(result, ensure_ascii=False)
