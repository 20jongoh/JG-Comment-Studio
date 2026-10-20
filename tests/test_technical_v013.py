"""Offline tests: no real accounts, provider traffic, or YouTube comments."""
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.drafting import _post, validate_comment
from app.main import create_app
from app.technical_draft import make_technical_draft, validate_technical_comment
from app.youtube import CHANNELS, ServiceError, sample_context

KEY = "TEST_ONLY_SECRET_NEVER_REAL_API_KEY"
OWN_ID = "1ahZuTIiFtA"
OWN_CHANNEL = "UC" + "z" * 22
TITLE = "TrueNet Shopify App Review Screencast"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("An offline test must not access an external API.")
    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)


def own_video():
    return {"id": OWN_ID, "title": TITLE, "description": "Owned test fixture, not a live lookup.",
            "channel_id": OWN_CHANNEL, "channel_title": "Local test owner", "source": "youtube", "is_test": True,
            "url": "https://www.youtube.com/watch?v=" + OWN_ID, "thumbnail": "", "published_at": "2026-10-07T00:00:00Z"}


def test_fixed_draft_is_explicitly_not_ai_and_does_not_claim_analysis():
    result = make_technical_draft(own_video())
    assert result["valid"]
    assert result["source_mode"] == "technical_test"
    assert result["ai_provider"] is None and result["ai_model"] is None
    assert result["evidence_source"] == "title" and result["evidence_timestamp"] is None
    assert result["evidence_quote"] in TITLE and result["evidence_quote"] in result["comment"]
    assert 40 <= len(result["comment"]) <= 120
    assert result["comment"].startswith("[과제 시연]")
    assert "AI 분석이나 서비스 추천이 아닙니다" in result["comment"]
    assert "운명원" not in result["comment"]
    assert result["warnings"]


@pytest.mark.parametrize("patch", [{"is_test": False}, {"is_test": 1}, {"source": "sample"}])
def test_fixed_draft_rejects_wrong_source_or_test_flag(patch):
    with pytest.raises(ServiceError):
        make_technical_draft({**own_video(), **patch})


@pytest.mark.parametrize("title", ["", "abc", "###", "https://example.org", None])
def test_invalid_title_never_gets_fabricated_evidence(title):
    with pytest.raises(ServiceError):
        make_technical_draft({**own_video(), "title": title})


@pytest.mark.parametrize("title", [TITLE, "본인 테스트 영상입니다", "테스트 " * 25, "가나다라마바사아자차카타파하" * 4])
def test_fixed_comment_length_and_exact_source_with_varied_titles(title):
    result = make_technical_draft({**own_video(), "title": title})
    assert result["valid"] and 40 <= result["char_count"] <= 120
    assert result["evidence_quote"] in title


@pytest.mark.parametrize("change", ["remove_disclosure", "append_link", "append_brand", "replace_quote", "edit_text"])
def test_fixed_test_text_cannot_be_turned_into_unchecked_promotional_comment(change):
    result = make_technical_draft(own_video())
    text = result["comment"]
    if change == "remove_disclosure": text = text.replace("[과제 시연]", "")
    elif change == "append_link": text += " https://example.org"
    elif change == "append_brand": text += " 운명원"
    elif change == "replace_quote": text = text.replace(result["evidence_quote"], "An unrelated video")
    else: text = text.replace("점검하는", "완료한")
    assert not validate_technical_comment(text, result["evidence_quote"], TITLE)["valid"]


def test_default_marketing_policy_still_requires_the_brand():
    result = make_technical_draft(own_video())
    checks = validate_comment(result["comment"], result["evidence_quote"])
    assert not checks["valid"]
    assert any(c["name"] == "운명원 1회" and not c["ok"] for c in checks["checks"])


@pytest.mark.parametrize(("http", "status", "expected_code"), [
    (500, "INTERNAL", "ai_api_error"), (503, "UNAVAILABLE", "ai_api_error"),
    (504, "DEADLINE_EXCEEDED", "ai_api_error"), (401, "UNAUTHENTICATED", "ai_authorization"),
    (403, "PERMISSION_DENIED", "ai_authorization"), (429, "RESOURCE_EXHAUSTED", "ai_quota"),
    (400, "INVALID_ARGUMENT", "ai_configuration"), (404, "NOT_FOUND", "ai_configuration"),
    (422, None, "ai_configuration"), (502, None, "ai_api_error"), (302, None, "ai_api_error"),
])
def test_http_diagnostics_keep_status_without_leaking_response_or_keys(monkeypatch, caplog, http, status, expected_code):
    response = httpx.Response(http, json={"error": {"status": status, "message": KEY + " private-message", "details": [KEY]}})
    post = Mock(return_value=response)
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(ServiceError) as exc:
        _post("https://example.invalid/unused", {"x-goog-api-key": KEY},
              {"contents": [{"parts": [{"text": "private-video-material"}]}]}, "gemini")
    assert exc.value.code == expected_code
    assert f"HTTP {http}" in str(exc.value)
    if status: assert status in str(exc.value)
    combined = str(exc.value) + caplog.text
    for secret in (KEY, "private-message", "private-video-material", "example.invalid"):
        assert secret not in combined
    assert post.call_count == 1  # no hidden retry/cost increase


@pytest.mark.parametrize("payload", [
    {"error": {"status": KEY, "message": KEY}},
    {"error": {"status": [KEY]}}, {"error": [KEY]}, [KEY], None,
])
def test_unknown_status_is_not_echoed_or_invented(monkeypatch, payload, caplog):
    monkeypatch.setattr(httpx, "post", Mock(return_value=httpx.Response(500, json=payload)))
    with pytest.raises(ServiceError) as exc:
        _post("https://example.invalid/unused", {}, {}, "gemini")
    assert "HTTP 500" in str(exc.value)
    assert KEY not in str(exc.value) + caplog.text
    assert "/ INTERNAL" not in str(exc.value)


def test_html_error_body_is_never_shown(monkeypatch):
    monkeypatch.setattr(httpx, "post", Mock(return_value=httpx.Response(502, text="<h1>" + KEY + "</h1>")))
    with pytest.raises(ServiceError) as exc:
        _post("https://example.invalid/unused", {}, {}, "gemini")
    assert "HTTP 502" in str(exc.value) and KEY not in str(exc.value)


@pytest.fixture
def client(tmp_path, monkeypatch):
    class FakeYouTube:
        channel_id = OWN_CHANNEL
        handle_id = OWN_CHANNEL
        title = TITLE
        metadata_calls = 0
        context_calls = 0
        def __init__(self, key): pass
        def resolve_video(self, video_id):
            type(self).metadata_calls += 1
            return {**own_video(), "id": video_id, "channel_id": self.channel_id, "title": self.title, "is_test": False}
        def channel_id_for_handle(self, handle): return self.handle_id
        def context(self, video, manual_transcript=""):
            type(self).context_calls += 1
            ctx = sample_context("sample_1")
            ctx["video"] = video
            ctx.pop("sample_draft", None)
            ctx["transcript_source"] = "manual"
            return ctx
    monkeypatch.setattr("app.main.YouTubeService", FakeYouTube)
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        token = c.get("/api/bootstrap").json()["session_token"]
        c.headers["X-CSRF-Token"] = token
        result = c.post("/api/settings", json={"youtube_api_key": KEY, "ai_api_key": KEY, "test_video_url": "https://youtu.be/" + OWN_ID,
                   "test_channel_id": OWN_CHANNEL, "expected_handle": "@local-owner"})
        assert result.status_code == 200
        c.fake_provider = FakeYouTube
        yield c


def draft_request(client, **changes):
    return client.post("/api/analyze", json={"video_id": OWN_ID, "mode": "live", "draft_kind": "technical_test", **changes})


def test_technical_endpoint_never_calls_gemini_context_or_posting(client, monkeypatch):
    ai = Mock(side_effect=AssertionError("Technical tests must not call AI"))
    monkeypatch.setattr("app.main.make_draft", ai)
    response = draft_request(client)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source_mode"] == "technical_test" and result["mode"] == "live"
    assert result["comments_count"] == 0 and result["transcript_source"] == "none"
    assert client.fake_provider.metadata_calls == 1 and client.fake_provider.context_calls == 0
    assert ai.call_count == 0
    assert client.get("/api/history").json()["items"] == []
    assert client.get("/api/bootstrap").json()["stats"]["total_success"] == 0
    assert KEY not in response.text
    assert client.post("/api/validate", json={"analysis_id": result["analysis_id"], "comment": result["comment"]}).json()["valid"]


@pytest.mark.parametrize("changes", [
    {"mode": "sample", "video_id": "sample_1"},
    {"transcript": "This text must not silently be described as analyzed."},
    {"video_id": "abcdefghijk"},
])
def test_technical_scope_rejects_sample_manual_text_or_another_video(client, changes):
    assert draft_request(client, **changes).status_code == 400
    assert client.fake_provider.context_calls == 0


def test_allowlisted_external_video_still_cannot_get_technical_no_brand_policy(client):
    client.fake_provider.channel_id = CHANNELS[0]["id"]
    result = draft_request(client)
    assert result.status_code == 400
    assert "본인 테스트 영상" in result.json()["detail"]


def test_client_cannot_forge_source_mode_test_flag_or_unknown_kind(client):
    for change in ({"is_test": True}, {"source_mode": "technical_test"}, {"draft_kind": "skip_all_checks"}):
        assert draft_request(client, **change).status_code == 422


def test_ai_failure_is_not_silently_turned_into_technical_success(client, monkeypatch):
    def fail(*args, **kwargs):
        raise ServiceError("Gemini 서버 오류 (HTTP 500 / INTERNAL)", "ai_api_error")
    monkeypatch.setattr("app.main.make_draft", fail)
    response = client.post("/api/analyze", json={"video_id": OWN_ID, "mode": "live"})
    assert response.status_code == 400 and "HTTP 500" in response.json()["detail"]
    assert client.fake_provider.context_calls == 1
    with client.app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 0
    assert client.get("/api/history").json()["items"] == []


def test_ai_and_test_actions_remain_independent(client, monkeypatch):
    expected = deepcopy(sample_context("sample_1")["sample_draft"])
    ai = Mock(return_value={**expected, "source_mode": "ai", "ai_provider": "gemini", "warnings": []})
    monkeypatch.setattr("app.main.make_draft", ai)
    response = client.post("/api/analyze", json={"video_id": OWN_ID, "mode": "live", "draft_kind": "ai"})
    assert response.status_code == 200
    assert response.json()["source_mode"] == "ai"
    assert ai.call_count == 1 and client.fake_provider.context_calls == 1
    assert "운명원" in response.json()["comment"]


@pytest.mark.parametrize("case", ["confirmation", "handle", "title", "settings", "edited_comment"])
def test_technical_publish_still_enforces_review_identity_current_scope_and_evidence(client, case):
    draft = draft_request(client).json()
    body = {"analysis_id": draft["analysis_id"], "comment": draft["comment"], "confirmed": True}
    if case == "confirmation": body["confirmed"] = False
    elif case == "handle": client.fake_provider.handle_id = "UC" + "a" * 22
    elif case == "title": client.fake_provider.title = "Changed title, unrelated content"
    elif case == "settings":
        assert client.post("/api/settings", json={"test_video_url": "https://youtu.be/abcdefghijk"}).status_code == 200
    else: body["comment"] = body["comment"].replace("테스트 문구", "홍보 문구")
    response = client.post("/api/publish", json=body)
    assert response.status_code == 400, response.text
    assert client.app.state.store.history() == []


def test_technical_job_uses_existing_duplicate_lock_and_keeps_approved_text(client, monkeypatch):
    draft = draft_request(client).json()
    captured = []
    async def simulated_publish(job, values, verify):
        verify()
        captured.append(job)
        client.app.state.store.mark_post_started(job["id"], values["daily_limit"])
        client.app.state.store.finish(job["id"], "unknown", "Offline simulated outcome; no YouTube access")
    monkeypatch.setattr(client.app.state.manager, "publish", simulated_publish)
    body = {"analysis_id": draft["analysis_id"], "comment": draft["comment"], "confirmed": True}
    assert client.post("/api/publish", json=body).status_code == 200
    assert len(captured) == 1 and captured[0]["comment"] == draft["comment"]
    assert client.post("/api/publish", json=body).status_code == 400
    assert len(captured) == 1
    assert client.get("/api/bootstrap").json()["stats"]["total_success"] == 0


def test_technical_mode_does_not_bypass_daily_limit(client):
    draft = draft_request(client).json()
    assert client.post("/api/settings", json={"daily_limit": 1}).status_code == 200
    store = client.app.state.store
    job = store.reserve({**own_video(), "id": "abcdefghijk"}, "other offline test", "유튜브1", "live", 1)
    store.finish(job["id"], "unknown", "Offline simulated record")
    response = client.post("/api/publish", json={"analysis_id": draft["analysis_id"], "comment": draft["comment"], "confirmed": True})
    assert response.status_code == 400
    assert "상한" in response.json()["detail"]


def test_partial_install_has_visible_version_and_new_assets(client):
    assert client.get("/api/bootstrap").json()["version"] == "0.1.4"
    html = client.get("/").text
    assert "technical-test-button" in html and "app.js?v=0.1.4" in html
    js = client.get("/static/app.js?v=0.1.4").text
    assert 'draft_kind: technical ? "technical_test" : "ai"' in js
