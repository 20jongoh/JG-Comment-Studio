"""Start the local application. Do not use uvicorn --reload on Windows."""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
import webbrowser
from contextlib import contextmanager


@contextmanager
def single_instance(directory):
    """Hold an OS file lock so a second process cannot recover an active job."""
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "instance.lock").open("a+b")
    try:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("같은 자료 폴더를 사용하는 프로그램이 이미 실행 중입니다. 기존 창을 사용하거나 기존 프로그램을 종료하세요.") from None
        yield
    finally:
        handle.close()  # The operating system releases the lock, even after a crash.


def main():
    parser = argparse.ArgumentParser(description="맥락 스튜디오 - 로컬 과제 프로그램")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--headless-practice", action="store_true", help="개발용 로컬 연습 검사에만 사용")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 이상이 필요합니다.")
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", args.port))
    except OSError:
        raise SystemExit(f"{args.port}번 포트를 사용할 수 없습니다. 이미 실행 중인지 확인하거나 --port 8766으로 실행하세요.")
    finally:
        probe.close()
    # Python subprocess support is needed for Playwright on Windows.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    os.environ.setdefault("PYTHONUTF8", "1")
    from app.main import create_app
    from app.config import DATA_DIR
    import uvicorn
    with single_instance(DATA_DIR):
        app = create_app(port=args.port, headless_practice=args.headless_practice)
        url = f"http://127.0.0.1:{args.port}"
        if not args.no_open:
            timer = threading.Timer(1.5, lambda: webbrowser.open(url))
            timer.daemon = True
            timer.start()
        print(f"\n맥락 스튜디오: {url}\n이 창을 닫으면 프로그램이 종료됩니다. 종료: Ctrl+C\n")
        # Explicit asyncio.run avoids a server choosing a Selector loop on Windows.
        config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", loop="asyncio", access_log=False)
        asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
