"""Read-only verification of one already-submitted comment on the owner's video.

Only documented YouTube Data API GET requests are used. This module cannot
insert, update, delete, like, or moderate a comment. Unrelated comments are
examined in memory only; no raw API payloads or credentials are persisted.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any

from .youtube import ServiceError, YouTubeService

COMMENT_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


def normalized_text(value: str) -> str:
    # Ignore layout whitespace, NOT punctuation, missing words or substrings.
    return re.sub(r"\s+", " ", value).strip()


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _items(body: Any) -> list:
    body = _object(body)
    result = body.get("items")
    if not isinstance(result, list):
        raise ServiceError("댓글 조회 응답의 목록 형식을 확인하지 못했습니다.", "comment_response_invalid")
    return result


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _matches_comment(item: dict, job: dict) -> bool:
    """Identity is an immutable channel ID, never a display name."""
    snippet = _object(item.get("snippet"))
    author = _object(snippet.get("authorChannelId"))
    text = snippet.get("textOriginal", snippet.get("textDisplay"))
    if not isinstance(text, str) or not text.strip():
        return False
    if snippet.get("parentId"):
        return False  # A reply cannot prove a top-level comment.
    if snippet.get("videoId") not in (None, job["video_id"]):
        return False
    if snippet.get("channelId") not in (None, job["channel_id"]):
        return False
    if snippet.get("moderationStatus") not in (None, "published"):
        return False
    if author.get("value") != job["channel_id"]:
        return False
    return normalized_text(text) == normalized_text(job["comment"])


def verify_existing_comment(job: dict, service: YouTubeService) -> dict:
    """Find the same top-level comment, then GET its ID independently.

    The thread establishes its video. comments.list can omit videoId, so the
    second lookup is bound to that first lookup by the same unique comment ID.
    Both reads must independently match author channel ID and the entire text.
    Searches at most 300 newest threads. Incomplete or ambiguous results fail
    closed; no successful result is fabricated and no posting lock is released.
    """
    if (job.get("mode") != "live" or not job.get("post_started")
            or not VIDEO_ID.fullmatch(str(job.get("video_id", "")))
            or not CHANNEL_ID.fullmatch(str(job.get("channel_id", "")))
            or not isinstance(job.get("comment"), str) or not job["comment"].strip()):
        raise ServiceError("실제 게시를 시도한 본인 영상의 기록만 재확인할 수 있습니다.", "invalid_verification_job")
    matches: dict[str, dict] = {}
    page_token = None
    seen_tokens: set[str] = set()
    complete = False
    for _ in range(3):
        params = {"part": "snippet", "videoId": job["video_id"],
                  "textFormat": "plainText", "order": "time", "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        body = service._get("commentThreads", params)
        for thread in _items(body):
            snippet = _object(_object(thread).get("snippet"))
            # This is the authoritative video binding, not a URL supplied by the UI.
            if snippet.get("videoId") != job["video_id"]:
                continue
            if snippet.get("channelId") not in (None, job["channel_id"]):
                continue
            top = _object(snippet.get("topLevelComment"))
            cid = top.get("id")
            if isinstance(cid, str) and COMMENT_ID.fullmatch(cid) and _matches_comment(top, job):
                if _stamp(_object(top.get("snippet")).get("publishedAt")) is None:
                    continue
                matches[cid] = top
        token = _object(body).get("nextPageToken")
        if not token:
            complete = True
            break
        if not isinstance(token, str) or len(token) > 2000 or token in seen_tokens:
            raise ServiceError("댓글 목록의 다음 페이지를 안전하게 확인하지 못했습니다.", "comment_pagination_invalid")
        seen_tokens.add(token)
        page_token = token
    if not complete:
        raise ServiceError("최신 댓글 300개까지만 조회했습니다. 전체 확인을 완료하지 못해 결과 미확인과 재게시 잠금을 유지합니다.", "comment_scan_incomplete")
    if not matches:
        raise ServiceError("YouTube API에서 같은 영상·작성자 채널 ID·댓글 전문이 일치하는 댓글을 찾지 못했습니다. 반영 지연이나 공개 상태 때문일 수 있습니다. 재게시하지 마세요.", "comment_not_verified")
    if len(matches) != 1:
        raise ServiceError("같은 계정의 동일한 댓글이 여러 개여서 원래 게시 기록을 하나로 확정할 수 없습니다. 재게시 잠금을 유지합니다.", "comment_ambiguous")
    cid, first = next(iter(matches.items()))
    # A different endpoint and a second fresh HTTP GET: do not reuse list evidence.
    body = service._get("comments", {"part": "snippet", "id": cid, "textFormat": "plainText"})
    exact = [item for item in _items(body) if isinstance(item, dict) and item.get("id") == cid]
    if len(exact) != 1 or not _matches_comment(exact[0], job):
        raise ServiceError("댓글 ID로 다시 조회했으나 동일한 작성자와 전문을 확인하지 못했습니다. 결과 미확인과 재게시 잠금을 유지합니다.", "comment_reread_mismatch")
    first_stamp = _stamp(_object(first.get("snippet")).get("publishedAt"))
    second_stamp = _stamp(_object(exact[0].get("snippet")).get("publishedAt"))
    if first_stamp is None or second_stamp != first_stamp:
        raise ServiceError("댓글의 두 조회 결과에서 게시 시각이 일치하지 않습니다. 재게시 잠금을 유지합니다.", "comment_reread_mismatch")
    return {
        "method": "youtube_api_double_read",
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "video_id": job["video_id"], "comment_id": cid,
        "author_channel_id": job["channel_id"],
        "comment_sha256": hashlib.sha256(normalized_text(job["comment"]).encode("utf-8")).hexdigest(),
        "published_at": second_stamp.isoformat(timespec="seconds"),
        "comment_url": f"https://www.youtube.com/watch?v={job['video_id']}&lc={cid}",
        "checks": ["video_id", "author_channel_id", "whole_comment", "top_level", "same_comment_id_reread"],
    }


def proof_matches_job(proof: dict, job: dict) -> bool:
    """Validate trusted evidence again at the durable-state write boundary."""
    cid = proof.get("comment_id")
    return bool(
        proof.get("method") == "youtube_api_double_read"
        and job.get("mode") == "live" and job.get("post_started")
        and isinstance(cid, str) and COMMENT_ID.fullmatch(cid)
        and proof.get("video_id") == job.get("video_id")
        and proof.get("author_channel_id") == job.get("channel_id")
        and proof.get("comment_sha256") == hashlib.sha256(normalized_text(job["comment"]).encode("utf-8")).hexdigest()
        and proof.get("comment_url") == f"https://www.youtube.com/watch?v={job['video_id']}&lc={cid}"
        and _stamp(proof.get("verified_at")) is not None
        and _stamp(proof.get("published_at")) is not None
        and proof.get("checks") == ["video_id", "author_channel_id", "whole_comment", "top_level", "same_comment_id_reread"]
    )
