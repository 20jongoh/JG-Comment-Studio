"""Durable reservations, a single-video lock, and a KST daily posting ledger."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .verification import proof_matches_job
import sqlite3
import uuid

KST = timezone(timedelta(hours=9))
ACTIVE = ("pending", "running")
LOCKING = ("pending", "running", "success", "unknown")


def now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(KST).date().isoformat()


class RuleError(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS videos (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analyses (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    video_title TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    comment TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    post_started INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    screenshot_url TEXT,
                    comment_url TEXT,
                    events TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_live_comment_per_video
                ON jobs(video_id) WHERE mode = 'live'
                  AND status IN ('pending','running','success','unknown');
            """)
            # Additive migration: keep all old jobs, events, locks and day keys.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "verification" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN verification TEXT")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def put_video(self, video: dict):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO videos VALUES (?,?)", (video["id"], json.dumps(video, ensure_ascii=False)))

    def video(self, video_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM videos WHERE id=?", (video_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def put_analysis(self, analysis: dict) -> dict:
        result = dict(analysis)
        result["analysis_id"] = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute("INSERT INTO analyses VALUES (?,?,?)", (result["analysis_id"], now(), json.dumps(result, ensure_ascii=False)))
        return result

    def analysis(self, analysis_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload FROM analyses WHERE id=?", (analysis_id,)).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _daily_count(conn, day: str, exclude_id: str = "") -> int:
        return conn.execute("""SELECT COUNT(*) FROM jobs
            WHERE mode='live' AND day_key=? AND id!=?
              AND (status IN ('pending','running','success','unknown') OR post_started=1)
            """, (day, exclude_id)).fetchone()[0]

    def stats(self, daily_limit: int) -> dict:
        with self.connect() as conn:
            return {"daily_used": self._daily_count(conn, today()), "daily_limit": daily_limit,
                    "total_success": conn.execute("SELECT COUNT(*) FROM jobs WHERE mode='live' AND status='success'").fetchone()[0]}

    def has_active(self) -> bool:
        with self.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM jobs WHERE status IN ('pending','running') LIMIT 1").fetchone())

    def reserve(self, video: dict, comment: str, profile: str, mode: str, daily_limit: int) -> dict:
        if mode not in ("live", "sample"):
            raise RuleError("작업 모드가 올바르지 않습니다.")
        job_id = uuid.uuid4().hex
        event_time = now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if mode == "live":
                existing = conn.execute("""SELECT status FROM jobs WHERE mode='live' AND video_id=?
                    AND status IN ('pending','running','success','unknown') LIMIT 1""", (video["id"],)).fetchone()
                if existing:
                    raise RuleError("이 영상은 이미 게시했거나 게시 여부를 확인 중입니다. 영상당 한 번만 게시할 수 있습니다.")
                if self._daily_count(conn, today()) >= daily_limit:
                    raise RuleError("오늘의 게시 상한에 도달했습니다. 한국시간 자정을 기준으로 계산합니다.")
            if conn.execute("SELECT 1 FROM jobs WHERE status IN ('pending','running') LIMIT 1").fetchone():
                raise RuleError("진행 중인 작업을 마친 뒤 다시 시도하세요.")
            events = [{"time": event_time, "message": "실제 게시 예약" if mode == "live" else "로컬 연습 예약 - YouTube에 게시하지 않습니다."}]
            conn.execute("""INSERT INTO jobs
                (id,created_at,day_key,video_id,channel_id,video_title,profile,comment,mode,status,stage,events)
                VALUES (?,?,?,?,?,?,?,?,?,'pending','준비 중',?)""",
                (job_id, event_time, today(), video["id"], video.get("channel_id", ""), video["title"], profile,
                 comment, mode, json.dumps(events, ensure_ascii=False)))
        return self.job(job_id)

    def event(self, job_id: str, stage: str, status: str = "running"):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT events,status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] not in ACTIVE:
                return
            events = json.loads(row["events"])
            events.append({"time": now(), "message": stage})
            conn.execute("UPDATE jobs SET stage=?,status=?,events=? WHERE id=?", (stage, status, json.dumps(events, ensure_ascii=False), job_id))

    def begin_verification(self, job_id: str) -> dict:
        """Reopen ONLY an uncertain, already-submitted job for read-only checks.

        Keeps its ID, day_key, created_at, post_started and per-video lock.
        Concurrent clicks cannot launch another task. No new posting is allowed.
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["mode"] != "live" or row["status"] != "unknown" or not row["post_started"]:
                raise RuleError("게시 후 결과 미확인인 기존 기록만 재확인할 수 있습니다.")
            if conn.execute("SELECT 1 FROM jobs WHERE status IN ('pending','running') LIMIT 1").fetchone():
                raise RuleError("진행 중인 작업이 끝난 뒤 재확인하세요.")
            events = json.loads(row["events"])
            if row["error"]:
                events.append({"time": now(), "message": "이전 확인 오류 보존: " + row["error"]})
            stage = "기존 댓글 읽기 전용 재확인 중 - 새 댓글을 게시하지 않습니다."
            events.append({"time": now(), "message": stage})
            conn.execute("UPDATE jobs SET status='running',stage=?,error=NULL,events=? WHERE id=?",
                         (stage, json.dumps(events, ensure_ascii=False), job_id))
        return self.job(job_id)

    def mark_post_started(self, job_id: str, daily_limit: int):
        """Commit BEFORE the external submit click. Recheck a midnight crossing."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["mode"] != "live" or row["status"] not in ACTIVE or row["post_started"]:
                raise RuleError("게시 작업의 상태가 올바르지 않습니다.")
            if self._daily_count(conn, today(), exclude_id=job_id) >= daily_limit:
                raise RuleError("게시 직전 일일 상한에 도달하여 중단했습니다.")
            conn.execute("UPDATE jobs SET post_started=1,day_key=? WHERE id=?", (today(), job_id))

    def finish(self, job_id: str, status: str, message: str, *, error: str | None = None,
               screenshot_url: str | None = None, comment_url: str | None = None, verification: dict | None = None):
        if status not in ("success", "failed", "unknown", "rehearsed"):
            raise ValueError("Unknown terminal status")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] not in ACTIVE:
                return
            if verification is not None:
                if status != "success" or not proof_matches_job(verification, dict(row)):
                    raise RuleError("기존 기록과 일치하는 댓글 확인 근거가 없어 성공 처리하지 않았습니다.")
                if comment_url != verification["comment_url"]:
                    raise RuleError("확인된 댓글 링크와 저장할 링크가 다릅니다.")
            if row["post_started"] and status == "failed":
                status = "unknown"
            events = json.loads(row["events"])
            events.append({"time": now(), "message": message})
            conn.execute("""UPDATE jobs SET status=?,stage=?,error=?,screenshot_url=COALESCE(?,screenshot_url),comment_url=?,events=?,verification=? WHERE id=?""",
                         (status, message, error, screenshot_url, comment_url, json.dumps(events, ensure_ascii=False),
                          json.dumps(verification, ensure_ascii=False) if verification else None, job_id))

    def recover_interrupted(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT id,post_started FROM jobs WHERE status IN ('pending','running')").fetchall()
        for row in rows:
            if row["post_started"]:
                self.finish(row["id"], "unknown", "프로그램이 중단되어 게시 결과를 확인하지 못했습니다. 재게시가 잠겨 있습니다.", error="작업 중 프로그램 종료")
            else:
                self.finish(row["id"], "failed", "게시 전 프로그램이 중단되었습니다.", error="작업 중 프로그램 종료")

    @staticmethod
    def _decode(row) -> dict | None:
        if not row:
            return None
        result = dict(row)
        result["events"] = json.loads(result["events"])
        result["verification"] = json.loads(result["verification"]) if result.get("verification") else None
        return result

    def job(self, job_id: str) -> dict | None:
        with self.connect() as conn:
            return self._decode(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def history(self, limit=100) -> list[dict]:
        with self.connect() as conn:
            return [self._decode(r) for r in conn.execute("SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT ?", (limit,)).fetchall()]
