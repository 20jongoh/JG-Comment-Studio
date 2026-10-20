"""Durable posting guards only. These tests never connect to YouTube or Chrome."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime as RealDatetime, timezone
import threading

import pytest

from app import storage
from app.storage import RuleError, Store


@pytest.fixture
def clock(monkeypatch):
    """Move one UTC instant; production code must derive its own KST date."""
    instant = [RealDatetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)]

    class FrozenDatetime(RealDatetime):
        @classmethod
        def now(cls, tz=None):
            value = instant[0]
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    def set_time(value):
        instant[0] = RealDatetime.fromisoformat(value)
        assert instant[0].tzinfo is not None

    monkeypatch.setattr(storage, "datetime", FrozenDatetime)
    return set_time


@pytest.fixture
def store(tmp_path, clock):
    return Store(tmp_path / "ledger.sqlite3")


def video(video_id="testvideo01"):
    return {"id": video_id, "channel_id": "UC" + "a" * 22, "title": "본인 테스트 영상"}


def reserve(store, video_id="testvideo01", *, profile="유튜브1", mode="live", limit=10):
    return store.reserve(video(video_id), "[과제 시연] 확인할 댓글입니다.", profile, mode, limit)


def complete_post(store, video_id, *, limit=10):
    job = reserve(store, video_id, limit=limit)
    store.mark_post_started(job["id"], limit)
    store.finish(job["id"], "success", "재조회 확인", comment_url="https://www.youtube.com/watch?v=" + video_id + "&lc=confirmed")
    return job


def test_video_lock_is_shared_across_profiles_and_survives_reopening(store):
    complete_post(store, "lockedvideo")
    reopened = Store(store.path)

    for profile in ("유튜브1", "다른 계정"):
        with pytest.raises(RuleError, match="영상당 한 번"):
            reserve(reopened, "lockedvideo", profile=profile)

    assert len(reopened.history()) == 1
    assert reopened.stats(10)["daily_used"] == 1


def test_quota_rolls_over_at_kst_midnight_not_utc_midnight(store, clock):
    clock("2026-10-19T14:59:59+00:00")  # 23:59:59 in Korea
    complete_post(store, "before_midnight", limit=1)
    with pytest.raises(RuleError, match="상한"):
        reserve(store, "quota_excess", limit=1)

    clock("2026-10-19T15:00:00+00:00")  # next Korean date, same UTC date
    assert store.stats(1)["daily_used"] == 0
    second = complete_post(store, "after_midnight", limit=1)

    assert store.job(second["id"])["day_key"] == "2026-10-20"
    assert store.job(second["id"])["created_at"].endswith("+09:00")
    assert store.stats(1) == {"daily_used": 1, "daily_limit": 1, "total_success": 2}


def test_failure_before_submit_releases_video_and_quota(store):
    first = reserve(store, limit=1)
    store.event(first["id"], "입력창 확인")
    store.finish(first["id"], "failed", "게시 전 중단", error="입력창 없음")

    assert store.job(first["id"])["status"] == "failed"
    assert store.job(first["id"])["post_started"] == 0
    assert store.stats(1)["daily_used"] == 0
    assert not store.has_active()

    retried = reserve(store, limit=1)
    assert retried["id"] != first["id"]
    assert store.job(first["id"])["status"] == "failed"


def test_failure_after_submit_remains_unknown_and_locked(store):
    first = reserve(store, limit=2)
    store.mark_post_started(first["id"], 2)
    store.finish(first["id"], "failed", "응답 확인 실패", error="timeout")

    reopened = Store(store.path)
    result = reopened.job(first["id"])
    assert result["status"] == "unknown"
    assert result["post_started"] == 1
    assert reopened.stats(2)["daily_used"] == 1
    assert not reopened.has_active()
    with pytest.raises(RuleError, match="영상당 한 번"):
        reserve(reopened, profile="다른 계정", limit=2)


@pytest.mark.parametrize(
    ("stage", "expected_status", "retry_allowed"),
    [("pending", "failed", True), ("typing", "failed", True), ("submitted", "unknown", False)],
)
def test_restart_distinguishes_before_and_after_external_submit(store, stage, expected_status, retry_allowed):
    job = reserve(store)
    if stage != "pending":
        store.event(job["id"], "입력 중")
    if stage == "submitted":
        store.mark_post_started(job["id"], 10)

    reopened = Store(store.path)
    reopened.recover_interrupted()
    recovered = reopened.job(job["id"])

    assert recovered["status"] == expected_status
    assert not reopened.has_active()
    assert recovered["error"]
    if retry_allowed:
        assert reopened.stats(10)["daily_used"] == 0
        assert reserve(reopened)["id"] != job["id"]
    else:
        assert reopened.stats(10)["daily_used"] == 1
        with pytest.raises(RuleError, match="영상당 한 번"):
            reserve(reopened)


def test_midnight_crossing_charges_the_day_of_external_submit(store, clock):
    clock("2026-10-19T14:59:59+00:00")
    job = reserve(store, limit=1)
    assert job["day_key"] == "2026-10-19"

    clock("2026-10-19T15:00:01+00:00")
    store.mark_post_started(job["id"], 1)
    submitted = store.job(job["id"])

    assert submitted["day_key"] == "2026-10-20"
    assert submitted["post_started"] == 1
    assert store.stats(1)["daily_used"] == 1


def test_midnight_submit_rechecks_new_day_ledger_before_marking_post_started(store, clock):
    # Build a persisted snapshot with a completed job on the target day, then an
    # old reservation. This isolates the final guard from the earlier reserve check.
    clock("2026-10-19T15:00:00+00:00")
    complete_post(store, "already_on_new_day", limit=1)
    clock("2026-10-19T14:59:59+00:00")
    waiting = reserve(store, "old_reservation", limit=1)

    clock("2026-10-19T15:00:01+00:00")
    with pytest.raises(RuleError, match="상한"):
        store.mark_post_started(waiting["id"], 1)

    result = store.job(waiting["id"])
    assert result["post_started"] == 0
    assert result["day_key"] == "2026-10-19"
    assert store.stats(1)["daily_used"] == 1


def test_reservations_are_atomic_across_connections_and_allow_only_one_active_job(store):
    count = 8
    clients = [Store(store.path) for _ in range(count)]
    barrier = threading.Barrier(count)

    def attempt(index):
        barrier.wait(timeout=10)
        try:
            return reserve(clients[index], "race_" + str(index))
        except RuleError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=count) as pool:
        outcomes = list(pool.map(attempt, range(count)))

    winners = [result for result in outcomes if isinstance(result, dict)]
    failures = [result for result in outcomes if isinstance(result, RuleError)]
    assert len(winners) == 1
    assert len(failures) == count - 1
    assert all("진행 중" in str(result) for result in failures)
    assert len(store.history()) == 1
    assert store.stats(10)["daily_used"] == 1
    assert store.has_active()


def test_rehearsal_never_uses_live_quota_but_active_rehearsal_blocks_parallel_job(store):
    complete_post(store, "completed_live", limit=1)
    sample = reserve(store, "sample_video", mode="sample", limit=1)

    assert store.stats(1) == {"daily_used": 1, "daily_limit": 1, "total_success": 1}
    with pytest.raises(RuleError, match="진행 중"):
        reserve(store, "second_sample", mode="sample", limit=1)

    store.finish(sample["id"], "rehearsed", "로컬 연습 완료")
    assert not store.has_active()
    assert store.stats(1)["daily_used"] == 1
    reserve(store, "sample_video", mode="sample", limit=1)


def test_submit_marker_is_one_way_and_cannot_be_called_twice(store):
    job = reserve(store)
    store.mark_post_started(job["id"], 10)
    with pytest.raises(RuleError, match="상태"):
        store.mark_post_started(job["id"], 10)
    assert store.job(job["id"])["post_started"] == 1


def test_late_callback_cannot_overwrite_a_confirmed_terminal_result(store):
    completed = complete_post(store, "confirmed_job")
    before = store.job(completed["id"])
    store.finish(completed["id"], "failed", "늦게 도착한 실패 콜백", error="stale")
    after = store.job(completed["id"])

    assert after["status"] == "success"
    assert after["comment_url"] == before["comment_url"]
    assert after["events"] == before["events"]
