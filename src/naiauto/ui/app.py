"""컴포지션 루트 — 프로젝트에서 유일하게 전역 조립을 하는 곳.

설정 로드 → i18n → 세션/클라이언트 → 로그인 → 서비스 → 메인 윈도우.
import-time 부작용 없음: 모든 초기화는 main() 안에서만 일어난다.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import platformdirs
from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QDialog

from .. import __version__
from ..core.api.client import NAIClient
from ..core.api.session import NAISession
from ..core.artist_combos import ArtistComboEngine
from ..core.i18n.manager import I18nManager
from ..core.logging_setup import configure_logging, enable_crash_log, install_excepthook
from ..core.settings import credentials
from ..core.settings.schema import APP_NAME, AppSettings
from ..core.settings.store import ensure_dirs, load_settings, save_settings
from ..core.wildcards.applier import WildcardApplier
from ..services.generation_service import GenerationService
from .login_dialog import LoginDialog
from .main_window import MainWindow
from .qt_bridge import QtEventBridge

TOKEN_CREDENTIAL_KEY = "api_token"


_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def log_dir() -> Path:
    return Path(platformdirs.user_log_dir(APP_NAME))


def build_service(client: NAIClient, settings: AppSettings, bridge: QtEventBridge) -> GenerationService:
    """프롬프트 확장 엔진까지 붙인 생성 서비스.

    와일드카드는 `apply()`마다 폴더를 다시 읽지만 아티스트 조합 엔진은 그렇지 않아
    여기서 한 번 `load()`해 둬야 `{artist:그룹}` 치환이 동작한다.
    """
    wildcards = WildcardApplier(settings.wildcards_dir)
    artist_combos = ArtistComboEngine(settings.artist_combos_dir)
    artist_combos.load()
    return GenerationService(client, wildcards=wildcards, artist_combos=artist_combos, on_event=bridge)


def _install_qt_message_handler() -> None:
    """Qt 자체 경고도 로그 파일에 남긴다 (콘솔 없이 실행하면 사라지므로)."""
    qt_logger = logging.getLogger("qt")

    def handler(mode: QtMsgType, context, message: str) -> None:
        qt_logger.log(_QT_LEVELS.get(mode, logging.INFO), "%s", message)

    qInstallMessageHandler(handler)


def selftest() -> int:
    """프로즌 빌드가 실제로 쓸 수 있는 상태인지 확인한다 (릴리스 워크플로가 호출).

    PyInstaller로 묶으면 조용히 깨지기 쉬운 세 가지를 실행해 본다:
    번들 리소스(언어 파일 4종·내장 태그 DB)와 keyring 백엔드. 어느 하나라도
    빠지면 앱은 뜨지만 "자동완성이 안 되고 토큰이 저장 안 되는" 상태가 된다.
    """
    from ..core.settings import credentials
    from ..core.tag_completer import TagCompleter, bundled_database_path

    failures: list[str] = []

    languages = sorted(I18nManager().get_available_languages())
    if len(languages) < 4:
        failures.append(f"언어 리소스 부족: {languages}")

    completer = TagCompleter(bundled_database_path())
    if not completer.load() or completer.tag_count == 0:
        failures.append(f"내장 태그 DB 로드 실패: {bundled_database_path()}")

    if not credentials.is_available():
        failures.append("keyring 백엔드를 쓸 수 없다 (토큰이 저장되지 않는다)")

    for line in failures:
        print(f"selftest FAIL: {line}")
    if failures:
        return 1
    print(f"selftest OK — v{__version__}, 언어 {len(languages)}종, 태그 {completer.tag_count:,}개")
    return 0


def main() -> int:
    # QApplication을 만들기 전에 처리해야 하는 인자들 (화면 없는 환경에서도 동작)
    if "--version" in sys.argv[1:]:
        print(__version__)
        return 0
    if "--selftest" in sys.argv[1:]:
        return selftest()

    settings = load_settings()
    configure_logging(log_dir(), debug=settings.debug_logging)
    enable_crash_log(log_dir())  # 세그폴트 등 네이티브 크래시 추적
    install_excepthook()
    _install_qt_message_handler()
    logger = logging.getLogger(__name__)
    logger.info("%s starting (debug logging: %s)", APP_NAME, settings.debug_logging)
    i18n = I18nManager(language=settings.language)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    session = NAISession()
    client = NAIClient(
        session,
        debug_headers=settings.debug_headers,
        forensics_dir=log_dir() / "forensics",
    )

    def validate_token(token: str) -> None:
        session.login_with_token(token)
        client.get_anlas()  # 실제 API 호출로 토큰 유효성 확인

    # 저장된 토큰으로 자동 로그인 시도, 실패 시 다이얼로그
    stored = credentials.load_credential(TOKEN_CREDENTIAL_KEY)
    logged_in = False
    if stored:
        try:
            validate_token(stored)
            logged_in = True
        except Exception as e:
            logger.warning("stored token rejected: %s", e)

    if not logged_in:
        dialog = LoginDialog(i18n, validate_token, initial_token=stored)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return 0
        if dialog.remember:
            credentials.save_credential(TOKEN_CREDENTIAL_KEY, dialog.token)
        else:
            credentials.delete_credential(TOKEN_CREDENTIAL_KEY)

    ensure_dirs(settings)
    bridge = QtEventBridge()
    service = build_service(client, settings, bridge)

    window = MainWindow(i18n, settings, client, service, bridge)
    window.show()

    exit_code = app.exec()

    service.shutdown()
    save_settings(window.collect_settings())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
