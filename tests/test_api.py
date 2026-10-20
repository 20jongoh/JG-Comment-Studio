"""Local API boundary tests. No external network or real comments are sent."""

from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.youtube import CHANNELS, sample_context


@pytest.fixture
def client(tmp_path):
    application = create_app(tmp_path)
    with TestClient(application, base_url="http://127.0.0.1:8765") as connection:
        token = connection.get("/api/bootstrap").json()["session_token"]
        connection.headers["X-CSRF-Token"] = token
        yield connection


def sample_analysis(client):
    response = client.post("/api/analyze", json={"video_id": "sample_1", "mode": "sample"})
    assert response.status_code == 200, response.text
    return response.json()


def test_secret_settings_are_never_sent_back(client):
    result = client.post("/api/settings", json={"youtube_api_key": "private_youtube_key", "ai_api_key": "private_ai_key"})
    assert result.status_code == 200
    assert result.json()["settings"]["youtube_api_key_set"] is True
    assert "private_youtube_key" not in result.text
    assert "private_ai_key" not in result.text
    assert "private_" not in client.get("/api/bootstrap").text


def test_mutations_require_current_session_token(client):
    result = client.post("/api/settings", json={"daily_limit": 3}, headers={"X-CSRF-Token": "wrong"})
    assert result.status_code == 403
    assert client.get("/api/bootstrap").json()["settings"]["daily_limit"] == 10


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example:8765"},
    {"Origin": "https://attacker.example"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_cross_site_and_dns_rebinding_are_blocked(client, headers):
    assert client.get("/api/bootstrap", headers=headers).status_code == 403


def test_offline_samples_never_claim_to_be_live_or_ai(client):
    videos = client.get("/api/videos?mode=sample").json()
    assert len(videos["videos"]) == 3
    assert all(video["source"] == "sample" for video in videos["videos"])
    draft = sample_analysis(client)
    assert draft["source_mode"] == "sample"
    assert draft["mode"] == "sample"
    assert draft["evidence_quote"] in draft["comment"]
    assert draft["warnings"]


def test_offline_live_read_fails_instead_of_showing_fake_latest(client):
    response = client.get("/api/videos?mode=live")
    assert response.status_code == 400
    assert "API" in response.json()["detail"]
    assert "videos" not in response.json()


def test_comment_edit_must_keep_exact_evidence(client):
    draft = sample_analysis(client)
    response = client.post("/api/validate", json={"analysis_id": draft["analysis_id"], "comment": draft["comment"].replace(draft["evidence_quote"], "전혀 다른 이야기의 문구")})
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert any(not check["ok"] and check["name"] == "근거 문구 포함" for check in response.json()["checks"])


def test_samples_cannot_be_published_even_with_confirmation(client):
    draft = sample_analysis(client)
    response = client.post("/api/publish", json={"analysis_id": draft["analysis_id"], "comment": draft["comment"], "confirmed": True})
    assert response.status_code == 400
    assert client.get("/api/history").json()["items"] == []


@pytest.mark.parametrize("url", [
    "https://example.org/watch?v=abcdefghijk",
    "https://www.youtube.com.attacker.example/watch?v=abcdefghijk",
    "https://youtube.com@attacker.example/watch?v=abcdefghijk",
    "https://youtube.com:444/watch?v=abcdefghijk",
])
def test_unapproved_url_is_rejected_before_provider(client, url):
    response = client.post("/api/videos/resolve", json={"url": url})
    assert response.status_code == 400


@pytest.fixture
def provider(monkeypatch):
    class FakeProvider:
        channel_id = "UC" + "z" * 22
        handle_channel_id = channel_id
        def __init__(self, api_key):
            pass
        def resolve_video(self, video_id):
            return {"id": video_id, "title": "연락의 일관성을 생각하는 연습", "description": "답장 속도보다 대화의 일관성을 보세요.",
                    "channel_id": self.channel_id, "channel_title": "테스트 채널", "source": "youtube", "is_test": False,
                    "url": "https://www.youtube.com/watch?v=" + video_id, "thumbnail": "", "published_at": "2026-10-01T00:00:00Z"}
        def channel_id_for_handle(self, handle):
            return self.handle_channel_id
    monkeypatch.setattr("app.main.YouTubeService", FakeProvider)
    return FakeProvider


def live_analysis(client, provider):
    # Save a server-held analysis; the client cannot supply its own channel metadata.
    sample = sample_context("sample_1")
    draft = deepcopy(sample["sample_draft"])
    draft.update({"video": provider("").resolve_video("abcdefghijk"), "source_mode": "ai", "mode": "live"})
    return client.app.state.store.put_analysis(draft)


def configure_own_test(client, provider):
    result = client.post("/api/settings", json={"youtube_api_key": "test_api_key",
        "test_video_url": "https://youtu.be/abcdefghijk", "test_channel_id": provider.channel_id,
        "expected_handle": "@test-owner"})
    assert result.status_code == 200, result.text


def test_link_scope_checks_resolved_channel_not_client_claim(client, provider):
    response = client.post("/api/videos/resolve", json={"url": "https://youtu.be/abcdefghijk"})
    assert response.status_code == 400
    configure_own_test(client, provider)
    assert client.post("/api/videos/resolve", json={"url": "https://youtu.be/abcdefghijk"}).json()["video"]["is_test"] is True
    # A different video in the same own channel still isn't an allowed test target.
    assert client.post("/api/videos/resolve", json={"url": "https://youtu.be/abcdefghi00"}).status_code == 400


def test_external_allowed_channel_cannot_use_posting_endpoint(client, provider):
    provider.channel_id = CHANNELS[0]["id"]
    draft = live_analysis(client, provider)
    response = client.post("/api/publish", json={"analysis_id": draft["analysis_id"], "comment": draft["comment"], "confirmed": True})
    assert response.status_code == 400
    assert client.app.state.store.history() == []


def test_post_requires_review_confirmation(client, provider):
    configure_own_test(client, provider)
    draft = live_analysis(client, provider)
    response = client.post("/api/publish", json={"analysis_id": draft["analysis_id"], "comment": draft["comment"]})
    assert response.status_code == 400
    assert client.app.state.store.history() == []


def test_claimed_test_channel_must_match_actual_posting_handle(client, provider):
    configure_own_test(client, provider)
    provider.handle_channel_id = "UC" + "x" * 22
    draft = live_analysis(client, provider)
    response = client.post("/api/publish", json={"analysis_id": draft["analysis_id"], "comment": draft["comment"], "confirmed": True})
    assert response.status_code == 400
    assert "핸들" in response.json()["detail"]
    assert client.app.state.store.history() == []


def test_video_channel_is_rechecked_before_reservation(client, provider):
    configure_own_test(client, provider)
    draft = live_analysis(client, provider)
    provider.channel_id = "UC" + "y" * 22
    response = client.post("/api/publish", json={"analysis_id": draft["analysis_id"], "comment": draft["comment"], "confirmed": True})
    assert response.status_code == 400
    assert client.app.state.store.history() == []


def test_valid_scoped_post_schedules_once_and_keeps_exact_text(client, provider, monkeypatch):
    configure_own_test(client, provider)
    draft = live_analysis(client, provider)
    captured = []
    async def no_network_publish(job, settings, verify):
        captured.append(job)
        client.app.state.store.mark_post_started(job["id"], settings["daily_limit"])
        client.app.state.store.finish(job["id"], "unknown", "테스트용 모의 처리: 실제 게시 없음")
    monkeypatch.setattr(client.app.state.manager, "publish", no_network_publish)
    body = {"analysis_id": draft["analysis_id"], "comment": draft["comment"], "confirmed": True}
    response = client.post("/api/publish", json=body)
    assert response.status_code == 200
    assert captured[0]["comment"] == draft["comment"]
    assert captured[0]["video_id"] == "abcdefghijk"
    assert client.post("/api/publish", json=body).status_code == 400
    assert len(captured) == 1
