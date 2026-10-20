"""Public YouTube metadata, optional captions, and explicitly synthetic fixtures.

Live metadata requires the user's YouTube Data API key. Caption retrieval is a
best-effort, unofficial read through youtube-transcript-api; unavailable captions
are never replaced by invented transcript text. The analysis path does not persist public comment author identities. The separate
read-only verification path retains evidence only for the matching own comment.
"""

from __future__ import annotations

import copy
import html
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx


CHANNELS = [
    {"id": "UCxlc0Ky0aWoD8uI09ad44Tg", "name": "외롭여"},
    {"id": "UC-ee_ziASM251TMXe3P3VZA", "name": "리즈시절 LizTV"},
    {"id": "UC09L3JxGVcn7SKLa-pjTEmw", "name": "러브커넥트 유지연"},
    {"id": "UCVdtDW-HF4KluixGaBAcI4w", "name": "연애데이터 노노"},
    {"id": "UCkGl2jieOVhGiHPdXF77Yyw", "name": "심리조각"},
]
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}\Z")
MAX_TRANSCRIPT_CHARS = 24_000
COMMENTS_SCOPE = (
    "YouTube API 관련성순 최상위 댓글 최대 20개를 받은 뒤, "
    "그 범위 안에서 좋아요순으로 정렬했습니다. 전체 댓글의 인기 순위는 아닙니다."
)
_API_BASE = "https://www.googleapis.com/youtube/v3/"
_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"
}


class ServiceError(Exception):
    """An error whose message may safely be shown in the UI (never a raw URL)."""

    def __init__(self, message: str, code: str = "service_error") -> None:
        super().__init__(message)
        self.code = code


def parse_video_id(value: str) -> str:
    """Parse only an 11-character ID or an explicitly approved YouTube URL.

    This does not verify a channel or grant posting permission. The server must
    resolve metadata and apply its channel/test-video policy after this step.
    Userinfo, explicit ports, unapproved hosts and redirect URLs are rejected.
    """
    if not isinstance(value, str):
        raise ServiceError("YouTube 영상 링크 또는 11자리 영상 ID를 입력해 주세요.", "invalid_video_url")
    value = value.strip()
    if VIDEO_ID_RE.fullmatch(value):
        return value
    if not value or len(value) > 2048 or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise ServiceError("올바른 YouTube 영상 링크를 입력해 주세요.", "invalid_video_url")
    if "://" not in value and any(value.lower().startswith(host + "/") for host in _YOUTUBE_HOSTS):
        value = "https://" + value
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or parts.hostname not in _YOUTUBE_HOSTS
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
            or ":" in parts.netloc
        ):
            raise ValueError("unapproved origin")
        video_id = ""
        if parts.hostname == "youtu.be":
            match = re.fullmatch(r"/([A-Za-z0-9_-]{11})/?", parts.path)
            video_id = match.group(1) if match else ""
        elif parts.path in {"/watch", "/watch/"}:
            values = parse_qs(parts.query, keep_blank_values=True).get("v", [])
            video_id = values[0] if len(values) == 1 else ""
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?", parts.path)
            video_id = match.group(1) if match else ""
        if not VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("missing video id")
        return video_id
    except (ValueError, UnicodeError):
        raise ServiceError(
            "youtube.com 또는 youtu.be의 영상 링크만 지원합니다. 계정 정보·포트가 포함된 링크는 허용하지 않습니다.",
            "invalid_video_url",
        ) from None


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _published_at(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return ""
        return stamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return ""


def _thumbnail(snippet: dict[str, Any]) -> str:
    thumbnails = snippet.get("thumbnails", {})
    if not isinstance(thumbnails, dict):
        return ""
    for size in ("maxres", "standard", "high", "medium", "default"):
        item = thumbnails.get(size, {})
        url = item.get("url", "") if isinstance(item, dict) else ""
        if not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme == "https" and parsed.netloc == "i.ytimg.com"
                and parsed.hostname == "i.ytimg.com" and parsed.port is None
                and not parsed.username and not parsed.password
            ):
                return url
        except ValueError:
            pass
    return ""


class YouTubeService:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""

    def _require_key(self) -> None:
        if not self._api_key:
            raise ServiceError(
                "실제 영상 조회에는 YouTube Data API 키가 필요합니다. 설정에서 키를 등록하거나 가상 연습 모드를 사용하세요.",
                "youtube_key_missing",
            )

    def _get(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        self._require_key()
        if resource not in {"channels", "playlistItems", "videos", "commentThreads", "comments"}:
            raise ServiceError("지원하지 않는 YouTube 조회입니다.", "unsupported_resource")
        try:
            response = httpx.get(
                _API_BASE + resource,
                params={**params, "key": self._api_key},
                timeout=httpx.Timeout(25.0, connect=5.0),
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise ServiceError("YouTube 조회가 시간 내에 응답하지 않았습니다. 잠시 후 다시 시도해 주세요.", "youtube_timeout") from None
        except (httpx.HTTPError, ValueError):
            # HTTP exception strings can include the API key in the query URL.
            raise ServiceError("YouTube에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.", "youtube_network") from None
        try:
            body = response.json()
        except ValueError:
            raise ServiceError("YouTube가 읽을 수 없는 응답을 반환했습니다.", "youtube_response_invalid") from None
        if not isinstance(body, dict):
            raise ServiceError("YouTube 응답 형식을 확인할 수 없습니다.", "youtube_response_invalid")
        if response.status_code != 200 or body.get("error"):
            error = body.get("error", {})
            details = error.get("errors", []) if isinstance(error, dict) else []
            reasons = {
                item.get("reason") for item in details
                if isinstance(item, dict) and isinstance(item.get("reason"), str)
            } if isinstance(details, list) else set()
            if "commentsDisabled" in reasons:
                raise ServiceError("이 영상은 댓글 조회가 비활성화되어 있습니다.", "comments_disabled")
            if reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"} or response.status_code == 429:
                raise ServiceError("YouTube API 할당량 또는 호출 한도에 도달했습니다.", "youtube_quota")
            if response.status_code in {400, 401, 403}:
                raise ServiceError(
                    "YouTube API 요청이 거절되었습니다. 키의 YouTube Data API v3 사용 설정·제한·할당량을 확인해 주세요.",
                    "youtube_authorization",
                )
            if response.status_code == 404:
                raise ServiceError("YouTube에서 요청한 공개 자료를 찾을 수 없습니다.", "youtube_not_found")
            raise ServiceError("YouTube 조회에 실패했습니다. 잠시 후 다시 시도해 주세요.", "youtube_api_error")
        if "items" in body and not isinstance(body["items"], list):
            raise ServiceError("YouTube 목록 응답 형식을 확인할 수 없습니다.", "youtube_response_invalid")
        return body

    @staticmethod
    def _video(item: dict[str, Any]) -> dict[str, Any]:
        snippet = item.get("snippet", {})
        if not isinstance(snippet, dict):
            raise ServiceError("영상 메타데이터가 올바르지 않습니다.", "youtube_response_invalid")
        video_id = parse_video_id(_text(item.get("id"), 100))
        channel_id = _text(snippet.get("channelId"), 100)
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
            raise ServiceError("영상의 채널을 확인할 수 없습니다.", "youtube_channel_unknown")
        title = _text(snippet.get("title"), 500)
        if not title:
            raise ServiceError("영상 제목을 확인할 수 없습니다.", "youtube_response_invalid")
        return {
            "id": video_id,
            "title": title,
            "description": _text(snippet.get("description"), 8000),
            "channel_id": channel_id,
            "channel_title": _text(snippet.get("channelTitle"), 300),
            # A playlist item's publishedAt is its insertion date. Only the
            # actual videos.list snippet.publishedAt is used for video dates.
            "published_at": _published_at(snippet.get("publishedAt")),
            "thumbnail": _thumbnail(snippet),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "source": "youtube",
            "is_test": False,
        }

    def resolve_video(self, video_id: str) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        body = self._get("videos", {"part": "snippet", "id": video_id})
        items = body.get("items", [])
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("id") == video_id:
                return self._video(item)
        raise ServiceError("공개 영상 정보를 찾지 못했습니다. 링크·공개 상태를 확인해 주세요.", "video_not_found")

    def channel_id_for_handle(self, handle: str) -> str:
        """Resolve a handle to its actual channel ID; this alone proves no login.

        Posting must additionally compare the returned ID with the test video
        and verify the active browser account/channel before typing or posting.
        """
        clean = handle.strip() if isinstance(handle, str) else ""
        clean = clean.removeprefix("@")
        if (
            not 1 <= len(clean) <= 100 or any(char.isspace() or ord(char) < 33 for char in clean)
            or any(char in clean for char in "/\\@?#:&")
        ):
            raise ServiceError("채널 URL 대신 @로 시작하는 본인 YouTube 핸들을 입력해 주세요.", "invalid_handle")
        result = self._get("channels", {"part": "id", "forHandle": "@" + clean})
        items = result.get("items", [])
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
            channel_id = items[0].get("id")
            if isinstance(channel_id, str) and re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
                return channel_id
        raise ServiceError("설정한 YouTube 핸들에 해당하는 공개 채널을 찾지 못했습니다.", "handle_not_found")

    def latest_videos(self, per_channel: int = 3) -> dict[str, Any]:
        if isinstance(per_channel, bool) or not isinstance(per_channel, int) or not 1 <= per_channel <= 10:
            raise ServiceError("채널별 조회 수는 1~10개로 지정해 주세요.", "invalid_count")
        body = self._get("channels", {
            "part": "contentDetails", "id": ",".join(channel["id"] for channel in CHANNELS), "maxResults": 50,
        })
        channels = {item.get("id"): item for item in body.get("items", []) if isinstance(item, dict)}
        warnings: list[str] = []
        ids_by_channel: dict[str, list[str]] = {}

        def load_playlist(channel: dict[str, str]) -> list[str]:
            entry = channels.get(channel["id"], {})
            details = entry.get("contentDetails")
            playlists = details.get("relatedPlaylists") if isinstance(details, dict) else None
            playlist_id = playlists.get("uploads") if isinstance(playlists, dict) else None
            if not isinstance(playlist_id, str) or not playlist_id:
                raise ServiceError("공개 업로드 목록을 확인하지 못했습니다.", "playlist_missing")
            result = self._get("playlistItems", {
                "part": "contentDetails", "playlistId": playlist_id, "maxResults": min(50, per_channel * 2),
            })
            return [
                item["contentDetails"]["videoId"] for item in result.get("items", [])
                if isinstance(item, dict) and isinstance(item.get("contentDetails"), dict)
                and VIDEO_ID_RE.fullmatch(str(item["contentDetails"].get("videoId", "")))
            ]

        # Independent channels may fail separately; success for the others is
        # retained and every failed channel gets a visible warning.
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(load_playlist, channel): channel for channel in CHANNELS}
            for future in as_completed(futures):
                channel = futures[future]
                try:
                    ids_by_channel[channel["id"]] = future.result()
                except ServiceError as exc:
                    warnings.append(f"{channel['name']}: {exc}")

        all_ids = list(dict.fromkeys(video_id for ids in ids_by_channel.values() for video_id in ids))
        videos_by_id: dict[str, dict[str, Any]] = {}
        for start in range(0, len(all_ids), 50):
            batch = all_ids[start:start + 50]
            try:
                result = self._get("videos", {"part": "snippet", "id": ",".join(batch), "maxResults": 50})
                for item in result.get("items", []):
                    if not isinstance(item, dict):
                        continue
                    try:
                        video = self._video(item)
                        videos_by_id[video["id"]] = video
                    except ServiceError:
                        warnings.append("일부 영상의 채널·제목 정보를 확인하지 못해 목록에서 제외했습니다.")
            except ServiceError as exc:
                warnings.append(f"영상 상세정보 일부 조회 실패: {exc}")

        videos: list[dict[str, Any]] = []
        for channel in CHANNELS:
            if channel["id"] not in ids_by_channel:
                continue
            matching = [
                videos_by_id[video_id] for video_id in ids_by_channel[channel["id"]]
                if video_id in videos_by_id and videos_by_id[video_id]["channel_id"] == channel["id"]
            ]
            matching.sort(key=lambda video: video["published_at"], reverse=True)
            videos.extend(matching[:per_channel])
            if not matching:
                warnings.append(f"{channel['name']}: 현재 확인 가능한 공개 영상이 없습니다.")
            elif any(not video["published_at"] for video in matching):
                warnings.append(f"{channel['name']}: 일부 영상의 게시 날짜를 확인할 수 없습니다.")
        videos.sort(key=lambda video: video["published_at"], reverse=True)
        return {"videos": videos, "warnings": list(dict.fromkeys(warnings))}

    def _comments(self, video_id: str) -> list[dict[str, Any]]:
        result = self._get("commentThreads", {
            "part": "snippet", "videoId": video_id, "order": "relevance", "maxResults": 20, "textFormat": "plainText",
        })
        comments: list[dict[str, Any]] = []
        for item in result.get("items", [])[:20]:
            try:
                snippet = item["snippet"]["topLevelComment"]["snippet"]
                text = _text(snippet.get("textOriginal") or snippet.get("textDisplay"), 1200).strip()
                # Author metadata is discarded; obvious @mentions in the text
                # are also removed from the context sent to a model.
                text = re.sub(r"@[\w.\-]+", "[호칭 생략]", html.unescape(text))
                count = _nonnegative_number(snippet.get("likeCount")) or 0
                if text:
                    comments.append({"text": text, "like_count": int(count)})
            except (KeyError, TypeError, AttributeError):
                continue
        return sorted(comments, key=lambda comment: comment["like_count"], reverse=True)

    @staticmethod
    def _captions(video_id: str) -> dict[str, Any]:
        fallback = {"transcript": "", "transcript_segments": [], "transcript_source": "none"}
        try:
            from requests import Session
            from requests.exceptions import Timeout
            from urllib3.util import Timeout as NetworkTimeout
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            return {**fallback, "warnings": ["선택 자막 모듈이 없어 제목·설명만 사용합니다. 필요하면 자막을 직접 입력하세요."]}

        class DeadlineSession(Session):
            def __init__(self) -> None:
                super().__init__()
                self.trust_env = False  # No implicit proxies or credential reuse.
                self.deadline = time.monotonic() + 15.0

            def request(self, method: str, url: str, **kwargs: Any) -> Any:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise Timeout("caption deadline")
                kwargs["timeout"] = NetworkTimeout(total=remaining, connect=min(3.0, remaining), read=min(8.0, remaining))
                return super().request(method, url, **kwargs)

        try:
            with DeadlineSession() as session:
                fetched = YouTubeTranscriptApi(http_client=session).fetch(video_id, languages=["ko", "en"])
                segments: list[dict[str, Any]] = []
                length = 0
                truncated = False
                for raw in fetched.to_raw_data():
                    remaining = MAX_TRANSCRIPT_CHARS - length - (1 if segments else 0)
                    if remaining <= 0:
                        truncated = True
                        break
                    raw_text = _text(raw.get("text"), MAX_TRANSCRIPT_CHARS + 1).strip()
                    if not raw_text:
                        continue
                    text = raw_text[:remaining]
                    truncated |= len(raw_text) > remaining
                    segments.append({
                        "text": text, "start": _nonnegative_number(raw.get("start")),
                        "duration": _nonnegative_number(raw.get("duration")),
                    })
                    length += len(text) + (1 if len(segments) > 1 else 0)
                if not segments:
                    return {**fallback, "warnings": ["읽을 수 있는 자막이 없어 제목·설명만 사용합니다."]}
                warnings = ["자막은 보조 도구로 읽은 텍스트입니다. 실제 장면이나 발언의 정확성을 보증하지 않습니다."]
                if getattr(fetched, "is_generated", False):
                    warnings.append("자동 생성 자막이므로 인식 오류가 있을 수 있습니다.")
                if truncated:
                    warnings.append("자막은 처음 24,000자까지만 사용했습니다.")
                return {
                    "transcript": "\n".join(segment["text"] for segment in segments),
                    "transcript_segments": segments,
                    "transcript_source": "youtube_captions",
                    "transcript_language": _text(getattr(fetched, "language_code", ""), 30),
                    "warnings": warnings,
                }
        except Exception as exc:
            # Never return third-party exception text; it can contain URLs and
            # diagnostic request details. No proxy, cookies, or CAPTCHA bypass.
            if type(exc).__name__ in {"RequestBlocked", "IpBlocked", "YouTubeRequestFailed"}:
                warning = "YouTube가 자막 요청을 제한했거나 거절했습니다. 제목·설명만 사용합니다."
            elif type(exc).__name__ in {"TranscriptsDisabled", "NoTranscriptFound"}:
                warning = "조회 가능한 한국어·영어 자막이 없어 제목·설명만 사용합니다."
            else:
                warning = "자막을 가져오지 못했습니다. 제목·설명만 사용하며, 필요한 자막은 직접 입력할 수 있습니다."
            return {**fallback, "warnings": [warning]}

    def context(self, video: dict[str, Any], manual_transcript: str = "") -> dict[str, Any]:
        self._require_key()
        video_id = parse_video_id(video.get("id", ""))
        if not isinstance(manual_transcript, str):
            raise ServiceError("직접 입력하는 자막은 텍스트여야 합니다.", "invalid_transcript")
        warnings: list[str] = []
        manual = manual_transcript.strip()
        with ThreadPoolExecutor(max_workers=2) as pool:
            comment_future = pool.submit(self._comments, video_id)
            caption_future = None if manual else pool.submit(self._captions, video_id)
            if manual:
                if len(manual) > MAX_TRANSCRIPT_CHARS:
                    warnings.append("직접 입력한 자막은 처음 24,000자까지만 사용했습니다.")
                manual = manual[:MAX_TRANSCRIPT_CHARS]
                captions = {
                    "transcript": manual,
                    "transcript_segments": [{"text": manual, "start": None, "duration": None}],
                    "transcript_source": "manual",
                    "warnings": ["사용자가 제공한 자막입니다. 원영상과의 일치 여부·시점은 확인하지 않았습니다."],
                }
            else:
                captions = caption_future.result()  # type: ignore[union-attr]
            try:
                comments = comment_future.result()
                comments_source = "youtube_data_api"
                if not comments:
                    warnings.append("조회 가능한 공개 댓글이 없습니다. 댓글 반응은 분석에 사용하지 않았습니다.")
            except ServiceError as exc:
                comments = []
                comments_source = "none"
                warnings.append(f"댓글을 읽지 못했습니다: {exc} 댓글 반응은 분석에 사용하지 않았습니다.")
        warnings.extend(captions.pop("warnings", []))
        return {
            "video": copy.deepcopy(video), **captions,
            "comments": comments, "comments_source": comments_source,
            "comments_scope": COMMENTS_SCOPE, "warnings": list(dict.fromkeys(warnings)),
        }


_SAMPLES = [
    {
        "title": "[가상 연습] 답장이 늦을 때 마음을 전하는 법",
        "description": "실제 유튜브 영상이 아닌 과제 연습용 창작 자료입니다. 연락 속도와 관계의 태도를 구분하는 가상 사례입니다.",
        "segments": [
            {"text": "답장이 늦으면 상대의 마음이 식었다고 단정하기 쉬워요.", "start": 0.0, "duration": 8.0},
            {"text": "답장 속도보다 약속을 지키는 태도를 살펴보세요.", "start": 12.0, "duration": 9.0},
            {"text": "불안한 마음은 추궁보다 나의 감정으로 말하는 연습이 필요해요.", "start": 28.0, "duration": 10.0},
        ],
        "audience": "[가상 댓글] 답장 시간만 보고 마음을 단정하지 말자는 질문이 남네요.",
        "draft": {
            "worry": "답장이 늦을 때 상대의 마음을 어떻게 판단하고 불안을 전할지 고민합니다.",
            "evidence_quote": "답장 속도보다 약속을 지키는 태도",
            "evidence_source": "transcript", "evidence_timestamp": 12.0,
            "comment": "[과제 시연] ‘답장 속도보다 약속을 지키는 태도’를 고민의 단서로 골랐어요. 운명원 연애 사주 콘텐츠라면 나의 관계 패턴을 어떤 질문으로 풀어볼까요?",
        },
    },
    {
        "title": "[가상 연습] 헤어진 뒤 연락을 고민하는 순간",
        "description": "실제 유튜브 영상이 아닌 과제 연습용 창작 자료입니다. 재회를 결정하기 전 확인할 질문을 다룹니다.",
        "segments": [
            {"text": "그리움만으로 다시 연락할지 결정하기는 어려워요.", "start": 0.0, "duration": 8.0},
            {"text": "다시 연락하기 전에 헤어진 이유가 달라졌는지 돌아보세요.", "start": 16.0, "duration": 9.0},
            {"text": "같은 갈등을 반복하지 않을 대화가 가능한지 먼저 생각해 보세요.", "start": 32.0, "duration": 10.0},
        ],
        "audience": "[가상 댓글] 그리움과 다시 만날 준비가 된 상태를 구분해 보고 싶어요.",
        "draft": {
            "worry": "전 연인에게 다시 연락하기 전에 이별의 원인과 반복될 갈등을 점검할지 고민합니다.",
            "evidence_quote": "헤어진 이유가 달라졌는지",
            "evidence_source": "transcript", "evidence_timestamp": 16.0,
            "comment": "[과제 시연] ‘헤어진 이유가 달라졌는지’부터 묻는 흐름을 골랐어요. 운명원 연애 사주도 재회를 단정하기보다 고민을 정리하는 질문으로 풀어보면 어떨까요?",
        },
    },
    {
        "title": "[가상 연습] 다툰 뒤 대화를 다시 여는 방법",
        "description": "실제 유튜브 영상이 아닌 과제 연습용 창작 자료입니다. 다툰 뒤 감정을 확인하고 대화를 시작하는 가상 사례입니다.",
        "segments": [
            {"text": "다툰 뒤에는 누가 옳은지부터 가리려는 마음이 앞설 수 있어요.", "start": 0.0, "duration": 8.0},
            {"text": "상대를 설득하기 전에 감정을 말로 확인하는 시간이 필요해요.", "start": 14.0, "duration": 10.0},
            {"text": "나는 이런 상황에서 서운했다고 말하며 대화를 시작해 보세요.", "start": 30.0, "duration": 9.0},
        ],
        "audience": "[가상 댓글] 누가 맞는지보다 어떤 감정이었는지 먼저 묻는 연습이 필요하겠어요.",
        "draft": {
            "worry": "다툰 뒤 서로의 감정을 확인하고 비난 없이 대화를 다시 시작할 방법을 고민합니다.",
            "evidence_quote": "감정을 말로 확인하는 시간",
            "evidence_source": "transcript", "evidence_timestamp": 14.0,
            "comment": "[과제 시연] ‘감정을 말로 확인하는 시간’을 댓글의 중심에 놓았어요. 운명원 연애 사주라는 소재도 서로의 대화 방식을 돌아보는 질문으로 이어질까요?",
        },
    },
]


def sample_videos() -> list[dict[str, Any]]:
    """Three invented local practice records, never represented as live videos."""
    return [{
        "id": f"sample_{index}", "title": sample["title"], "description": sample["description"],
        "channel_id": "sample_channel", "channel_title": "가상 연습 채널 · 실제 영상 아님",
        "published_at": f"2026-01-0{index}T09:00:00Z", "thumbnail": "", "url": "",
        "source": "sample", "is_test": False,
    } for index, sample in enumerate(_SAMPLES, start=1)]


def sample_context(video_id: str) -> dict[str, Any]:
    if video_id not in {"sample_1", "sample_2", "sample_3"}:
        raise ServiceError("가상 연습 영상을 찾을 수 없습니다.", "sample_not_found")
    index = int(video_id[-1]) - 1
    sample = copy.deepcopy(_SAMPLES[index])
    return {
        "video": sample_videos()[index],
        "transcript": "\n".join(segment["text"] for segment in sample["segments"]),
        "transcript_segments": sample["segments"], "transcript_source": "sample",
        "comments": [{"text": sample["audience"], "like_count": 0}],
        "comments_source": "sample", "comments_scope": "직접 작성한 가상 댓글 1개입니다. 실제 반응·인기도 자료가 아닙니다.",
        "sample_draft": sample["draft"],
        "warnings": ["영상·자막·댓글·날짜는 가상 연습 데이터입니다. 실제 YouTube 조회 결과가 아닙니다."],
    }
