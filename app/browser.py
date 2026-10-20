"""Visible, single-profile automation for an explicitly configured own test video.

No account rotation, proxy, stealth flag, CAPTCHA handling, or hidden posting.
Login is performed by the user in an ordinary dedicated Chrome window.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, unquote, urlparse

from .config import PROFILE_NAME
from .storage import RuleError, Store
from .verification import verify_existing_comment
from .youtube import ServiceError, YouTubeService


class BrowserError(Exception):
    pass


def find_chrome() -> str | None:
    candidates: list[str] = []
    if sys.platform == "win32":
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(key)
            if base:
                candidates.append(str(Path(base) / "Google/Chrome/Application/chrome.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for command in ("google-chrome", "google-chrome-stable", "chrome"):
        found = shutil.which(command)
        if found:
            candidates.append(found)
    return next((path for path in candidates if Path(path).is_file()), None)


def normalized_handle(value: str) -> str:
    return unquote(value).strip().casefold()


def normalized_comment(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class BrowserManager:
    def __init__(self, directory: Path, store: Store, base_url: str, *, headless_practice=False):
        self.directory = directory
        self.profile_dir = directory / "profiles" / "youtube1"
        self.screenshot_dir = directory / "screenshots"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.headless_practice = headless_practice
        self.lock = asyncio.Lock()
        self._playwright = None
        self._context = None
        self._practice_context = None
        self._practice_browser = None

    def prepared(self) -> bool:
        # This indicates that Chrome initialized a profile, NOT that login succeeded.
        return (self.profile_dir / "Default").is_dir()

    async def open_login(self) -> str:
        if self.lock.locked() or self.store.has_active():
            raise BrowserError("작업을 마친 다음 로그인 창을 열어주세요.")
        await self.close()
        executable = find_chrome()
        if not executable:
            raise BrowserError("Google Chrome을 찾지 못했습니다. Chrome 설치 후 다시 실행하세요.")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [executable, f"--user-data-dir={self.profile_dir}", "--profile-directory=Default",
             "--lang=ko-KR", "https://www.youtube.com/?hl=ko"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return "유튜브1 전용 크롬을 열었습니다. 직접 로그인하고 본인 @핸들을 확인한 뒤, 열린 전용 크롬 창을 모두 닫아주세요."

    async def _engine(self):
        if self._playwright is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
        return self._playwright

    async def _live_context(self):
        if self._context is not None:
            try:
                self._context.pages
                page = await self._context.new_page()
                await page.close()
                return self._context
            except Exception:
                self._context = None
        if not find_chrome():
            raise BrowserError("Google Chrome을 찾지 못했습니다. 실제 시연에는 Chrome이 필요합니다.")
        engine = await self._engine()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._context = await engine.chromium.launch_persistent_context(
                str(self.profile_dir), channel="chrome", headless=False,
                locale="ko-KR", no_viewport=True, accept_downloads=False,
                args=["--lang=ko-KR", "--start-maximized"], timeout=20000,
            )
            self._context.set_default_timeout(12000)
            return self._context
        except Exception as exc:
            raise BrowserError("전용 크롬을 열지 못했습니다. 로그인용 유튜브1 크롬 창을 모두 닫고 다시 시도하세요. 다른 프로그램이 같은 프로필을 사용 중일 수 있습니다.") from exc

    async def close(self):
        if self.lock.locked():
            raise BrowserError("입력 또는 확인 중에는 크롬을 닫을 수 없습니다. 작업 종료를 기다려주세요.")
        for attr in ("_context", "_practice_context"):
            context = getattr(self, attr)
            if context is not None:
                with suppress(Exception):
                    await context.close()
                setattr(self, attr, None)
        if self._practice_browser is not None:
            with suppress(Exception):
                await self._practice_browser.close()
            self._practice_browser = None
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    async def _identity(self, page, expected_handle: str):
        avatar = page.locator("ytd-masthead #avatar-btn").first
        try:
            await avatar.wait_for(state="visible", timeout=12000)
            await avatar.click(timeout=5000)
            header = page.locator("ytd-active-account-header-renderer").first
            await header.wait_for(state="visible", timeout=7000)
            handle_node = header.locator("#channel-handle")
            if await handle_node.count():
                handle = await handle_node.first.inner_text()
            else:
                tokens = re.findall(r"@[^\s]+", await header.inner_text())
                handle = tokens[0] if len(tokens) == 1 else ""
            await page.keyboard.press("Escape")
        except Exception as exc:
            with suppress(Exception):
                await page.keyboard.press("Escape")
            raise BrowserError("현재 로그인한 @핸들을 확인하지 못했습니다. 유튜브1 프로필에 로그인하고 한국어 YouTube 화면인지 확인하세요. 계정 메뉴 구조가 바뀐 경우 선택자 조정이 필요합니다.") from exc
        if not handle or normalized_handle(handle) != normalized_handle(expected_handle):
            raise BrowserError("로그인한 채널의 @핸들이 설정과 다릅니다. 유튜브1에서 게시할 채널로 전환한 뒤 설정의 @핸들을 확인하세요.")

    async def _comments_visible(self, page):
        comments = page.locator("ytd-comments#comments").first
        for _ in range(12):
            if await comments.count():
                await comments.scroll_into_view_if_needed(timeout=4000)
                if await page.locator("ytd-comment-simplebox-renderer #placeholder-area").count():
                    return comments
            await page.mouse.wheel(0, 650)
            await asyncio.sleep(0.35)
        raise BrowserError("댓글 입력창을 찾지 못했습니다. 댓글 사용 설정, 로그인, 연령 제한과 페이지 로딩 상태를 확인하세요.")

    async def _screenshot(self, page, job_id: str) -> str | None:
        if page is None or page.is_closed():
            return None
        try:
            # Local diagnostic only; mask account areas instead of capturing identity.
            masks = [page.locator("ytd-masthead"), page.locator("ytd-popup-container")]
            await page.screenshot(path=str(self.screenshot_dir / f"{job_id}.png"), full_page=False, mask=masks, timeout=6000)
            return f"/api/screenshots/{job_id}.png"
        except Exception:
            return None

    @staticmethod
    def _valid_permalink(href: str | None, video_id: str) -> str | None:
        if not href:
            return None
        if href.startswith("/"):
            href = "https://www.youtube.com" + href
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        if (parsed.scheme == "https" and parsed.netloc in ("www.youtube.com", "youtube.com")
                and parsed.path == "/watch" and query.get("v") == [video_id]
                and len(query.get("lc", [])) == 1
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", query["lc"][0])):
            return f"https://www.youtube.com/watch?v={video_id}&lc={query['lc'][0]}"
        return None

    @staticmethod
    def _author_matches(href: str, handle: str, channel_id: str = "") -> bool:
        try:
            parsed = urlparse(href)
            if parsed.netloc:
                if parsed.scheme not in ("", "https") or parsed.netloc not in ("youtube.com", "www.youtube.com"):
                    return False
            elif parsed.scheme or not href.startswith("/"):
                return False
            path = unquote(parsed.path).rstrip("/")
            if path.startswith("/@") and path.count("/") == 1:
                return normalized_handle(path[1:]) == normalized_handle(handle)
            return bool(channel_id and path == "/channel/" + channel_id)
        except (ValueError, UnicodeError):
            return False

    async def _find_own_comment(self, page, comment: str, handle: str, video_id: str,
                                channel_id: str = "") -> str | None:
        """Read one DOM snapshot; never combine evidence from different comments.

        Accept wrappers around the top-level renderer and author links using
        either the configured handle or the independently verified channel ID.
        Exclude reply renderers and links embedded in user-written comment text.
        Actual live YouTube markup is not available in an uploaded screenshot.
        """
        rows = await page.locator("ytd-comment-thread-renderer").evaluate_all("""threads => {
          const result = [];
          const rootsSelector = 'ytd-comment-view-model, ytd-comment-renderer';
          for (const thread of threads.slice(0, 100)) {
            for (const root of thread.querySelectorAll(rootsSelector)) {
              if (root.closest('ytd-comment-thread-renderer') !== thread) continue;
              if (root.closest('ytd-comment-replies-renderer')) continue;
              if (root.parentElement?.closest(rootsSelector)) continue;
              if (!root.getClientRects().length) continue;
              const text = root.querySelector('#content-text');
              const author = root.querySelector('#author-text');
              if (!text || !author || text.closest(rootsSelector) !== root || author.closest(rootsSelector) !== root) continue;
              const anchor = author.matches('a[href]') ? author : author.querySelector('a[href]');
              const links = [...root.querySelectorAll('a[href*="lc="]')]
                .filter(a => a.closest(rootsSelector) === root && !a.closest('#content-text'))
                .map(a => a.getAttribute('href'));
              result.push({text: text.innerText || text.textContent || '',
                author_href: anchor?.getAttribute('href') || '', links});
            }
          }
          return result;
        }""")
        matches = set()
        for row in rows:
            if normalized_comment(row.get("text", "")) != normalized_comment(comment):
                continue
            if not self._author_matches(row.get("author_href", ""), handle, channel_id):
                continue
            for href in row.get("links", []):
                permalink = self._valid_permalink(href, video_id)
                if permalink:
                    matches.add(permalink)
        return next(iter(matches)) if len(matches) == 1 else None

    async def _wait_for_comment(self, page, job: dict, settings: dict, *, rounds=16):
        for _ in range(rounds):
            match = await self._find_own_comment(page, job["comment"], settings["expected_handle"],
                                                  job["video_id"], job["channel_id"])
            if match:
                return match
            await asyncio.sleep(0.75)
        return None

    async def _confirm_submission(self, page, job: dict, settings: dict):
        """Prefer screen evidence; fall back to clearly labelled API double-read."""
        with suppress(Exception):
            await page.bring_to_front()
        try:
            permalink = await self._wait_for_comment(page, job, settings)
            if permalink:
                self.store.event(job["id"], "댓글 링크를 다시 열어 화면의 본문과 작성자 확인 중")
                await page.goto(permalink + "&hl=ko", wait_until="domcontentloaded", timeout=30000)
                with suppress(BrowserError):
                    await self._comments_visible(page)
                verified = await self._wait_for_comment(page, job, settings)
                if verified == permalink:
                    self.store.finish(job["id"], "success", "게시 완료 - 댓글 링크를 다시 열어 화면의 본문과 작성자를 확인했습니다.", comment_url=verified)
                    return
        except Exception:
            # Do not leak browser URL/credentials. Failed DOM checks are NOT proof.
            pass
        self.store.event(job["id"], "화면 확인 불충분 - YouTube API로 기존 댓글을 읽기 전용 교차 확인 중")
        service = YouTubeService(settings["youtube_api_key"])
        proof = None
        last_error = None
        for attempt in range(3):
            try:
                proof = await asyncio.to_thread(verify_existing_comment, self.store.job(job["id"]), service)
                break
            except ServiceError as exc:
                last_error = exc
                if exc.code != "comment_not_verified" or attempt == 2:
                    break
                await asyncio.sleep(3)
        if not proof:
            raise BrowserError((str(last_error) if last_error else "댓글을 다시 확인하지 못했습니다.") + " 재게시가 잠겨 있습니다.")
        # Display the verified permalink when possible, but do not call this a
        # successful screen check. The saved evidence identifies API verification.
        try:
            if not page.is_closed():
                await page.goto(proof["comment_url"] + "&hl=ko", wait_until="domcontentloaded", timeout=20000)
                with suppress(Exception):
                    await self._comments_visible(page)
                self.store.event(job["id"], "API로 확인한 댓글 링크를 화면에 열었습니다. 화면 자동 대조 성공과는 구분합니다.")
        except Exception:
            self.store.event(job["id"], "API 댓글 확인 완료. 브라우저 링크 열기는 완료하지 못했습니다.")
        self.store.finish(job["id"], "success", "게시 확인 - YouTube API 이중 조회로 영상·작성자 채널 ID·댓글 전문을 확인했습니다. (화면 자동 확인과 별도)",
                          comment_url=proof["comment_url"], verification=proof)

    async def verify_existing(self, job: dict, read_verification):
        """No browser launch, typing or submit call. Updates the SAME ledger row."""
        async with self.lock:
            try:
                self.store.event(job["id"], "기존 댓글 조회 중 - 영상 ID·작성자 채널 ID·전문 및 댓글 ID 재조회")
                proof = await asyncio.to_thread(read_verification)
                self.store.finish(job["id"], "success", "기존 댓글 확인 완료 - YouTube API 이중 조회. 새 댓글은 게시하지 않았습니다.",
                                  comment_url=proof["comment_url"], verification=proof)
            except Exception as exc:
                message = str(exc) if isinstance(exc, (ServiceError, BrowserError, RuleError)) else "기존 댓글 조회 중 오류가 발생했습니다. API 연결과 설정을 확인하세요."
                self.store.finish(job["id"], "unknown", "기존 댓글 재확인 미완료 - 새 댓글을 게시하지 않았습니다.",
                                  error=message + " 재게시 잠금은 유지됩니다.")

    async def publish(self, job: dict, settings: dict, verify_video):
        """verify_video is a synchronous trusted API check, called again before posting."""
        page = None
        async with self.lock:
            try:
                self.store.event(job["id"], "영상과 게시 범위 다시 확인 중")
                await asyncio.to_thread(verify_video)
                self.store.event(job["id"], "유튜브1 크롬 프로필 여는 중")
                context = await self._live_context()
                page = await context.new_page()
                await page.goto(f"https://www.youtube.com/watch?v={job['video_id']}&hl=ko", wait_until="domcontentloaded", timeout=35000)
                self.store.event(job["id"], "로그인한 채널 확인 중")
                await self._identity(page, settings["expected_handle"])
                await self._comments_visible(page)
                self.store.event(job["id"], "댓글 입력창 여는 중")
                await page.locator("ytd-comment-simplebox-renderer #placeholder-area").first.click()
                editor = page.locator('ytd-comment-simplebox-renderer #contenteditable-root[contenteditable="true"]').first
                await editor.wait_for(state="visible", timeout=10000)
                if (await editor.inner_text()).strip():
                    raise BrowserError("댓글 창에 이미 입력된 내용이 있습니다. 사용자가 작성 중인 내용을 덮어쓰지 않도록 중단했습니다.")
                self.store.event(job["id"], "입력 중 - 한 글자씩 타이핑")
                await editor.press_sequentially(job["comment"], delay=65)
                if normalized_comment(await editor.inner_text()) != normalized_comment(job["comment"]):
                    raise BrowserError("화면에 입력된 글이 승인한 초안과 다릅니다. 게시하지 않았습니다.")
                if parse_qs(urlparse(page.url).query).get("v") != [job["video_id"]]:
                    raise BrowserError("선택한 영상과 현재 화면의 영상이 다릅니다. 게시하지 않았습니다.")
                # A second trusted metadata check closes the gap between review and submit.
                await asyncio.to_thread(verify_video)
                await self._identity(page, settings["expected_handle"])
                if normalized_comment(await editor.inner_text()) != normalized_comment(job["comment"]):
                    raise BrowserError("게시 직전 입력 내용이 달라졌습니다. 게시하지 않았습니다.")
                self.store.event(job["id"], "게시 준비 완료 - 승인된 댓글 제출 중")
                submit = page.locator("ytd-comment-simplebox-renderer #submit-button").first.get_by_role("button", name=re.compile(r"^(댓글|Comment)$"))
                if not await submit.count():
                    submit = page.locator("ytd-comment-simplebox-renderer #submit-button button").first
                await submit.wait_for(state="visible", timeout=5000)
                if not await submit.is_enabled():
                    raise BrowserError("댓글 게시 버튼이 비활성화되어 있습니다. 게시하지 않았습니다.")
                self.store.mark_post_started(job["id"], settings["daily_limit"])
                await submit.click(timeout=12000)
                self.store.event(job["id"], "게시 응답 확인 중 - 내 댓글과 작성자 확인")
                await self._confirm_submission(page, job, settings)
            except Exception as exc:
                screenshot = await self._screenshot(page, job["id"])
                message = str(exc) if isinstance(exc, (BrowserError, RuleError)) else "크롬 화면 제어 중 오류가 발생했습니다. 로그인, 네트워크, 한국어 화면과 프로필 사용 상태를 확인하세요."
                current = self.store.job(job["id"])
                uncertain = bool(current and current["post_started"])
                if uncertain and "재게시" not in message:
                    message += " 게시 여부를 확인할 수 없어 재게시가 잠겨 있습니다."
                self.store.finish(job["id"], "unknown" if uncertain else "failed", "게시 여부 미확인" if uncertain else "게시 전 중단", error=message, screenshot_url=screenshot)

    async def rehearse(self, job: dict):
        page = None
        async with self.lock:
            try:
                self.store.event(job["id"], "연습용 크롬 여는 중 - YouTube 연결 없음")
                engine = await self._engine()
                if self._practice_context is None:
                    options = {"headless": self.headless_practice}
                    if find_chrome() and not self.headless_practice:
                        options["channel"] = "chrome"
                    browser = await engine.chromium.launch(**options)
                    self._practice_browser = browser
                    self._practice_context = await browser.new_context(locale="ko-KR", viewport={"width": 1080, "height": 760})
                page = await self._practice_context.new_page()
                await page.goto(f"{self.base_url}/practice?job={job['id']}", wait_until="domcontentloaded")
                editor = page.locator('[data-testid="practice-editor"]')
                await editor.click()
                self.store.event(job["id"], "연습 입력 중 - 한 글자씩 타이핑")
                await editor.press_sequentially(job["comment"], delay=45 if not self.headless_practice else 1)
                if (await editor.input_value()) != job["comment"]:
                    raise BrowserError("연습 화면의 입력 내용이 초안과 다릅니다.")
                await page.get_by_role("button", name="연습 댓글 저장", exact=True).click()
                await page.locator('[data-testid="practice-saved"]').wait_for(state="visible")
                if (await page.locator('[data-testid="practice-saved"]').inner_text()) != job["comment"]:
                    raise BrowserError("연습 화면의 결과 확인에 실패했습니다.")
                self.store.finish(job["id"], "rehearsed", "로컬 입력 연습 완료 - 실제 YouTube 게시 실적이 아닙니다.")
            except Exception as exc:
                screenshot = await self._screenshot(page, job["id"])
                message = str(exc) if isinstance(exc, BrowserError) else "연습용 브라우저를 실행하지 못했습니다. Chrome 설치 상태를 확인하고 다시 시도하세요."
                self.store.finish(job["id"], "failed", "로컬 연습 실패", error=message, screenshot_url=screenshot)
