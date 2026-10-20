"""Local configuration; credentials never leave this module through the UI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("JG_STUDIO_DATA_DIR", str(ROOT / "runtime_data"))).resolve()
PROFILE_NAME = "유튜브1"
DEFAULTS = {
    "youtube_api_key": "",
    "ai_api_key": "",
    "ai_provider": "gemini",
    "ai_model": "gemini-3.8-flash",
    "test_video_url": "",
    "test_channel_id": "",
    "expected_handle": "",
    "daily_limit": 10,
}


class Settings:
    def __init__(self, directory: Path = DATA_DIR):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "settings.json"
        self.lock = threading.RLock()
        self.values = dict(DEFAULTS)
        if self.path.exists():
            try:
                stored = json.loads(self.path.read_text(encoding="utf-8"))
                self.values.update({k: v for k, v in stored.items() if k in DEFAULTS})
            except (OSError, ValueError) as exc:
                raise RuntimeError("설정 파일을 읽지 못했습니다. runtime_data/settings.json을 확인하세요.") from exc

    def get(self) -> dict:
        with self.lock:
            return dict(self.values)

    def public(self) -> dict:
        values = self.get()
        return {
            **{k: v for k, v in values.items() if k not in ("youtube_api_key", "ai_api_key")},
            "youtube_api_key_set": bool(values["youtube_api_key"]),
            "ai_api_key_set": bool(values["ai_api_key"]),
        }

    def update(self, patch: dict) -> dict:
        with self.lock:
            candidate = dict(self.values)
            for key, value in patch.items():
                if key not in DEFAULTS or value is None:
                    continue
                if isinstance(value, str):
                    value = value.strip()
                if key in ("youtube_api_key", "ai_api_key") and not value:
                    continue  # Blank password fields preserve a saved credential.
                candidate[key] = value
            if candidate["ai_provider"] not in ("gemini", "openai"):
                raise ValueError("AI 제공자는 gemini 또는 openai를 선택하세요.")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", candidate["ai_model"]):
                raise ValueError("올바른 AI 모델 ID를 입력하세요.")
            if isinstance(candidate["daily_limit"], bool) or not 1 <= int(candidate["daily_limit"]) <= 10:
                raise ValueError("일일 게시 상한은 1~10개로 설정하세요.")
            candidate["daily_limit"] = int(candidate["daily_limit"])
            if candidate["test_channel_id"] and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", candidate["test_channel_id"]):
                raise ValueError("본인 채널 ID는 UC로 시작하는 24자리 ID입니다.")
            handle = candidate["expected_handle"]
            if handle and (not handle.startswith("@") or any(ch.isspace() for ch in handle) or len(handle) > 100):
                raise ValueError("현재 게시 계정의 @핸들을 공백 없이 입력하세요.")
            for key in ("youtube_api_key", "ai_api_key"):
                if len(candidate[key]) > 1000 or any(ch.isspace() for ch in candidate[key]):
                    raise ValueError("API 키에 공백이 있거나 길이가 올바르지 않습니다.")
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                temp.chmod(0o600)
            except OSError:
                pass
            os.replace(temp, self.path)
            self.values = candidate
            return self.public()
