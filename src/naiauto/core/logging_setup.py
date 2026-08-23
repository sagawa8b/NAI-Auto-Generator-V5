"""로깅 구성 — 일반/디버그 레벨 전환 + 크래시 추적 (Qt 없음).

로그는 항상 파일로 남는다. 사용자가 재현하기 어려운 문제(스크롤 중 강제 종료
같은)를 사후에 볼 수 있어야 하기 때문이다.

  naiauto.log        일반 로그 (INFO, 디버그 모드면 DEBUG)
  naiauto.crash.log  faulthandler가 남기는 네이티브 스택 — 파이썬 예외가 아니라
                     세그폴트 등으로 프로세스가 죽을 때의 유일한 단서

파일은 회전(rotate)시켜 무한히 커지지 않게 한다.
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import sys
from pathlib import Path

from .settings.schema import default_log_dir

LOG_NAME = "naiauto.log"
CRASH_LOG_NAME = "naiauto.crash.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

_crash_file = None  # faulthandler가 계속 쓰므로 살려 둔다

_logger = logging.getLogger(__name__)


class LoggingConfigError(RuntimeError):
    """로그 디렉터리를 쓸 수 없다 (Req 8.7)."""


def log_path(log_dir: Path) -> Path:
    return Path(log_dir) / LOG_NAME


def crash_log_path(log_dir: Path) -> Path:
    return Path(log_dir) / CRASH_LOG_NAME


def configure_logging(log_dir: Path, *, debug: bool = False) -> Path:
    """루트 로거를 (재)구성하고 로그 파일 경로를 돌려준다.

    디버그 모드를 켜고 끌 때 다시 불러도 된다 — 핸들러가 쌓이지 않는다.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_path(log_dir)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(FORMAT)
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    # 디버그 모드에서도 라이브러리 내부 로그까지 쏟아지면 읽을 수 없다
    logging.getLogger("urllib3").setLevel(logging.INFO)
    logging.getLogger("PIL").setLevel(logging.INFO)
    return path


def enable_crash_log(log_dir: Path) -> Path:
    """세그폴트 등으로 죽을 때 네이티브 스택을 파일에 남긴다.

    다시 불러도 된다 — 이전 핸들은 새 핸들로 갈아탄 뒤 닫는다 (핸들 누수 방지).
    """
    global _crash_file
    path = crash_log_path(Path(log_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8", buffering=1)
    previous = _crash_file
    # faulthandler를 새 파일로 옮긴 뒤에 이전 핸들을 닫는다 (닫힌 파일을 가리키지 않도록)
    _crash_file = handle
    faulthandler.enable(file=handle, all_threads=True)
    if previous is not None and previous is not handle:
        try:
            previous.close()
        except OSError:  # 이미 닫혔거나 디스크 오류 — 크래시 로그 때문에 죽을 이유는 없다
            _logger.debug("failed to close previous crash log handle", exc_info=True)
    return path


def probe_log_dir(log_dir: Path) -> None:
    """디렉터리를 만들고 로그 파일에 append 모드로 열어 본다. 실패 시 LoggingConfigError."""
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        with log_path(Path(log_dir)).open("a", encoding="utf-8"):
            pass
    except OSError as exc:
        raise LoggingConfigError(f"cannot use log dir {log_dir}: {exc}") from exc


def reconfigure_logging(
    log_dir: Path,
    *,
    debug: bool,
    previous_dir: Path,
    previous_debug: bool,
    crash_log: bool = True,
) -> Path:
    """로그 레벨/디렉터리를 바꾼다. 실패하면 이전 설정으로 되돌리고 예외를 올린다.

    성공: 새 디렉터리로 configure_logging + enable_crash_log, 새 로그 경로 반환
    실패: previous_dir/previous_debug로 configure_logging 재실행 후 LoggingConfigError
          (롤백까지 실패하면 OS 표준 디렉터리로 한 번 더 시도하고 로그만 남긴다)

    디렉터리를 먼저 검사(probe)하므로, 쓸 수 없는 경로를 받은 경우 루트 로거는 손대지도
    않는다 — 이전 디렉터리로 로그가 계속 쌓인다 (Req 8.7).
    """
    log_dir = Path(log_dir)
    disturbed = False
    try:
        probe_log_dir(log_dir)
        disturbed = True  # 여기부터는 루트 로거 핸들러를 갈아치운다
        path = configure_logging(log_dir, debug=debug)
        if crash_log:
            enable_crash_log(log_dir)
        return path
    except (LoggingConfigError, OSError) as exc:
        error = (
            exc
            if isinstance(exc, LoggingConfigError)
            else LoggingConfigError(f"cannot use log dir {log_dir}: {exc}")
        )
        if disturbed:
            _rollback_logging(Path(previous_dir), previous_debug, crash_log=crash_log)
        raise error from exc


def _rollback_logging(previous_dir: Path, previous_debug: bool, *, crash_log: bool) -> None:
    """이전 디렉터리로 로깅을 복구한다. 그것도 실패하면 OS 표준 → 스트림 전용으로 내려간다."""
    for candidate in (previous_dir, default_log_dir()):
        try:
            configure_logging(candidate, debug=previous_debug)
            if crash_log:
                enable_crash_log(candidate)
        except OSError:
            continue
        if candidate != previous_dir:
            _logger.critical("logging rolled back to OS default dir %s", candidate)
        return

    _configure_stream_only(previous_debug)
    _logger.critical("no writable log dir — logging to stderr only")


def _configure_stream_only(debug: bool) -> None:
    """파일 핸들러를 만들 수 없을 때의 최후 수단 — 콘솔에라도 남긴다."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(FORMAT))
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(stream_handler)


def install_excepthook() -> None:
    """잡히지 않은 예외를 로그에 남긴다 (콘솔 없이 실행돼도 흔적이 남도록)."""
    logger = logging.getLogger("naiauto.unhandled")
    previous = sys.excepthook

    def hook(exc_type, exc_value, traceback) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical("unhandled exception", exc_info=(exc_type, exc_value, traceback))
        previous(exc_type, exc_value, traceback)

    sys.excepthook = hook


def read_log(path: Path, max_bytes: int = 256 * 1024) -> str:
    """로그 뷰어용 — 파일 끝에서 max_bytes만 읽는다 (커도 즉시 열리도록)."""
    path = Path(path)
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:  # 잘린 첫 줄은 버린다
        text = text.split("\n", 1)[-1]
    return text


__all__ = [
    "CRASH_LOG_NAME",
    "LOG_NAME",
    "LoggingConfigError",
    "configure_logging",
    "crash_log_path",
    "enable_crash_log",
    "install_excepthook",
    "log_path",
    "probe_log_dir",
    "read_log",
    "reconfigure_logging",
]
