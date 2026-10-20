"""Local Chromium markup fixtures and mocked submission verification. No login/API."""
import asyncio
from html import escape
import shutil

import pytest
from playwright.async_api import async_playwright

from app.browser import BrowserManager, BrowserError
from app.storage import Store
from app.youtube import YouTubeService
from tests_helper_v014 import VID, CID, COMMENT, COMMENT_ID, unknown, FixtureService


URL = f"https://www.youtube.com/watch?v={VID}&lc={COMMENT_ID}"


def markup(*, wrapper=False, author=None, body=COMMENT, link=URL, root_tag="ytd-comment-view-model"):
    author = author or "/@own"
    root = f'<{root_tag}><a id="author-text" href="{escape(author, quote=True)}">@own</a>'
    root += f'<span id="published-time-text"><a href="{escape(link, quote=True)}">7분 전</a></span>'
    root += f'<div id="content-text">{escape(body)}</div></{root_tag}>'
    return f'<ytd-comment-thread-renderer>{"<div id=body>" if wrapper else ""}{root}{"</div>" if wrapper else ""}</ytd-comment-thread-renderer>'


@pytest.mark.parametrize("case", ["direct", "wrapped", "old_renderer", "channel_link", "wrong_author", "reply_only", "mixed_reply_link", "link_inside_text", "wrong_video", "partial", "ambiguous"])
def test_dom_lookup_uses_a_whole_top_level_comment(case, tmp_path):
    html = markup()
    expected = URL
    if case == "wrapped": html = markup(wrapper=True)
    elif case == "old_renderer": html = markup(root_tag="ytd-comment-renderer")
    elif case == "channel_link": html = markup(author="/channel/" + CID)
    elif case == "wrong_author": html = markup(author="/@different"); expected = None
    elif case == "reply_only": html = '<ytd-comment-replies-renderer>' + markup() + '</ytd-comment-replies-renderer>'; expected = None
    elif case == "mixed_reply_link":
        html = markup(link="/watch?v=" + VID).replace('</ytd-comment-thread-renderer>', '<ytd-comment-replies-renderer>' + markup(body="different reply") + '</ytd-comment-replies-renderer></ytd-comment-thread-renderer>')
        expected = None
    elif case == "link_inside_text":
        html = markup(link="/watch?v=" + VID).replace(escape(COMMENT), f'<a href="{URL}">{escape(COMMENT)}</a>')
        expected = None
    elif case == "wrong_video": html = markup(link=URL.replace(VID, "01234567890")); expected = None
    elif case == "partial": html = markup(body=COMMENT[:10]); expected = None
    elif case == "ambiguous": html = markup() + markup(link=URL + "other"); expected = None

    async def run():
        async with async_playwright() as pw:
            executable = shutil.which("chromium")
            options = {"headless": True}
            if executable: options["executable_path"] = executable
            browser = await pw.chromium.launch(**options)
            page = await browser.new_page()
            await page.set_content('<style>ytd-comment-thread-renderer,ytd-comment-view-model,ytd-comment-renderer{display:block}</style>' + html)
            manager = BrowserManager(tmp_path, Store(tmp_path / "db.sqlite3"), "http://127.0.0.1:8765")
            result = await manager._find_own_comment(page, COMMENT, "@own", VID, CID)
            assert result == expected
            await browser.close()
    asyncio.run(run())


class Page:
    def __init__(self, fails=False): self.urls = []; self.fails = fails
    async def bring_to_front(self): pass
    async def goto(self, url, **kwargs):
        self.urls.append(url)
        if self.fails: raise RuntimeError("Synthetic closed page")
    def is_closed(self): return False


def test_post_confirmation_uses_api_if_dom_evidence_is_unavailable(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite3"); j = unknown(store); j = store.begin_verification(j["id"])
    manager = BrowserManager(tmp_path, store, "http://127.0.0.1:8765")
    async def no_dom(*a, **k): return None
    async def visible(*a, **k): return None
    monkeypatch.setattr(manager, "_wait_for_comment", no_dom)
    monkeypatch.setattr(manager, "_comments_visible", visible)
    fixture = FixtureService()
    monkeypatch.setattr(YouTubeService, "_get", lambda self, r, p: fixture._get(r, p))
    page = Page()
    asyncio.run(manager._confirm_submission(page, j, {"expected_handle": "@own", "youtube_api_key": "fixture"}))
    result = store.job(j["id"])
    assert result["status"] == "success" and result["verification"]["method"] == "youtube_api_double_read"
    assert result["comment_url"] == URL
    assert page.urls == [URL + "&hl=ko"]
    assert "화면 자동 확인과 별도" in result["stage"]
    assert len(store.history()) == 1


def test_dom_permalink_double_read_is_still_supported_without_api(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite3"); j = unknown(store); j = store.begin_verification(j["id"])
    manager = BrowserManager(tmp_path, store, "http://127.0.0.1:8765")
    async def matching(*a, **k): return URL
    async def visible(*a, **k): return None
    monkeypatch.setattr(manager, "_wait_for_comment", matching)
    monkeypatch.setattr(manager, "_comments_visible", visible)
    def forbidden(*a, **k): pytest.fail("DOM verified result must not require API fallback")
    monkeypatch.setattr(YouTubeService, "_get", forbidden)
    page = Page()
    asyncio.run(manager._confirm_submission(page, j, {"expected_handle": "@own", "youtube_api_key": "fixture"}))
    result = store.job(j["id"])
    assert result["status"] == "success" and result["verification"] is None
    assert page.urls == [URL + "&hl=ko"]


def test_closed_browser_does_not_erase_independent_api_evidence(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite3"); j = unknown(store); j = store.begin_verification(j["id"])
    manager = BrowserManager(tmp_path, store, "http://127.0.0.1:8765")
    async def no_dom(*a, **k): raise RuntimeError("Synthetic detached DOM")
    monkeypatch.setattr(manager, "_wait_for_comment", no_dom)
    fixture = FixtureService()
    monkeypatch.setattr(YouTubeService, "_get", lambda self, r, p: fixture._get(r, p))
    asyncio.run(manager._confirm_submission(Page(fails=True), j, {"expected_handle": "@own", "youtube_api_key": "fixture"}))
    result = store.job(j["id"])
    assert result["status"] == "success" and result["verification"]
    assert "브라우저 링크 열기는 완료하지 못했습니다" in str(result["events"])
