"""NAI 프롬프트 어시스턴트 — WD 태거 + LM Studio를 한 창으로 통합.

세 모드를 라디오로 전환한다:

- **WD 태거**: 로컬 WD14 ONNX 모델로 이미지 → 단부루 태그.
- **LLM 태거**: LM Studio LLM으로 텍스트/이미지 → 자연어 프롬프트 또는 단부루 태그.
- **LLM 어시스턴트**: LM Studio VLM으로 이미지 + 텍스트 지시 → 지시대로 변형된 프롬프트.

입력은 **좌(텍스트) / 우(이미지) 나란히** 두고, 값이 있는 쪽만 쓴다 (빈 쪽은 무시).
결과는 편집 가능한 텍스트 하나로 통일한다 (WD의 체크박스 목록은 폐기). 기존
`wd14_dialog.py` / `lmstudio_dialog.py`를 이 파일이 대체한다.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image
from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
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
    QVBoxLayout,
    QWidget,
)

from ..core.llm.lmstudio_client import (
    LENGTH_LONG,
    LENGTH_MEDIUM,
    LENGTH_SHORT,
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
from ..core.wd14_tagger import WD14Error, threshold_for_length
from .widgets.hidpi_image import HiDpiImageLabel

if TYPE_CHECKING:
    from ..core.i18n.manager import I18nManager
    from ..core.wd14_tagger import WD14Tagger

logger = logging.getLogger(__name__)

_PREVIEW_SIZE = 200  # 미리보기 한 변 (논리 픽셀)
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
_WD_INFERENCE_TIMEOUT = 30  # WD14 추론 타임아웃 (초)

#: 모드 식별자 (설정·라디오와 공유).
MODE_WD = "wd_tagger"
MODE_LLM_TAGGER = "llm_tagger"
MODE_LLM_ASSISTANT = "llm_assistant"

#: LLM 예외 → 안내 문구 i18n 키.
_ERROR_KEYS: tuple[tuple[type[LMStudioError], str], ...] = (
    (LMStudioNotInstalled, "lmstudio.err_not_installed"),
    (LMStudioConnectionError, "lmstudio.err_connection"),
    (LMStudioNoModelError, "lmstudio.err_no_model"),
    (LMStudioVisionUnsupported, "lmstudio.err_vision"),
    (LMStudioTimeoutError, "lmstudio.err_timeout"),
    (LMStudioResponseError, "lmstudio.err_response"),
)


def _error_key(exc: LMStudioError) -> str:
    for exc_type, key in _ERROR_KEYS:
        if isinstance(exc, exc_type):
            return key
    return "lmstudio.err_response"


class _LLMWorker(QThread):
    """LM Studio 생성 1회 (태거/어시스턴트 공통). `cancel()`로 중단."""

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
        except Exception as e:  # noqa: BLE001
            logger.exception("LM Studio generation failed")
            self.failed.emit("lmstudio.err_response", str(e))
        else:
            self.finished_ok.emit(result)


class _WDWorker(QThread):
    """WD14 로컬 추론 1회. 예측 태그를 쉼표로 이은 문자열로 돌려준다."""

    finished_ok = Signal(object)  # PromptResult (prompt=태그 문자열)
    failed = Signal(str, str)  # (error_key, detail)
    cancelled = Signal()  # WD는 취소를 지원하지 않지만 인터페이스 통일용

    def __init__(
        self, tagger: WD14Tagger, image: bytes, threshold: float, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._tagger = tagger
        self._image = image
        self._threshold = threshold
        self._timed_out = False
        self._timer: threading.Timer | None = None

    def cancel(self) -> None:
        # WD 추론은 짧고 중단 지점이 없다 — 인터페이스만 맞춘다.
        pass

    def run(self) -> None:
        import io

        self._timer = threading.Timer(_WD_INFERENCE_TIMEOUT, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()
        try:
            image = Image.open(io.BytesIO(self._image))
            image.load()
            predictions = self._tagger.predict(image, threshold=self._threshold)
            if self._timed_out:
                return
            self._timer.cancel()
            tags = [p.tag for p in predictions]
            self.finished_ok.emit(PromptResult(prompt=", ".join(tags), negative_prompt="", raw=""))
        except WD14Error as e:
            if not self._timed_out:
                self._timer.cancel()
                self.failed.emit("wd14.error", str(e))
        except Exception as e:  # noqa: BLE001
            if not self._timed_out:
                self._timer.cancel()
                logger.exception("WD14 inference failed")
                self.failed.emit("wd14.error", str(e))

    def _on_timeout(self) -> None:
        self._timed_out = True
        self.failed.emit("wd14.error", "WD14 inference timed out (exceeded 30 seconds).")


class AssistantDialog(QDialog):
    """WD 태거 / LLM 태거 / LLM 어시스턴트를 한 창에서.

    Parameters
    ----------
    config : LMStudioConfig
        LM Studio 연결·타임아웃·시스템 프롬프트 (MainWindow가 설정에서 만든다).
    i18n : I18nManager
    wd_tagger_factory : Callable[[], WD14Tagger] | None
        WD 모드에서 호출해 태거를 만든다. None이면 WD 모드는 잠긴다 (모델/런타임 부재).
    wd_unavailable_reason : str
        WD를 쓸 수 없을 때 상태 문구로 보여줄 이유 (모델 없음/런타임 없음).
    default_apply_mode / default_style / default_mode : 기본 선택값.
    """

    #: (prompt, negative_prompt, mode) — mode ∈ {"append", "replace"}.
    prompt_ready = Signal(str, str, str)

    def __init__(
        self,
        config: LMStudioConfig,
        i18n: I18nManager,
        wd_tagger_factory=None,
        wd_unavailable_reason: str = "",
        default_apply_mode: str = "append",
        default_style: str = STYLE_NATURAL,
        default_mode: str = MODE_LLM_TAGGER,
        default_length: str = LENGTH_MEDIUM,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._i18n = i18n
        self._wd_tagger_factory = wd_tagger_factory
        self._wd_unavailable_reason = wd_unavailable_reason
        self._default_apply_mode = default_apply_mode
        self._default_style = default_style
        self._default_length = default_length
        self._generator = LMStudioPromptGenerator()
        self._worker: QThread | None = None
        self._image_bytes: bytes | None = None

        tr = i18n.get_text
        self.setWindowTitle(tr("assistant.title"))
        self.setMinimumSize(620, 680)

        self._build_ui(default_mode)
        # WD 모드가 아니면 창이 열릴 때 LM Studio 모델 목록을 채운다.
        if self._current_mode() != MODE_WD:
            self._refresh_models(initial=True)
        self._update_mode_controls()

    # ── UI ───────────────────────────────────────────────

    def _build_ui(self, default_mode: str) -> None:
        tr = self._i18n.get_text
        layout = QVBoxLayout(self)

        # 입력: 좌(텍스트) / 우(이미지) 나란히
        io_row = QHBoxLayout()

        text_col = QVBoxLayout()
        self._text_title = QLabel(tr("assistant.text_input"), self)
        text_col.addWidget(self._text_title)
        self._text_edit = QPlainTextEdit(self)
        self._text_edit.setPlaceholderText(tr("assistant.text_placeholder"))
        text_col.addWidget(self._text_edit, 1)
        io_row.addLayout(text_col, 1)

        image_col = QVBoxLayout()
        self._image_title = QLabel(tr("assistant.image_input"), self)
        image_col.addWidget(self._image_title)
        self._image_label = HiDpiImageLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumHeight(160)
        self._image_label.setStyleSheet(
            "QLabel { border: 2px dashed #888; border-radius: 8px; padding: 16px; }"
        )
        self._image_label.setText(tr("assistant.drop_here"))
        image_col.addWidget(self._image_label, 1)
        img_btn_row = QHBoxLayout()
        self._browse_btn = QPushButton(tr("assistant.browse"), self)
        self._browse_btn.clicked.connect(self._on_browse)
        self._clear_image_btn = QPushButton(tr("assistant.clear_image"), self)
        self._clear_image_btn.clicked.connect(self._clear_image)
        self._clear_image_btn.setEnabled(False)
        img_btn_row.addWidget(self._browse_btn)
        img_btn_row.addWidget(self._clear_image_btn)
        image_col.addLayout(img_btn_row)
        io_row.addLayout(image_col, 1)

        layout.addLayout(io_row, 1)
        self.setAcceptDrops(True)

        # 모드 라디오
        self._wd_radio = QRadioButton(tr("assistant.mode_wd"), self)
        self._llm_tagger_radio = QRadioButton(tr("assistant.mode_llm_tagger"), self)
        self._llm_assistant_radio = QRadioButton(tr("assistant.mode_llm_assistant"), self)
        self._mode_group = QButtonGroup(self)
        for radio in (self._wd_radio, self._llm_tagger_radio, self._llm_assistant_radio):
            self._mode_group.addButton(radio)
            layout.addWidget(radio)
        {
            MODE_WD: self._wd_radio,
            MODE_LLM_TAGGER: self._llm_tagger_radio,
            MODE_LLM_ASSISTANT: self._llm_assistant_radio,
        }.get(default_mode, self._llm_tagger_radio).setChecked(True)
        self._mode_group.buttonToggled.connect(self._on_mode_changed)

        # 모델 + 새로고침
        model_row = QHBoxLayout()
        self._model_label = QLabel(tr("assistant.model"), self)
        self._model_combo = QComboBox(self)
        self._refresh_btn = QPushButton(tr("assistant.refresh"), self)
        self._refresh_btn.clicked.connect(self._refresh_models)
        model_row.addWidget(self._model_label)
        model_row.addWidget(self._model_combo, 1)
        model_row.addWidget(self._refresh_btn)
        layout.addLayout(model_row)

        # 출력 스타일
        style_row = QHBoxLayout()
        self._style_label = QLabel(tr("assistant.style"), self)
        self._natural_radio = QRadioButton(tr("assistant.style_natural"), self)
        self._danbooru_radio = QRadioButton(tr("assistant.style_danbooru"), self)
        self._style_group = QButtonGroup(self)
        self._style_group.addButton(self._natural_radio)
        self._style_group.addButton(self._danbooru_radio)
        (self._danbooru_radio if self._default_style == STYLE_DANBOORU else self._natural_radio).setChecked(
            True
        )
        style_row.addWidget(self._style_label)
        style_row.addWidget(self._natural_radio)
        style_row.addWidget(self._danbooru_radio)
        style_row.addStretch(1)
        layout.addLayout(style_row)

        # 출력 길이 — 생성 버튼과 스타일 설정 사이. LLM은 max_tokens+지시로,
        # WD 태거는 일반 태그 임계값으로 번역된다.
        length_row = QHBoxLayout()
        self._length_label = QLabel(tr("assistant.length"), self)
        self._length_short_radio = QRadioButton(tr("assistant.length_short"), self)
        self._length_medium_radio = QRadioButton(tr("assistant.length_medium"), self)
        self._length_long_radio = QRadioButton(tr("assistant.length_long"), self)
        self._length_group = QButtonGroup(self)
        self._length_radios = {
            LENGTH_SHORT: self._length_short_radio,
            LENGTH_MEDIUM: self._length_medium_radio,
            LENGTH_LONG: self._length_long_radio,
        }
        for radio in self._length_radios.values():
            self._length_group.addButton(radio)
        self._length_radios.get(self._default_length, self._length_medium_radio).setChecked(True)
        length_row.addWidget(self._length_label)
        length_row.addWidget(self._length_short_radio)
        length_row.addWidget(self._length_medium_radio)
        length_row.addWidget(self._length_long_radio)
        length_row.addStretch(1)
        layout.addLayout(length_row)

        # 생성 / 중지
        gen_row = QHBoxLayout()
        self._generate_btn = QPushButton(tr("assistant.generate"), self)
        self._generate_btn.clicked.connect(self._on_generate)
        gen_row.addWidget(self._generate_btn)
        self._stop_btn = QPushButton(tr("assistant.stop"), self)
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.setEnabled(False)
        gen_row.addWidget(self._stop_btn)
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        gen_row.addWidget(self._progress, 1)
        layout.addLayout(gen_row)

        self._status_label = QLabel("", self)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        # 결과
        layout.addWidget(QLabel(tr("assistant.result"), self))
        self._result_edit = QPlainTextEdit(self)
        self._result_edit.setPlaceholderText(tr("assistant.result_placeholder"))
        layout.addWidget(self._result_edit, 1)

        self._negative_title = QLabel(tr("assistant.result_negative"), self)
        layout.addWidget(self._negative_title)
        self._negative_edit = QPlainTextEdit(self)
        self._negative_edit.setFixedHeight(64)
        layout.addWidget(self._negative_edit)

        # 반영 버튼
        apply_row = QHBoxLayout()
        self._append_btn = QPushButton(tr("assistant.apply_append"), self)
        self._append_btn.clicked.connect(lambda: self._apply("append"))
        self._replace_btn = QPushButton(tr("assistant.apply_replace"), self)
        self._replace_btn.clicked.connect(lambda: self._apply("replace"))
        self._copy_btn = QPushButton(tr("assistant.copy"), self)
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

    # ── 모드 ─────────────────────────────────────────────

    def _current_mode(self) -> str:
        if self._wd_radio.isChecked():
            return MODE_WD
        if self._llm_assistant_radio.isChecked():
            return MODE_LLM_ASSISTANT
        return MODE_LLM_TAGGER

    def _on_mode_changed(self, _button, checked: bool) -> None:
        if not checked:
            return  # buttonToggled는 켜짐/꺼짐 둘 다 온다 — 켜진 것만 처리
        mode = self._current_mode()
        # LLM 모드로 처음 들어오면 모델 목록이 비어 있을 수 있으니 채운다.
        if mode != MODE_WD and self._model_combo.count() == 0:
            self._refresh_models(initial=True)
        self._update_mode_controls()

    def _update_mode_controls(self) -> None:
        """모드에 따라 컨트롤 활성/표시와 버튼 라벨을 맞춘다."""
        tr = self._i18n.get_text
        mode = self._current_mode()
        is_wd = mode == MODE_WD
        is_llm = not is_wd

        # WD: 텍스트 무시(비활성), 모델·스타일 비활성, 네거티브 숨김.
        self._text_edit.setEnabled(is_llm)
        self._text_title.setEnabled(is_llm)
        self._model_label.setEnabled(is_llm)
        self._model_combo.setEnabled(is_llm)
        self._refresh_btn.setEnabled(is_llm)
        self._style_label.setEnabled(is_llm)
        self._natural_radio.setEnabled(is_llm)
        self._danbooru_radio.setEnabled(is_llm)
        self._negative_title.setVisible(is_llm)
        self._negative_edit.setVisible(is_llm)

        # 생성 버튼 라벨은 모드별로.
        label = {
            MODE_WD: "assistant.generate_wd",
            MODE_LLM_TAGGER: "assistant.generate",
            MODE_LLM_ASSISTANT: "assistant.generate_assistant",
        }[mode]
        self._generate_btn.setText(tr(label))

        # WD를 쓸 수 없으면 이유를 상태로 알리고 생성 잠금.
        if is_wd and self._wd_tagger_factory is None:
            self._status_label.setText(self._wd_unavailable_reason or tr("assistant.wd_unavailable"))
            self._generate_btn.setEnabled(False)
        else:
            self._generate_btn.setEnabled(True)
            self._status_label.setText("")

    # ── 이미지 입력 ──────────────────────────────────────

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
            self, tr("assistant.choose_image"), "", tr("assistant.image_filter")
        )
        if path:
            self._load_image(Path(path))

    def _load_image(self, path: Path) -> None:
        tr = self._i18n.get_text
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._status_label.setText(tr("assistant.image_open_failed", exc))
            return
        self._image_bytes = data
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            self._image_label.show_fitted(pixmap, QSize(_PREVIEW_SIZE, _PREVIEW_SIZE))
        else:
            self._image_label.setText(path.name)
        self._clear_image_btn.setEnabled(True)
        self._status_label.setText(tr("assistant.image_loaded", path.name))

    def _clear_image(self) -> None:
        self._image_bytes = None
        self._image_label.setText(self._i18n.get_text("assistant.drop_here"))
        self._clear_image_btn.setEnabled(False)

    # ── 모델 목록 ────────────────────────────────────────

    def _refresh_models(self, initial: bool = False) -> None:
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
        self._status_label.setText(
            tr("assistant.models_found", len(models)) if models else tr("assistant.no_models")
        )

    # ── 생성 ─────────────────────────────────────────────

    def _on_generate(self) -> None:
        tr = self._i18n.get_text
        mode = self._current_mode()
        text = self._text_edit.toPlainText().strip()
        image = self._image_bytes

        if mode == MODE_WD:
            if image is None:
                self._status_label.setText(tr("assistant.need_image"))
                return
            if self._wd_tagger_factory is None:
                self._status_label.setText(self._wd_unavailable_reason or tr("assistant.wd_unavailable"))
                return
            self._start_wd(image)
            return

        # LLM 태거 / 어시스턴트
        if mode == MODE_LLM_ASSISTANT and image is None:
            self._status_label.setText(tr("assistant.need_image_assistant"))
            return
        if not text and image is None:
            self._status_label.setText(tr("assistant.need_input"))
            return

        style = STYLE_DANBOORU if self._danbooru_radio.isChecked() else STYLE_NATURAL
        config = dataclasses.replace(
            self._config,
            style=style,
            length=self._selected_length(),
            model=self._model_combo.currentText().strip(),
            assistant=(mode == MODE_LLM_ASSISTANT),
        )
        self._set_busy(True)
        self._status_label.setText(tr("assistant.generating"))
        worker = _LLMWorker(self._generator, text, image, config, self)
        worker.finished_ok.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        self._worker = worker
        worker.start()

    def _start_wd(self, image: bytes) -> None:
        tr = self._i18n.get_text
        try:
            tagger = self._wd_tagger_factory()
        except Exception as e:  # noqa: BLE001
            logger.warning("WD14 tagger init failed: %s", e)
            self._status_label.setText(tr("assistant.wd_unavailable"))
            return
        self._set_busy(True)
        self._status_label.setText(tr("assistant.generating"))
        worker = _WDWorker(tagger, image, threshold_for_length(self._selected_length()), self)
        worker.finished_ok.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        self._worker = worker
        worker.start()

    def _selected_length(self) -> str:
        """현재 선택된 출력 길이 (short|medium|long)."""
        for length, radio in self._length_radios.items():
            if radio.isChecked():
                return length
        return LENGTH_MEDIUM

    def _on_stop(self) -> None:
        if self._worker is not None:
            self._status_label.setText(self._i18n.get_text("assistant.stopping"))
            self._stop_btn.setEnabled(False)
            self._worker.cancel()

    def _on_done(self, result: PromptResult) -> None:
        tr = self._i18n.get_text
        self._set_busy(False)
        self._worker = None
        self._result_edit.setPlainText(result.prompt)
        self._negative_edit.setPlainText(result.negative_prompt)
        has_prompt = bool(result.prompt.strip())
        for btn in (self._append_btn, self._replace_btn, self._copy_btn):
            btn.setEnabled(has_prompt)
        self._status_label.setText(tr("assistant.done") if has_prompt else tr("assistant.empty_result"))

    def _on_failed(self, error_key: str, detail: str) -> None:
        self._set_busy(False)
        self._worker = None
        self._status_label.setText(self._i18n.get_text(error_key, detail))
        logger.warning("assistant generation error [%s]: %s", error_key, detail)

    def _on_cancelled(self) -> None:
        self._set_busy(False)
        self._worker = None
        self._status_label.setText(self._i18n.get_text("assistant.stopped"))

    def _set_busy(self, busy: bool) -> None:
        self._progress.setVisible(busy)
        self._generate_btn.setEnabled(not busy)
        # WD 워커는 취소를 지원하지 않으니 그때는 중지 버튼을 열지 않는다.
        self._stop_btn.setEnabled(busy and isinstance(self._worker, _LLMWorker))
        self._browse_btn.setEnabled(not busy)
        self._refresh_btn.setEnabled(not busy and self._current_mode() != MODE_WD)
        self._model_combo.setEnabled(not busy and self._current_mode() != MODE_WD)
        for radio in (self._wd_radio, self._llm_tagger_radio, self._llm_assistant_radio):
            radio.setEnabled(not busy)
        for radio in self._length_radios.values():
            radio.setEnabled(not busy)
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
        self._status_label.setText(self._i18n.get_text("assistant.copied"))

    # ── 종료 정리 ────────────────────────────────────────

    def _shutdown_worker(self) -> None:
        worker = self._worker
        if worker is None:
            return
        if isinstance(worker, _LLMWorker):
            worker.cancel()
        worker.wait(6000)
        self._worker = None

    def reject(self) -> None:
        self._shutdown_worker()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._shutdown_worker()
        super().closeEvent(event)


__all__ = ["MODE_LLM_ASSISTANT", "MODE_LLM_TAGGER", "MODE_WD", "AssistantDialog"]
