# 오픈소스 및 공식 문서 출처

이 프로젝트는 아래 라이브러리를 설치해 사용합니다. 라이브러리 소스나 바이너리를 ZIP에 복제해 포함하지 않습니다.
원저작자의 저작권과 라이선스는 각 프로젝트에 있습니다. 이 목록은 원 라이선스를 대체하지 않습니다.

| 프로젝트 | 용도 | 원문 |
| --- | --- | --- |
| FastAPI | 로컬 HTTP API | https://github.com/fastapi/fastapi |
| Uvicorn | ASGI 서버 | https://github.com/Kludex/uvicorn |
| HTTPX | YouTube·AI API 호출 | https://github.com/encode/httpx |
| Playwright | 전용 Chrome 제어와 로컬 화면 검증 | https://github.com/microsoft/playwright-python |
| youtube-transcript-api | 선택적 공개 자막 읽기 | https://github.com/jdepoix/youtube-transcript-api |
| pytest | 개발용 검증 | https://github.com/pytest-dev/pytest |

직접 의존성과 버전은 requirements.txt, 개발 검사 의존성은 requirements-dev.txt에 있습니다.
Python 표준 라이브러리의 SQLite·JSON·HTTP 관련 기능도 사용합니다.

## 설계에 참고한 공식 문서

- 채널 업로드 목록: https://developers.google.com/youtube/data/reference/rest/v3/playlistItems/list
- 영상 메타데이터: https://developers.google.com/youtube/data/reference/rest/v3/videos/list
- 댓글 조회: https://developers.google.com/youtube/data/reference/rest/v3/commentThreads/list
- 자막 다운로드 권한: https://developers.google.com/youtube/data/reference/rest/v3/captions/download
- 전용 프로필: https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context
- 한 글자씩 입력: https://playwright.dev/python/docs/api/class-locator#locator-press-sequentially
- Gemini 구조화 출력: https://ai.google.dev/gemini-api/docs/structured-output
- OpenAI Responses API: https://platform.openai.com/docs/api-reference/responses

검토 기준일: 2026-10-19. 외부 API와 YouTube UI는 이후 변경될 수 있습니다.
