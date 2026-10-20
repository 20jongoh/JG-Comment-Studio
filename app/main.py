from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import re
import secrets
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .browser import BrowserError, BrowserManager, find_chrome
from .config import DATA_DIR, PROFILE_NAME, ROOT, Settings
from .drafting import make_draft, validate_comment
from .verification import verify_existing_comment
from .technical_draft import make_technical_draft, validate_technical_comment
from .storage import RuleError, Store
from .youtube import CHANNELS, ServiceError, YouTubeService, parse_video_id, sample_context, sample_videos


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveRequest(StrictModel):
    url: str = Field(min_length=1, max_length=500)


class AnalyzeRequest(StrictModel):
    video_id: str = Field(min_length=1, max_length=50)
    mode: Literal["sample", "live"] = "sample"
    transcript: str = Field(default="", max_length=24000)
    draft_kind: Literal["ai", "technical_test"] = "ai"


class CommentRequest(StrictModel):
    analysis_id: str = Field(min_length=1, max_length=50)
    comment: str = Field(max_length=2000)


class PublishRequest(CommentRequest):
    confirmed: bool = False


class VerifyExistingRequest(StrictModel):
    confirmed: bool = False


class SettingsRequest(StrictModel):
    youtube_api_key: str | None = Field(default=None, max_length=1000)
    ai_api_key: str | None = Field(default=None, max_length=1000)
    ai_provider: Literal["openai", "gemini"] | None = None
    ai_model: str | None = Field(default=None, max_length=100)
    test_video_url: str | None = Field(default=None, max_length=500)
    test_channel_id: str | None = Field(default=None, max_length=50)
    expected_handle: str | None = Field(default=None, max_length=100)
    daily_limit: int | None = Field(default=None, ge=1, le=10, strict=True)


def create_app(directory=DATA_DIR, port: int = 8765, *, headless_practice=False) -> FastAPI:
    settings = Settings(directory)
    store = Store(directory / "ledger.sqlite3")
    session_token = secrets.token_urlsafe(32)
    base_url = f"http://127.0.0.1:{port}"
    manager = BrowserManager(directory, store, base_url, headless_practice=headless_practice)
    pending_tasks: set[asyncio.Task] = set()
    analyze_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        store.recover_interrupted()
        yield
        for task in list(pending_tasks):
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        with suppress(Exception):
            await manager.close()
        store.recover_interrupted()

    app = FastAPI(title="맥락 스튜디오", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.settings = settings
    app.state.manager = manager

    def schedule(coroutine):
        task = asyncio.create_task(coroutine)
        pending_tasks.add(task)
        task.add_done_callback(pending_tasks.discard)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "이 프로그램은 로컬 주소에서만 사용할 수 있습니다."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "외부 웹페이지의 요청을 차단했습니다."}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in {base_url, f"http://localhost:{port}"}:
            return JSONResponse({"detail": "외부 출처의 요청을 차단했습니다."}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session_token):
                return JSONResponse({"detail": "화면을 새로고침한 뒤 다시 시도하세요. 요청 확인 값이 일치하지 않습니다."}, status_code=403)
            try:
                content_length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "잘못된 요청입니다."}, status_code=400)
            if content_length > 150000 or len(await request.body()) > 150000:
                return JSONResponse({"detail": "입력 내용이 너무 큽니다."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' https://i.ytimg.com data:; connect-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ServiceError)
    @app.exception_handler(BrowserError)
    @app.exception_handler(RuleError)
    async def operational_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def test_id(values: dict) -> str | None:
        if not values["test_video_url"]:
            return None
        try:
            return parse_video_id(values["test_video_url"])
        except (ValueError, ServiceError):
            return None

    def flag_video(video: dict, values: dict) -> dict:
        result = dict(video)
        result["is_test"] = bool(video["id"] == test_id(values) and video["channel_id"] == values["test_channel_id"])
        return result

    def allowed_video(video: dict, values: dict) -> dict:
        result = flag_video(video, values)
        if result["channel_id"] not in {channel["id"] for channel in CHANNELS} and not result["is_test"]:
            raise RuleError("지정된 5개 채널 또는 설정에 등록한 본인 테스트 영상만 불러올 수 있습니다. 링크의 실제 채널을 확인했습니다.")
        return result

    def require_test(video: dict, values: dict):
        if video.get("source") != "youtube" or not flag_video(video, values)["is_test"]:
            raise RuleError("이 시연 버전은 설정에 등록한 본인 테스트 영상에만 게시합니다. 외부 채널은 조회와 초안까지 사용할 수 있습니다.")
        if not values["expected_handle"]:
            raise RuleError("설정에서 유튜브1로 게시할 @핸들을 입력하세요.")

    def lookup_analysis(analysis_id: str) -> dict:
        analysis = store.analysis(analysis_id)
        if not analysis:
            raise HTTPException(404, "초안을 찾지 못했습니다. 영상을 다시 선택하세요.")
        return analysis

    def checked_comment(analysis: dict, text: str) -> dict:
        if analysis.get("source_mode") == "technical_test":
            require_test(analysis["video"], settings.get())
            return validate_technical_comment(text, analysis.get("evidence_quote", ""), analysis["video"].get("title", ""))
        return validate_comment(text, evidence_quote=analysis.get("evidence_quote", ""), require_disclosure=True)

    def bootstrap_payload() -> dict:
        values = settings.get()
        return {
            "version": __version__, "session_token": session_token,
            "settings": settings.public(), "channels": CHANNELS,
            "stats": store.stats(values["daily_limit"]),
            "readiness": {"youtube": bool(values["youtube_api_key"]), "ai": bool(values["ai_api_key"]),
                          "chrome": bool(find_chrome()), "test_video": bool(test_id(values) and values["test_channel_id"] and values["expected_handle"])},
            "profile": {"name": PROFILE_NAME, "prepared": manager.prepared(), "login_verified": False},
        }

    @app.get("/api/bootstrap")
    async def bootstrap():
        return bootstrap_payload()

    @app.post("/api/settings")
    async def update_settings(body: SettingsRequest):
        if store.has_active():
            raise RuleError("현재 작업을 마친 뒤 설정을 변경하세요.")
        patch = body.model_dump(exclude_none=True)
        if patch.get("test_video_url"):
            video_id = parse_video_id(patch["test_video_url"])
            patch["test_video_url"] = f"https://www.youtube.com/watch?v={video_id}"
        try:
            settings.update(patch)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return bootstrap_payload()

    @app.get("/api/videos")
    async def videos(mode: Literal["sample", "live"] = "sample"):
        if mode == "sample":
            items = sample_videos()
            for video in items:
                store.put_video(video)
            return {"videos": items, "warnings": ["연습용으로 만든 가상 영상입니다. 실제 채널의 최신 영상이나 실제 게시 실적이 아닙니다."]}
        values = settings.get()
        service = YouTubeService(values["youtube_api_key"])
        result = await asyncio.to_thread(service.latest_videos, 3)
        result["videos"] = [allowed_video(v, values) for v in result["videos"]]
        own_id = test_id(values)
        if own_id and values["test_channel_id"]:
            try:
                own = allowed_video(await asyncio.to_thread(service.resolve_video, own_id), values)
                if not own["is_test"]:
                    result["warnings"].append("테스트 영상의 실제 채널 ID가 설정의 본인 채널 ID와 다릅니다.")
                else:
                    result["videos"] = [own] + [v for v in result["videos"] if v["id"] != own_id]
            except (ServiceError, RuleError) as exc:
                result["warnings"].append("본인 테스트 영상: " + str(exc))
        for video in result["videos"]:
            store.put_video(video)
        return result

    @app.post("/api/videos/resolve")
    async def resolve(body: ResolveRequest):
        values = settings.get()
        video_id = parse_video_id(body.url)
        video = await asyncio.to_thread(YouTubeService(values["youtube_api_key"]).resolve_video, video_id)
        video = allowed_video(video, values)
        store.put_video(video)
        return {"video": video}

    @app.post("/api/analyze")
    async def analyze(body: AnalyzeRequest):
        if analyze_lock.locked():
            raise RuleError("초안을 만드는 중입니다. 잠시 후 다시 시도하세요.")
        async with analyze_lock:
            values = settings.get()
            if body.draft_kind == "technical_test" and (body.mode != "live" or body.transcript):
                raise RuleError("게시 테스트는 실제 모드에서 자막 입력 없이 사용하세요. AI 분석과 별도인 고정 문구입니다.")
            if body.mode == "sample":
                if body.video_id not in {v["id"] for v in sample_videos()}:
                    raise RuleError("연습 영상 ID가 올바르지 않습니다.")
                context = sample_context(body.video_id)
            else:
                service = YouTubeService(values["youtube_api_key"])
                video_id = parse_video_id(body.video_id)
                video = allowed_video(await asyncio.to_thread(service.resolve_video, video_id), values)
                if body.draft_kind == "technical_test":
                    require_test(video, values)
                    context = {"video": video, "transcript_source": "none", "comments": [],
                               "comments_scope": "게시 기능 점검 모드: 자막·시청자 댓글을 읽지 않았습니다.", "warnings": []}
                else:
                    context = await asyncio.to_thread(service.context, video, body.transcript)
                    context["video"] = video
            if body.draft_kind == "technical_test":
                draft = make_technical_draft(context["video"])
            else:
                draft = await asyncio.to_thread(make_draft, context, values["ai_provider"], values["ai_api_key"], values["ai_model"])
            draft.update({"video": context["video"], "mode": body.mode,
                          "transcript_source": context.get("transcript_source", "none"),
                          "comments_count": len(context.get("comments", [])),
                          "comments_scope": context.get("comments_scope", "")})
            draft["warnings"] = list(dict.fromkeys(context.get("warnings", []) + draft.get("warnings", [])))
            return store.put_analysis(draft)

    @app.post("/api/validate")
    async def validate(body: CommentRequest):
        return checked_comment(lookup_analysis(body.analysis_id), body.comment)

    @app.post("/api/rehearse")
    async def rehearse(body: CommentRequest):
        analysis = lookup_analysis(body.analysis_id)
        if analysis.get("mode") != "sample":
            raise RuleError("로컬 연습은 연습 데이터에서 진행하세요.")
        result = checked_comment(analysis, body.comment)
        if not result["valid"]:
            raise RuleError("댓글 점검 항목을 모두 통과한 뒤 실행하세요.")
        job = store.reserve(analysis["video"], body.comment, PROFILE_NAME, "sample", settings.get()["daily_limit"])
        schedule(manager.rehearse(job))
        return {"job_id": job["id"]}

    @app.post("/api/publish")
    async def publish(body: PublishRequest):
        if body.confirmed is not True:
            raise RuleError("선택한 영상과 댓글을 확인하고 게시를 승인하세요.")
        analysis = lookup_analysis(body.analysis_id)
        values = settings.get()
        if analysis.get("mode") != "live":
            raise RuleError("연습 데이터는 YouTube에 게시할 수 없습니다.")
        require_test(analysis["video"], values)
        checks = checked_comment(analysis, body.comment)
        if not checks["valid"]:
            raise RuleError("댓글 길이, 원문 근거, 시연 표시와 금지 요소를 다시 확인하세요.")
        service = YouTubeService(values["youtube_api_key"])
        def verify():
            fresh = service.resolve_video(analysis["video"]["id"])
            current_settings = settings.get()
            require_test(fresh, current_settings)
            if analysis.get("source_mode") == "technical_test":
                current_checks = validate_technical_comment(body.comment, analysis.get("evidence_quote", ""), fresh.get("title", ""))
                if not current_checks["valid"]:
                    raise RuleError("게시 직전 테스트 문구와 영상 제목이 일치하지 않습니다. 다시 초안을 준비하세요.")
            handle_channel = service.channel_id_for_handle(current_settings["expected_handle"])
            if handle_channel != fresh["channel_id"]:
                raise RuleError("게시할 @핸들의 채널과 테스트 영상을 올린 채널이 다릅니다. 본인 영상을 올린 채널로 로그인하고 설정을 확인하세요.")
            return flag_video(fresh, current_settings)
        fresh_video = await asyncio.to_thread(verify)
        if settings.get() != values:
            raise RuleError("게시 준비 중 설정이 변경되었습니다. 화면을 새로고침하고 다시 확인하세요.")
        job = store.reserve(fresh_video, body.comment, PROFILE_NAME, "live", values["daily_limit"])
        schedule(manager.publish(job, values, verify))
        return {"job_id": job["id"]}

    @app.post("/api/jobs/{job_id}/verify-existing")
    async def verify_existing(job_id: str, body: VerifyExistingRequest):
        # No new reservation, analysis generation or external mutation here.
        if body.confirmed is not True:
            raise RuleError("기존 댓글을 읽기 전용으로 재확인할지 승인하세요.")
        old = store.job(job_id)
        if not old:
            raise HTTPException(404, "기존 실행 기록을 찾지 못했습니다.")
        if old["mode"] != "live" or old["status"] != "unknown" or not old["post_started"]:
            raise RuleError("게시 후 결과 미확인인 기록에서만 기존 댓글을 재확인할 수 있습니다.")
        if store.has_active() or manager.lock.locked():
            raise RuleError("진행 중인 작업이 끝난 뒤 재확인하세요.")
        values = settings.get()
        if test_id(values) != old["video_id"] or values["test_channel_id"] != old["channel_id"]:
            raise RuleError("기존 기록의 본인 테스트 영상과 현재 설정이 다릅니다. 원래 테스트 영상 설정을 유지하세요.")
        service = YouTubeService(values["youtube_api_key"])

        def check_scope():
            fresh = service.resolve_video(old["video_id"])
            require_test(fresh, values)
            if fresh["channel_id"] != old["channel_id"]:
                raise RuleError("기존 기록과 영상의 실제 채널 ID가 다릅니다.")
            if service.channel_id_for_handle(values["expected_handle"]) != old["channel_id"]:
                raise RuleError("설정한 @핸들이 기존 게시 기록의 본인 채널과 다릅니다.")

        await asyncio.to_thread(check_scope)
        if settings.get() != values:
            raise RuleError("재확인 준비 중 설정이 변경되었습니다. 화면을 새로고침하세요.")
        # Atomic transition keeps the original day quota and the video lock.
        job = store.begin_verification(job_id)
        schedule(manager.verify_existing(job, lambda: verify_existing_comment(job, service)))
        return {"job_id": job["id"], "read_only": True}

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        job = store.job(job_id)
        if not job:
            raise HTTPException(404, "작업을 찾지 못했습니다.")
        return job

    @app.get("/api/history")
    async def history():
        return {"items": store.history()}

    @app.post("/api/profile/open")
    async def open_profile():
        return {"message": await manager.open_login()}

    @app.post("/api/profile/close")
    async def close_profile():
        if store.has_active():
            raise RuleError("진행 중인 작업이 끝난 뒤 크롬을 닫아주세요.")
        await manager.close()
        return {"message": "프로그램에서 연 자동화 크롬을 닫았습니다. 직접 연 로그인용 크롬은 창의 X 버튼으로 닫아주세요."}

    @app.get("/api/screenshots/{filename}")
    async def screenshot(filename: str):
        if not re.fullmatch(r"[a-f0-9]{32}\.png", filename):
            raise HTTPException(404, "화면 캡처를 찾지 못했습니다.")
        path = manager.screenshot_dir / filename
        if not path.is_file():
            raise HTTPException(404, "브라우저가 열리지 않은 경우 화면 캡처가 없을 수 있습니다.")
        return FileResponse(path, media_type="image/png")

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/practice")
    async def practice():
        return FileResponse(ROOT / "static" / "practice.html")

    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    return app
