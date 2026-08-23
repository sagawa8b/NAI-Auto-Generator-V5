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


def _emit(text: str) -> None:
    """어떤 콘솔에서도 안전하게 한 줄 출력한다.

    Windows 러너의 기본 콘솔은 cp1252라 한글을 그대로 쓰면 UnicodeEncodeError가
    난다. 프로즌 GUI 빌드에서는 그 예외가 PyInstaller의 모달 오류 창을 띄워
    프로세스가 종료하지 않았다 (릴리스 워크플로가 3분 타임아웃에 걸렸다).
    windowed 빌드는 sys.stdout이 아예 없기도 하다.
    """
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")
    stream.flush()


def selftest() -> int:
    """프로즌 빌드가 실제로 쓸 수 있는 상태인지 확인한다 (릴리스 워크플로가 호출).

    PyInstaller로 묶으면 조용히 깨지기 쉬운 것들을 실제로 실행해 본다: 번들
    리소스(언어 파일 4종·내장 태그 DB), keyring 백엔드, 옵션 페이지 등록. 어느 하나라도
    빠지면 앱은 뜨지만 "자동완성이 안 되고 토큰이 저장 안 되고 옵션 창이 빈" 상태가 된다.
    셋 다 실제로 v0.1.0~v0.2.0 빌드에서 하나씩 터진 것들이다.
    """
    from ..core.settings import credentials
    from ..core.tag_completer import TagCompleter, bundled_database_path
    from .options_dialog import NAV_ORDER
    from .options_pages import registered_pages

    failures: list[str] = []

    languages = sorted(I18nManager().get_available_languages())
    if len(languages) < 4:
        failures.append(f"언어 리소스 부족: {languages}")

    completer = TagCompleter(bundled_database_path())
    if not completer.load() or completer.tag_count == 0:
        failures.append(f"내장 태그 DB 로드 실패: {bundled_database_path()}")

    if not credentials.is_available():
        failures.append("keyring 백엔드를 쓸 수 없다 (토큰이 저장되지 않는다)")

    pages = registered_pages()
    missing_pages = [key for key in NAV_ORDER if key not in pages]
    if missing_pages:
        failures.append(f"옵션 페이지 누락: {missing_pages} (옵션 창이 비어서 뜬다)")

    for line in failures:
        _emit(f"selftest FAIL: {line}")
    if failures:
        return 1
    _emit(
        f"selftest OK — v{__version__}, 언어 {len(languages)}종, "
        f"태그 {completer.tag_count:,}개, 옵션 페이지 {len(NAV_ORDER)}종"
    )
    return 0


def main() -> int:
    # QApplication을 만들기 전에 처리해야 하는 인자들 (화면 없는 환경에서도 동작)
    if "--version" in sys.argv[1:]:
        _emit(__version__)
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
        try:
            client.get_anlas()  # 실제 API 호출로 토큰 유효성 확인
        except Exception:
            session.logout()  # 못 쓰는 토큰이 남아 로그인된 것처럼 보이면 안 된다
            raise

    # 저장된 토큰으로 자동 로그인 시도, 실패하거나 없으면 다이얼로그
    stored = credentials.load_credential(credentials.TOKEN_KEY)
    if stored:
        try:
            validate_token(stored)
        except Exception as e:
            logger.warning("stored token rejected: %s", e)

    if not session.is_logged_in():
        dialog = LoginDialog(i18n, validate_token, initial_token=stored)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if dialog.remember:
                credentials.save_credential(credentials.TOKEN_KEY, dialog.token)
            else:
                credentials.delete_credential(credentials.TOKEN_KEY)
        # 취소해도 앱은 뜬다 (V4와 같다). 로그아웃 상태로 시작하고,
        # 파일 → 로그인(Ctrl+I)으로 언제든 로그인할 수 있다.

    ensure_dirs(settings)
    bridge = QtEventBridge()
    service = build_service(client, settings, bridge)

    window = MainWindow(i18n, settings, client, service, bridge)
    window.set_logged_in(session.is_logged_in())
    window.show()

    exit_code = app.exec()

    service.shutdown()
    save_settings(window.collect_settings())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
