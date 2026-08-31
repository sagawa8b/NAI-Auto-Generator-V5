"""컴포지션 루트 — 프로젝트에서 유일하게 전역 조립을 하는 곳.

설정 로드 → i18n → 세션/클라이언트 → 로그인 → 서비스 → 메인 윈도우.
import-time 부작용 없음: 모든 초기화는 main() 안에서만 일어난다.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

import platformdirs
from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtGui import QIcon
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


def app_icon_path() -> Path:
    """번들 앱 아이콘 경로 — 언어 파일·태그 DB와 같은 규칙(`naiauto/resources/`)을 따른다."""
    return Path(__file__).resolve().parent.parent / "resources" / "icons" / "app_icon.ico"


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

    if not app_icon_path().is_file():
        failures.append(f"앱 아이콘 리소스 누락: {app_icon_path()} (작업 표시줄 아이콘이 빠진다)")

    if not credentials.is_available():
        failures.append("keyring 백엔드를 쓸 수 없다 (토큰이 저장되지 않는다)")

    # WD14 자동 태깅은 onnxruntime + numpy가 번들에 들어가야 돌아간다. 빠져도 앱은
    # 뜨지만 "모델을 쓸 수 없음"으로만 보여서, 배포 뒤에야 드러났다 (v0.6.5).
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError as e:
        failures.append(f"WD14 태깅을 쓸 수 없다: {e}")

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
        f"태그 {completer.tag_count:,}개, 옵션 페이지 {len(NAV_ORDER)}종, WD14 준비됨"
    )
    return 0


def smoketest() -> int:
    """프로즌 빌드에서 창이 실제로 뜨는지 (릴리스 워크플로가 --selftest 다음에 호출).

    `--selftest`는 리소스와 등록 상태만 본다. 창을 세우는 도중 죽는 실수는 잡지 못했다 —
    v0.2.1이 프로퍼티를 함수처럼 불러 시작하자마자 TypeError로 죽은 채 릴리스됐다.
    여기서는 로그아웃 상태로 메인 윈도우를 실제로 만들고 이벤트 루프를 한 바퀴 돌린다.

    화면이 없는 러너에서 돌아야 하므로 offscreen 플랫폼을 강제한다.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from .options_dialog import NAV_ORDER, OptionsDialog

    settings = AppSettings()
    settings.check_updates_on_start = False  # 네트워크 없이도 끝나야 한다

    qt_app = QApplication.instance() or QApplication([])
    session = NAISession()
    client = NAIClient(session)
    bridge = QtEventBridge()
    service = build_service(client, settings, bridge)

    window = MainWindow(I18nManager(language="ko"), settings, client, service, bridge)
    window.show()
    qt_app.processEvents()

    failures: list[str] = []
    if window.once_button.isEnabled():
        failures.append("로그아웃 상태인데 생성 버튼이 열려 있다")
    if not window.login_action.isEnabled():
        failures.append("로그인 메뉴가 비활성이다 (로그인할 방법이 없다)")

    dialog = OptionsDialog(window._i18n, settings, parent=window)
    if dialog._nav.count() != len(NAV_ORDER):
        failures.append(f"옵션 창 항목 {dialog._nav.count()}개 (기대 {len(NAV_ORDER)}개)")
    dialog.close()

    window.close()
    service.shutdown()

    for line in failures:
        _emit(f"smoketest FAIL: {line}")
    if failures:
        return 1
    _emit(f"smoketest OK — 창 생성, 옵션 {len(NAV_ORDER)}종, 로그아웃 상태 반영")
    return 0


def ensure_login(
    session: NAISession,
    i18n: I18nManager,
    validate: Callable[[str], None],
) -> None:
    """시작 시 로그인 — 저장된 토큰을 먼저 써 보고, 안 되면 다이얼로그를 띄운다.

    `main()`에서 떼어낸 이유: 시작 경로가 테스트되지 않아 프로퍼티를 함수처럼 부르는
    실수가 그대로 릴리스까지 나갔다 (v0.2.1의 `TypeError: 'bool' object is not callable`).

    취소해도 예외를 내지 않는다 — 앱은 로그아웃 상태로 뜨고, 파일 → 로그인(Ctrl+I)으로
    언제든 로그인할 수 있다 (V4와 같다).
    """
    stored = credentials.load_credential(credentials.TOKEN_KEY)
    if stored:
        try:
            validate(stored)
        except Exception as e:
            logging.getLogger(__name__).warning("stored token rejected: %s", e)

    if session.is_logged_in:
        return

    dialog = LoginDialog(i18n, validate, initial_token=stored)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    if dialog.remember:
        credentials.save_credential(credentials.TOKEN_KEY, dialog.token)
    else:
        credentials.delete_credential(credentials.TOKEN_KEY)


def main() -> int:
    # QApplication을 만들기 전에 처리해야 하는 인자들 (화면 없는 환경에서도 동작)
    if "--version" in sys.argv[1:]:
        _emit(__version__)
        return 0
    if "--selftest" in sys.argv[1:]:
        return selftest()
    if "--smoketest" in sys.argv[1:]:
        return smoketest()

    settings = load_settings()
    configure_logging(log_dir(), debug=settings.debug_logging)
    enable_crash_log(log_dir())  # 세그폴트 등 네이티브 크래시 추적
    install_excepthook()
    _install_qt_message_handler()
    logger = logging.getLogger(__name__)
    logger.info("%s starting (debug logging: %s)", APP_NAME, settings.debug_logging)
    i18n = I18nManager(language=settings.language)

    # Windows 작업 표시줄에서 python.exe/제네릭 아이콘 대신 앱 고유 아이콘이 표시되도록 설정
    # (V4의 gui.py와 동일한 방식). AppUserModelID가 없으면 프로즌 빌드가 아닌 개발 실행에서
    # 작업 표시줄 아이콘이 파이썬 인터프리터 것으로 그룹핑된다.
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("sagawa8b.NAIAutoV5")
        except OSError:
            logging.getLogger(__name__).warning("failed to set AppUserModelID", exc_info=True)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(app_icon_path())))

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

    ensure_login(session, i18n, validate_token)

    ensure_dirs(settings)
    bridge = QtEventBridge()
    service = build_service(client, settings, bridge)

    window = MainWindow(i18n, settings, client, service, bridge)
    window.set_logged_in(session.is_logged_in)
    window.show()

    exit_code = app.exec()

    service.shutdown()
    save_settings(window.collect_settings())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
