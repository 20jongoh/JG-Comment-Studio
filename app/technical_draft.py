"""Explicit, non-AI browser-test draft for the one configured own video.

This is separate from the marketing/AI path, never an automatic fallback.
The caller must verify the video and current posting scope on the server.
"""
from __future__ import annotations

from typing import Any

from .drafting import BRAND, _quote_allowed, _quote_spans, validate_comment
from .youtube import ServiceError


def technical_comment(quote: str) -> str:
    return (f"[과제 시연] ‘{quote}’ 영상에서 브라우저 댓글 입력·게시·확인을 점검하는 "
            "테스트 문구입니다. AI 분석이나 서비스 추천이 아닙니다.")


def validate_technical_comment(comment: str, quote: str, title: str) -> dict[str, Any]:
    """Only this fixed, visibly technical text can use the no-brand test policy.

    AI/marketing drafts continue to use the unchanged brand/evidence rules.
    A caller cannot switch the policy with a client-supplied flag.
    """
    base = validate_comment(comment, quote, require_disclosure=True)
    checks = [check for check in base["checks"] if check["name"] != "운명원 1회"]
    quote_ok = isinstance(quote, str) and _quote_allowed(quote) and isinstance(title, str) and quote in title
    checks += [
        {"name": "제목 원문 일치", "ok": bool(quote_ok), "detail": "YouTube API로 확인한 제목 일부를 그대로 사용합니다. 영상 내용 분석이 아닙니다."},
        {"name": "고정 기술 테스트 문구", "ok": bool(quote_ok and comment == technical_comment(quote)), "detail": "AI 분석·추천으로 오해되지 않도록 서버의 고정 문구만 허용합니다."},
        {"name": "브랜드 홍보 없음", "ok": isinstance(comment, str) and BRAND not in comment, "detail": "게시 기능 점검 전용이며 마케팅 초안의 대체물이 아닙니다."},
    ]
    return {"valid": all(item["ok"] for item in checks), "checks": checks, "char_count": base["char_count"]}


def make_technical_draft(video: dict[str, Any]) -> dict[str, Any]:
    if video.get("source") != "youtube" or video.get("is_test") is not True:
        raise ServiceError("AI 없는 게시 테스트는 설정에 등록한 본인 테스트 영상에서만 사용할 수 있습니다.", "test_draft_scope")
    title = video.get("title", "")
    if not isinstance(title, str):
        raise ServiceError("영상 제목을 확인하지 못했습니다.", "test_title_missing")
    spans = _quote_spans(title[:500])
    if not spans:
        raise ServiceError("테스트 문구에 넣을 4~28자의 제목 일부를 찾지 못했습니다. 본인 영상 제목을 확인하세요.", "test_title_missing")
    left, right = spans[0]
    quote = title[left:right]
    comment = technical_comment(quote)
    result = validate_technical_comment(comment, quote, title)
    if not result["valid"]:
        raise ServiceError("고정 테스트 문구의 길이 또는 제목 검사를 통과하지 못했습니다.", "test_draft_invalid")
    return {
        **result, "worry": "제목을 확인해 본인 영상의 게시 기능만 점검합니다. 내용·자막·연애 고민은 분석하지 않았습니다.",
        "evidence_quote": quote, "evidence_source": "title", "evidence_timestamp": None,
        "comment": comment, "source_mode": "technical_test", "ai_provider": None, "ai_model": None,
        "warnings": [
            "게시 테스트용 고정 문구 v0.1.3입니다. Gemini를 호출하지 않았으며 AI 분석 결과가 아닙니다.",
            "YouTube API로 영상 ID·채널·제목을 확인했습니다. 자막과 시청자 댓글은 읽거나 AI에 전송하지 않았습니다.",
            "이 경로는 브라우저 입력·게시·확인만 점검합니다. 과제의 연애 맥락 이해·브랜드 유도 항목을 검증하지 않습니다.",
            "초안 생성은 게시가 아닙니다. 별도의 확인 창에서 승인해야 본인 영상에 실제 댓글이 등록됩니다.",
        ],
    }
