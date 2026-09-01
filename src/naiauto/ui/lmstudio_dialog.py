"""자연어 프롬프트 생성 다이얼로그 — LM Studio 로컬 LLM 연동.

입력(단어/문장 + 선택적 이미지) → 백그라운드 스레드에서 `respond` → 편집 가능한
프롬프트 미리보기 → 프롬프트/네거티브 칸에 반영. WD14 자동 태깅 다이얼로그
(`ui/wd14_dialog.py`)와 같은 구조지만, 결과가 태그 목록이 아니라 한 덩어리의
프롬프트 문자열이라 체크박스 대신 편집기다.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.llm.lmstudio_client import (
    STYLE_DANBOORU,
    STYLE_NATURAL,
    LMStudioCancelled,
    LMStudioConfig,
    LMStudioConnectionError,
    LMStudioError,
    LMStudioNoModelError,
    LMStudioNotInstalled,
    LMStudioPromptGenerator,
    LMStudioResponseError,
    LMStudioTimeoutError,
    LMStudioVisionUnsupported,
    PromptResult,
)
from .widgets.hidpi_image import HiDpiImageLabel

if TYPE_CHECKING:
    from ..core.i18n.manager import I18nManager

logger = logging.getLogger(__name__)

_PREVIEW_SIZE = 200  # 미리보기 한 변 (논리 픽셀)
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
#: 탭 인덱스 — 입력을 활성 탭 하나로만 결정할 때 쓴다 (텍스트=0, 이미지=1).
_TEXT_TAB_INDEX = 0
_IMAGE_TAB_INDEX = 1

#: SDK 예외 → 안내 문구 i18n 키. 유형별로 사용자가 할 일이 다르다.
_ERROR_KEYS: tuple[tuple[type[LMStudioError], str], ...] = (
    (LMStudioNotInstalled, "lmstudio.err_not_installed"),
    (LMStudioConnectionError, "lmstudio.err_connection"),
    (LMStudioNoModelError, "lmstudio.err_no_model"),
    (LMStudioVisionUnsupported, "lmstudio.err_vision"),
    (LMStudioTimeoutError, "lmstudio.err_timeout"),
    (LMStudioResponseError, "lmstudio.err_response"),
)


class _GenerateWorker(QThread):
    """LM Studio 생성 1회를 백그라운드에서 돌린다 (타임아웃은 SDK 내장).

    `cancel()`로 중단을 요청하면 코어가 스트림을 멈추고 서버에 취소를 보낸다.
    """

    finished_ok = Signal(object)  # PromptResult
    failed = Signal(str, str)  # (error_key, detail)
    cancelled = Signal()

    def __init__(
        self,
        generator: LMStudioPromptGenerator,
        text: str,
        image: bytes | None,
        config: LMStudioConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._generator = generator
        self._text = text
        self._image = image
        self._config = config
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """생성 중단 요청 (Stop 버튼). 다음 스트림 조각에서 멈춘다."""
        self._cancel.set()

    def run(self) -> None:
        try:
            result = self._generator.generate(
                self._text, self._image, self._config, should_cancel=self._cancel.is_set
            )
        except LMStudioCancelled:
            self.cancelled.emit()
        except LMStudioError as e:
            self.failed.emit(_error_key(e), str(e))
        except Exception as e:  # noqa: BLE001 - 워커 예외가 조용히 사라지면 안 된다
            logger.exception("LM Studio generation failed")
            self.failed.emit("lmstudio.err_response", str(e))
        else:
            self.finished_ok.emit(result)


def _error_key(exc: LMStudioError) -> str:
    for exc_type, key in _ERROR_KEYS:
        if isinstance(exc, exc_type):
            return key
    return "lmstudio.err_response"


class LMStudioDialog(QDialog):
    """자연어 프롬프트 생성 다이얼로그.

    Parameters
    ----------
    config : LMStudioConfig
        연결·모델·타임아웃·시스템 프롬프트 (MainWindow가 설정에서 만들어 넘긴다).
    i18n : I18nManager
    parent : QWidget | None
    """

    #: (prompt, negative_prompt, mode) — mode ∈ {"append", "replace"}.
    prompt_ready = Signal(str, str, str)

    def __init__(
        self,
        config: LMStudioConfig,
        i18n: I18nManager,
        default_apply_mode: str = "append",
        default_style: str = STYLE_NATURAL,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._i18n = i18n
        self._default_apply_mode = default_apply_mode
        self._default_style = default_style
        self._generator = LMStudioPromptGenerator()
        self._worker: _GenerateWorker | None = None
        self._image_bytes: bytes | None = None
        self._result: PromptResult | None = None

        tr = i18n.get_text
        self.setWindowTitle(tr("lmstudio.title"))
        self.setMinimumSize(560, 620)

        self._build_ui()
        # 창이 열리면 서버에 로드된 모델 목록을 곧바로 채운다 (연결이 안 되면 상태 문구로 알린다).
        self._refresh_models(initial=True)

    # ── UI ───────────────────────────────────────────────

    def _build_ui(self) -> None:
        tr = self._i18n.get_text
        layout = QVBoxLayout(self)

        # 입력: 텍스트 탭 / 이미지 탭
        self._tabs = QTabWidget(self)

        self._text_edit = QPlainTextEdit(self)
        self._text_edit.setPlaceholderText(tr("lmstudio.text_placeholder"))
        self._tabs.addTab(self._text_edit, tr("lmstudio.tab_text"))

        image_tab = QWidget(self)
        image_layout = QVBoxLayout(image_tab)
        self._image_label = HiDpiImageLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumHeight(160)
        self._image_label.setStyleSheet(
            "QLabel { border: 2px dashed #888; border-radius: 8px; padding: 16px; }"
        )
        self._image_label.setText(tr("lmstudio.drop_here"))
        image_layout.addWidget(self._image_label, 1)
        browse_row = QHBoxLayout()
        self._browse_btn = QPushButton(tr("lmstudio.browse"), self)
        self._browse_btn.clicked.connect(self._on_browse)
        self._clear_image_btn = QPushButton(tr("lmstudio.clear_image"), self)
        self._clear_image_btn.clicked.connect(self._clear_image)
        self._clear_image_btn.setEnabled(False)
        browse_row.addStretch()
        browse_row.addWidget(self._browse_btn)
        browse_row.addWidget(self._clear_image_btn)
        browse_row.addStretch()
        image_layout.addLayout(browse_row)
        self._tabs.addTab(image_tab, tr("lmstudio.tab_image"))
        layout.addWidget(self._tabs)

        self.setAcceptDrops(True)

        # 모델: 서버에 지금 로드된 것만 보여 준다. NAI에 저장하지 않으므로 서버가
        # 우리 때문에 새 모델을 로드하는 일이 없다 (복수 모델 로딩 방지).
        model_row = QHBoxLayout()
        self._model_label = QLabel(tr("lmstudio.model"), self)
        self._model_combo = QComboBox(self)
        self._refresh_btn = QPushButton(tr("lmstudio.refresh"), self)
        self._refresh_btn.clicked.connect(self._refresh_models)
        model_row.addWidget(self._model_label)
        model_row.addWidget(self._model_combo, 1)
        model_row.addWidget(self._refresh_btn)
        layout.addLayout(model_row)

        # 출력 스타일: 자연어 문장 vs 단부루 태그. 모델이 두 형태를 오가는 편차를 없앤다.
        style_row = QHBoxLayout()
        self._style_label = QLabel(tr("lmstudio.style"), self)
        self._natural_radio = QRadioButton(tr("lmstudio.style_natural"), self)
        self._danbooru_radio = QRadioButton(tr("lmstudio.style_danbooru"), self)
        if self._default_style == STYLE_DANBOORU:
            self._danbooru_radio.setChecked(True)
        else:
            self._natural_radio.setChecked(True)
        style_row.addWidget(self._style_label)
        style_row.addWidget(self._natural_radio)
        style_row.addWidget(self._danbooru_radio)
        style_row.addStretch(1)
        layout.addLayout(style_row)

        # 생성 / 중지 버튼 + 진행 표시
        gen_row = QHBoxLayout()
        self._generate_btn = QPushButton(tr("lmstudio.generate"), self)
        self._generate_btn.clicked.connect(self._on_generate)
        gen_row.addWidget(self._generate_btn)
        self._stop_btn = QPushButton(tr("lmstudio.stop"), self)
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.setEnabled(False)
        gen_row.addWidget(self._stop_btn)
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setVisible(False)
        gen_row.addWidget(self._progress, 1)
        layout.addLayout(gen_row)

        self._status_label = QLabel("", self)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        # 결과 (편집 가능)
        layout.addWidget(QLabel(tr("lmstudio.result"), self))
        self._result_edit = QPlainTextEdit(self)
        self._result_edit.setPlaceholderText(tr("lmstudio.result_placeholder"))
        layout.addWidget(self._result_edit, 1)

        layout.addWidget(QLabel(tr("lmstudio.result_negative"), self))
        self._negative_edit = QPlainTextEdit(self)
        self._negative_edit.setFixedHeight(70)
        layout.addWidget(self._negative_edit)

        # 반영 버튼들
        apply_row = QHBoxLayout()
        self._append_btn = QPushButton(tr("lmstudio.apply_append"), self)
        self._append_btn.clicked.connect(lambda: self._apply("append"))
        self._replace_btn = QPushButton(tr("lmstudio.apply_replace"), self)
        self._replace_btn.clicked.connect(lambda: self._apply("replace"))
        self._copy_btn = QPushButton(tr("lmstudio.copy"), self)
        self._copy_btn.clicked.connect(self._on_copy)
        for btn in (self._append_btn, self._replace_btn, self._copy_btn):
            btn.setEnabled(False)
        apply_row.addWidget(self._append_btn)
        apply_row.addWidget(self._replace_btn)
        apply_row.addStretch()
        apply_row.addWidget(self._copy_btn)
        layout.addLayout(apply_row)

        self._button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self._button_box.rejected.connect(self.reject)
        self._button_box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.reject)
        layout.addWidget(self._button_box)

    # ── 이미지 입력 (WD14 다이얼로그와 같은 절차) ─────────

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in _IMAGE_SUFFIXES:
                    event.acceptProposedAction()
                    return

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    path = Path(url.toLocalFile())
                    if path.suffix.lower() in _IMAGE_SUFFIXES:
                        self._load_image(path)
                        event.acceptProposedAction()
                        return

    def _on_browse(self) -> None:
        tr = self._i18n.get_text
        path, _ = QFileDialog.getOpenFileName(
            self, tr("lmstudio.choose_image"), "", tr("lmstudio.image_filter")
        )
        if path:
            self._load_image(Path(path))

    def _load_image(self, path: Path) -> None:
        tr = self._i18n.get_text
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._status_label.setText(tr("lmstudio.image_open_failed", exc))
            return
        self._image_bytes = data
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            self._image_label.show_fitted(pixmap, QSize(_PREVIEW_SIZE, _PREVIEW_SIZE))
        else:
            self._image_label.setText(path.name)
        self._clear_image_btn.setEnabled(True)
        self._tabs.setCurrentIndex(_IMAGE_TAB_INDEX)
        self._status_label.setText(tr("lmstudio.image_loaded", path.name))

    def _clear_image(self) -> None:
        self._image_bytes = None
        self._image_label.setText(self._i18n.get_text("lmstudio.drop_here"))
        self._clear_image_btn.setEnabled(False)

    # ── 모델 목록 ────────────────────────────────────────

    def _refresh_models(self, initial: bool = False) -> None:
        """서버에 로드된 모델로 콤보를 다시 채운다. 붙지 못하면 상태 문구로 알린다.

        모델을 NAI에 저장하지 않는다 — 여기 보이는 것은 서버가 이미 로드해 둔 목록뿐이고,
        생성 시 그중 고른 것을 쓴다. 처음 열 때(`initial`)는 설정에 남은 모델명을 우선
        선택해 준다.
        """
        tr = self._i18n.get_text
        try:
            models = self._generator.list_models(self._config.host)
        except LMStudioError as e:
            self._status_label.setText(tr(_error_key(e), str(e)))
            return

        prefer = self._config.model if initial else self._model_combo.currentText()
        self._model_combo.clear()
        self._model_combo.addItems(models)
        if prefer:
            idx = self._model_combo.findText(prefer)
            if idx >= 0:
                self._model_combo.setCurrentIndex(idx)
        if models:
            self._status_label.setText(tr("lmstudio.models_found", len(models)))
        else:
            self._status_label.setText(tr("lmstudio.no_models"))

    # ── 생성 ─────────────────────────────────────────────

    def _on_generate(self) -> None:
        tr = self._i18n.get_text
        # 입력은 **현재 활성 탭 하나만** 쓴다 — 다른 탭에 남은 값이 섞이지 않도록
        # (텍스트 탭이면 이미지 무시, 이미지 탭이면 텍스트 무시).
        on_image_tab = self._tabs.currentIndex() == _IMAGE_TAB_INDEX
        if on_image_tab:
            text, image = "", self._image_bytes
        else:
            text, image = self._text_edit.toPlainText().strip(), None

        if not text and image is None:
            self._status_label.setText(tr("lmstudio.need_input"))
            return

        self._set_busy(True)
        self._status_label.setText(tr("lmstudio.generating"))
        style = STYLE_DANBOORU if self._danbooru_radio.isChecked() else STYLE_NATURAL
        model = self._model_combo.currentText().strip()
        config = dataclasses.replace(self._config, style=style, model=model)
        self._worker = _GenerateWorker(self._generator, text, image, config, self)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _on_stop(self) -> None:
        """진행 중인 생성 중단 요청. 서버에도 취소가 전달된다."""
        if self._worker is not None:
            self._status_label.setText(self._i18n.get_text("lmstudio.stopping"))
            self._stop_btn.setEnabled(False)
            self._worker.cancel()

    def _on_done(self, result: PromptResult) -> None:
        tr = self._i18n.get_text
        self._set_busy(False)
        self._worker = None
        self._result = result
        self._result_edit.setPlainText(result.prompt)
        self._negative_edit.setPlainText(result.negative_prompt)
        has_prompt = bool(result.prompt.strip())
        for btn in (self._append_btn, self._replace_btn, self._copy_btn):
            btn.setEnabled(has_prompt)
        self._status_label.setText(tr("lmstudio.done") if has_prompt else tr("lmstudio.empty_result"))

    def _on_failed(self, error_key: str, detail: str) -> None:
        self._set_busy(False)
        self._worker = None
        self._status_label.setText(self._i18n.get_text(error_key, detail))
        logger.warning("LM Studio generation error [%s]: %s", error_key, detail)

    def _on_cancelled(self) -> None:
        self._set_busy(False)
        self._worker = None
        self._status_label.setText(self._i18n.get_text("lmstudio.stopped"))

    def _set_busy(self, busy: bool) -> None:
        self._progress.setVisible(busy)
        self._generate_btn.setEnabled(not busy)
        self._stop_btn.setEnabled(busy)
        self._browse_btn.setEnabled(not busy)
        self._refresh_btn.setEnabled(not busy)
        self._model_combo.setEnabled(not busy)
        self.setAcceptDrops(not busy)

    # ── 반영 / 복사 ──────────────────────────────────────

    def _apply(self, mode: str) -> None:
        prompt = self._result_edit.toPlainText().strip()
        negative = self._negative_edit.toPlainText().strip()
        if not prompt and not negative:
            return
        self.prompt_ready.emit(prompt, negative, mode)
        self.accept()

    def _on_copy(self) -> None:
        prompt = self._result_edit.toPlainText().strip()
        if not prompt:
            return
        QGuiApplication.clipboard().setText(prompt)
        self._status_label.setText(self._i18n.get_text("lmstudio.copied"))

    # ── 종료 정리 ────────────────────────────────────────

    def _shutdown_worker(self) -> None:
        """진행 중인 워커에 취소를 보내고 끝날 때까지 기다린다.

        창을 닫을 때 워커가 살아 있으면 스레드가 죽은 위젯을 만지거나 웹소켓이
        어정쩡하게 남는다. 취소 → wait로 깔끔히 정리한다 (코어가 close도 한다)."""
        worker = self._worker
        if worker is None:
            return
        worker.cancel()
        worker.wait(5000)
        self._worker = None

    def reject(self) -> None:
        self._shutdown_worker()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 이름
        self._shutdown_worker()
        super().closeEvent(event)
