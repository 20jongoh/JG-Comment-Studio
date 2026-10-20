"""Offline regression tests: synthetic replies only; no real YouTube requests."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import create_app
from app.browser import BrowserManager
from app.storage import Store, RuleError
from app.verification import verify_existing_comment, proof_matches_job
from app.youtube import YouTubeService, ServiceError

VID = "abcdEFG1234"
CID = "UC" + "a" * 22
COMMENT = "[과제 시연] 원래 승인한 댓글 전문입니다. 브라우저 확인용 테스트입니다."
COMMENT_ID = "UgxSyntheticOnly123"
STAMP = "2026-10-19T14:26:47Z"


def job():
    return dict(id="a" * 32, video_id=VID, channel_id=CID, comment=COMMENT,
                mode="live", status="unknown", post_started=1, created_at="2026-10-19T23:26:33+09:00")


def top_comment():
    return {"id": COMMENT_ID, "snippet": {"authorChannelId": {"value": CID},
            "textDisplay": COMMENT, "publishedAt": STAMP}}


def thread():
    return {"id": "ThreadIdIsNotCommentId", "snippet": {
        "videoId": VID, "channelId": CID, "topLevelComment": top_comment()}}


class FixtureService:
    def __init__(self, first=None, second=None):
        self.first = {"items": [thread()]} if first is None else first
        self.second = {"items": [top_comment()]} if second is None else second
        self.calls = []
    def _get(self, resource, params):
        assert resource in {"commentThreads", "comments"}
        self.calls.append((resource, deepcopy(params)))
        return deepcopy(self.first if resource == "commentThreads" else self.second)


def proof(j=None):
    return verify_existing_comment(j or job(), FixtureService())


def test_two_read_evidence_binds_original_comment_to_video_and_author_id():
    svc = FixtureService()
    result = verify_existing_comment(job(), svc)
    assert result["comment_id"] == COMMENT_ID  # NOT the thread ID
    assert result["author_channel_id"] == CID
    assert result["comment_url"].endswith("&lc=" + COMMENT_ID)
    assert proof_matches_job(result, job())
    assert [r for r, _ in svc.calls] == ["commentThreads", "comments"]
    assert all(p["textFormat"] == "plainText" for _, p in svc.calls)
    assert svc.calls[0][1]["videoId"] == VID
    assert svc.calls[1][1]["id"] == COMMENT_ID


@pytest.mark.parametrize("fault", ["wrong_video", "wrong_owner", "wrong_author", "display_name_only",
    "partial_body", "extra_body", "punctuation", "reply", "missing_time", "bad_time", "held", "bad_id", "different_video_in_top"])
def test_first_read_never_accepts_unrelated_or_insufficient_evidence(fault):
    row = thread(); sn = row["snippet"]; top = sn["topLevelComment"]; ts = top["snippet"]
    if fault == "wrong_video": sn["videoId"] = "xyz12345678"
    elif fault == "wrong_owner": sn["channelId"] = "UC" + "b" * 22
    elif fault == "wrong_author": ts["authorChannelId"]["value"] = "UC" + "b" * 22
    elif fault == "display_name_only": ts.pop("authorChannelId"); ts["authorDisplayName"] = "same name"
    elif fault == "partial_body": ts["textDisplay"] = COMMENT[:20]
    elif fault == "extra_body": ts["textDisplay"] += " 변경"
    elif fault == "punctuation": ts["textDisplay"] = COMMENT.replace(".", "!")
    elif fault == "reply": ts["parentId"] = "parent"
    elif fault == "missing_time": ts.pop("publishedAt")
    elif fault == "bad_time": ts["publishedAt"] = "not-a-time"
    elif fault == "held": ts["moderationStatus"] = "heldForReview"
    elif fault == "bad_id": top["id"] = "x&v=other"
    elif fault == "different_video_in_top": ts["videoId"] = "xyz12345678"
    svc = FixtureService(first={"items": [row]})
    with pytest.raises(ServiceError): verify_existing_comment(job(), svc)
    assert len(svc.calls) == 1


@pytest.mark.parametrize("fault", ["wrong_id", "wrong_author", "changed_body", "reply", "time_changed", "video_changed", "deleted", "duplicate_item"])
def test_second_read_must_independently_confirm_same_comment(fault):
    top = top_comment(); sn = top["snippet"]; items = [top]
    if fault == "wrong_id": top["id"] += "different"
    elif fault == "wrong_author": sn["authorChannelId"]["value"] = "UC" + "b" * 22
    elif fault == "changed_body": sn["textDisplay"] += "modified"
    elif fault == "reply": sn["parentId"] = "parent"
    elif fault == "time_changed": sn["publishedAt"] = "2026-10-18T14:26:47Z"
    elif fault == "video_changed": sn["videoId"] = "xyz12345678"
    elif fault == "deleted": items = []
    elif fault == "duplicate_item": items.append(deepcopy(top))
    with pytest.raises(ServiceError, match="재게시"):
        verify_existing_comment(job(), FixtureService(second={"items": items}))


def test_multiple_matching_comments_are_not_silently_chosen():
    second = thread(); second["snippet"]["topLevelComment"]["id"] += "two"
    with pytest.raises(ServiceError) as exc:
        verify_existing_comment(job(), FixtureService(first={"items": [thread(), second]}))
    assert exc.value.code == "comment_ambiguous"


def test_missing_video_binding_is_not_inferred_from_the_request():
    row = thread(); row["snippet"].pop("videoId")
    with pytest.raises(ServiceError): verify_existing_comment(job(), FixtureService(first={"items": [row]}))


def test_layout_whitespace_is_tolerated_but_not_arbitrary_text_rewriting():
    row = thread(); row["snippet"]["topLevelComment"]["snippet"]["textDisplay"] = "\n" + COMMENT.replace(" ", "  \n")
    assert verify_existing_comment(job(), FixtureService(first={"items": [row]}))["comment_id"] == COMMENT_ID


def test_pagination_is_bounded_and_only_follows_returned_tokens():
    class Pages(FixtureService):
        def _get(self, resource, params):
            if resource == "comments": return super()._get(resource, params)
            self.calls.append((resource, deepcopy(params)))
            if not params.get("pageToken"): return {"items": [], "nextPageToken": "next"}
            assert params["pageToken"] == "next"
            return {"items": [thread()]}
    svc = Pages(); assert verify_existing_comment(job(), svc)["comment_id"] == COMMENT_ID
    assert len(svc.calls) == 3


def test_scan_limit_cannot_be_misrepresented_as_full_search():
    class Endless(FixtureService):
        def _get(self, resource, params):
            self.calls.append((resource, params))
            return {"items": [thread()], "nextPageToken": str(len(self.calls))}
    svc = Endless()
    with pytest.raises(ServiceError) as exc: verify_existing_comment(job(), svc)
    assert exc.value.code == "comment_scan_incomplete" and len(svc.calls) == 3


@pytest.mark.parametrize("body", [{}, {"items": "bad"}, {"items": None}])
def test_malformed_api_response_cannot_be_a_success(body):
    with pytest.raises(ServiceError): verify_existing_comment(job(), FixtureService(first=body))


def test_repeated_pagination_token_stops():
    with pytest.raises(ServiceError) as exc:
        verify_existing_comment(job(), FixtureService(first={"items": [], "nextPageToken": "same"}))
    assert exc.value.code == "comment_pagination_invalid"


@pytest.mark.parametrize("change", [{"mode": "sample"}, {"post_started": 0}, {"video_id": "bad"}, {"channel_id": "@not-an-id"}, {"comment": ""}])
def test_unsubmitted_or_invalid_job_cannot_be_verified(change):
    svc = FixtureService()
    with pytest.raises(ServiceError): verify_existing_comment({**job(), **change}, svc)
    assert svc.calls == []


def test_actual_service_only_uses_get_and_never_leaks_key_into_returned_evidence(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        assert url.startswith("https://www.googleapis.com/youtube/v3/")
        body = {"items": [thread()]} if url.endswith("commentThreads") else {"items": [top_comment()]}
        return httpx.Response(200, json=body)
    monkeypatch.setattr(httpx, "get", get)
    def forbidden(*args, **kwargs): pytest.fail("An external mutation must not be called")
    for name in ("post", "put", "patch", "delete"):
        monkeypatch.setattr(httpx, name, forbidden)
    result = verify_existing_comment(job(), YouTubeService("secret_for_fixture"))
    assert len(calls) == 2 and "secret_for_fixture" not in str(result)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_http_errors_remain_unknown_and_do_not_leak_secret(monkeypatch, status):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(status, json={"error": {"message": "secret_for_fixture"}}))
    with pytest.raises(ServiceError) as exc: verify_existing_comment(job(), YouTubeService("secret_for_fixture"))
    assert "secret_for_fixture" not in str(exc.value)


def own_video():
    return {"id": VID, "channel_id": CID, "title": "Synthetic own test", "source": "youtube"}


def unknown(store):
    j = store.reserve(own_video(), COMMENT, "유튜브1", "live", 10)
    store.mark_post_started(j["id"], 10)
    store.finish(j["id"], "unknown", "old check failed", error="old DOM result", screenshot_url="/api/screenshots/old.png")
    return store.job(j["id"])


def test_old_database_migrates_additively_preserving_records_and_locks(tmp_path):
    store = Store(tmp_path / "db.sqlite3"); original = unknown(store)
    with store.connect() as conn: conn.execute("ALTER TABLE jobs DROP COLUMN verification")
    newer = Store(store.path); recovered = newer.job(original["id"])
    assert recovered == original
    with pytest.raises(RuleError): newer.reserve(own_video(), COMMENT, "유튜브1", "live", 10)


def test_recheck_updates_same_row_and_preserves_day_quota_and_prior_error(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite3"); original = unknown(store)
    started = store.begin_verification(original["id"])
    assert started["status"] == "running" and started["post_started"] == 1
    for key in ("id", "day_key", "created_at", "comment", "video_id", "channel_id"):
        assert started[key] == original[key]
    ev = proof(started)
    store.finish(started["id"], "success", "API read confirmed", comment_url=ev["comment_url"], verification=ev)
    latest = store.job(original["id"])
    assert len(store.history()) == 1
    assert latest["verification"] == ev and latest["screenshot_url"] == original["screenshot_url"]
    assert "old DOM result" in str(latest["events"])
    assert store.stats(10)["total_success"] == 1
    with pytest.raises(RuleError): store.reserve(own_video(), COMMENT, "유튜브1", "live", 10)
    from app import storage
    monkeypatch.setattr(storage, "today", lambda: "2099-01-01")
    assert store.stats(10)["daily_used"] == 0
    assert store.job(original["id"])["day_key"] == original["day_key"]


def test_invalid_proof_is_not_saved_as_success(tmp_path):
    store = Store(tmp_path / "db.sqlite3"); j = unknown(store); store.begin_verification(j["id"])
    p = proof(j); p["author_channel_id"] = "UC" + "b" * 22
    with pytest.raises(RuleError): store.finish(j["id"], "success", "not valid", comment_url=p["comment_url"], verification=p)
    assert store.job(j["id"])["status"] == "running"


def test_interruption_during_recheck_never_unlocks_video(tmp_path):
    store = Store(tmp_path / "db.sqlite3"); j = unknown(store); store.begin_verification(j["id"])
    store.recover_interrupted()
    assert store.job(j["id"])["status"] == "unknown"
    with pytest.raises(RuleError): store.reserve(own_video(), COMMENT, "유튜브1", "live", 10)


def test_two_recheck_clicks_are_atomic(tmp_path):
    store = Store(tmp_path / "db.sqlite3"); j = unknown(store)
    barrier = threading.Barrier(4)
    def start(_):
        s = Store(store.path); barrier.wait()
        try: return s.begin_verification(j["id"])
        except RuleError: return None
    with ThreadPoolExecutor(max_workers=4) as pool: result = list(pool.map(start, range(4)))
    assert sum(r is not None for r in result) == 1
    assert len(store.history()) == 1


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    monkeypatch.setattr(YouTubeService, "resolve_video", lambda self, vid: own_video())
    monkeypatch.setattr(YouTubeService, "channel_id_for_handle", lambda self, handle: CID)
    fixture = FixtureService()
    monkeypatch.setattr(YouTubeService, "_get", lambda self, r, p: fixture._get(r, p))
    async def forbidden(*a, **k): pytest.fail("Read-only recovery called browser/publishing")
    for name in ("publish", "rehearse", "_engine", "_live_context", "_identity"):
        monkeypatch.setattr(app.state.manager, name, forbidden)
    with TestClient(app, base_url="http://127.0.0.1:8765") as connection:
        connection.headers["X-CSRF-Token"] = connection.get("/api/bootstrap").json()["session_token"]
        app.state.settings.update({"youtube_api_key": "fixture_key", "test_video_url": f"https://www.youtube.com/watch?v={VID}",
                                   "test_channel_id": CID, "expected_handle": "@own"})
        yield connection


def wait_terminal(client, job_id):
    for _ in range(200):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("unknown", "success", "failed"): return j
        time.sleep(.01)
    pytest.fail("Read-only verification did not finish")


def test_full_api_recovery_has_no_browser_and_no_new_job(client):
    j = unknown(client.app.state.store)
    response = client.post(f"/api/jobs/{j['id']}/verify-existing", json={"confirmed": True})
    assert response.status_code == 200 and response.json()["read_only"] is True
    assert response.json()["job_id"] == j["id"]
    result = wait_terminal(client, j["id"])
    assert result["status"] == "success" and result["verification"]["method"] == "youtube_api_double_read"
    assert len(client.get("/api/history").json()["items"]) == 1
    assert client.post(f"/api/jobs/{j['id']}/verify-existing", json={"confirmed": True}).status_code == 400


def test_get_status_is_still_read_only_and_does_not_start_recovery(client):
    j = unknown(client.app.state.store)
    before = client.get(f"/api/jobs/{j['id']}").json()
    after = client.get(f"/api/jobs/{j['id']}").json()
    assert before == after and after["status"] == "unknown"


@pytest.mark.parametrize("body", [{}, {"confirmed": False}, {"confirmed": True, "status": "success"}, {"confirmed": True, "comment_url": "https://evil.example"}])
def test_recovery_requires_consent_and_rejects_injected_success(client, body):
    j = unknown(client.app.state.store)
    assert client.post(f"/api/jobs/{j['id']}/verify-existing", json=body).status_code in (400, 422)
    assert client.app.state.store.job(j["id"])["status"] == "unknown"


def test_recovery_has_csrf_protection(client):
    j = unknown(client.app.state.store)
    assert client.post(f"/api/jobs/{j['id']}/verify-existing", json={"confirmed": True}, headers={"X-CSRF-Token": "wrong"}).status_code == 403


def test_wrong_original_test_settings_fail_before_any_recovery(client):
    j = unknown(client.app.state.store)
    client.app.state.settings.update({"test_channel_id": "UC" + "b" * 22})
    response = client.post(f"/api/jobs/{j['id']}/verify-existing", json={"confirmed": True})
    assert response.status_code == 400 and client.app.state.store.job(j["id"])["status"] == "unknown"


def test_wrong_handle_mapping_cannot_reconcile_another_author(client, monkeypatch):
    j = unknown(client.app.state.store)
    monkeypatch.setattr(YouTubeService, "channel_id_for_handle", lambda self, h: "UC" + "b" * 22)
    assert client.post(f"/api/jobs/{j['id']}/verify-existing", json={"confirmed": True}).status_code == 400


def test_api_failure_preserves_unknown_and_does_not_increment_success(client, monkeypatch):
    j = unknown(client.app.state.store)
    def deny(*a, **k): raise ServiceError("Synthetic API limit", "youtube_quota")
    monkeypatch.setattr(YouTubeService, "_get", deny)
    assert client.post(f"/api/jobs/{j['id']}/verify-existing", json={"confirmed": True}).status_code == 200
    result = wait_terminal(client, j["id"])
    assert result["status"] == "unknown" and result["post_started"] == 1
    assert client.app.state.store.stats(10)["total_success"] == 0
    with pytest.raises(RuleError): client.app.state.store.reserve(own_video(), COMMENT, "유튜브1", "live", 10)


def test_version_and_history_button_survive_reload(client):
    assert client.get("/api/bootstrap").json()["version"] == "0.1.4"
    assert 'id="job-verify-existing"' in client.get("/").text
    js = client.get("/static/app.js").text
    assert '"기존 댓글 재확인"' in js and '/verify-existing' in js
    assert '"등록 확인 · API"' in js


@pytest.mark.parametrize("href,channel,expected", [
    ("/@own", "", True), ("https://www.youtube.com/@own", "", True),
    ("/channel/" + CID, CID, True), ("/channel/" + CID, "", False),
    ("/channel/" + CID, "UC" + "b" * 22, False),
    ("https://evil.example/@own", CID, False), ("/nested/@own", CID, False),
    ("javascript:alert(1)", CID, False), ("/@other", CID, False),
    ("https://youtube.com:443/@own", CID, False), ("http://youtube.com/@own", CID, False),
])
def test_dom_author_links_do_not_rely_on_display_names(href, channel, expected):
    assert BrowserManager._author_matches(href, "@own", channel) is expected
