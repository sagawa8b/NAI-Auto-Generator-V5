"""Enhance(강화 업스케일) 입력 위젯 — 원본 이미지 + 배율 + 강도.

V5 웹 UI의 Enhance 대화상자와 같은 구성이다: 원본 한 장, Upscale Amount(1x / 1.5x /
Max), Magnitude(우리는 strength/noise를 그대로 노출한다 — 웹 UI의 Magnitude 2가
strength 0.5 / noise 0 이었다).

배율마다 요청이 어떻게 달라지는지는 `core/enhance.py`가 전부 계산한다. 이 위젯은
고른 값과 원본 크기를 넘기고, 돌려받은 계획(EnhancePlan)을 문구로 보여줄 뿐이다.

폴더를 고르면 그 안의 NAI 이미지를 훑어 대기열을 만든다 (V4.5의 벌크 강화). 실제
순환은 메인 윈도우가 `GenerationJob.request_provider`로 돌린다 — 이미지마다 크기도
프롬프트도 달라서, 한 요청을 반복하는 일반 연속 생성과는 다른 경로가 필요하다.
"""

from __future__ import annotations

import io
import os

from PIL import Image
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ...core.enhance import (
    DEFAULT_NOISE,
    DEFAULT_STRENGTH,
    STANDARD_15X_SIZES,
    UPSCALE_AMOUNTS,
    EnhancePlan,
    EnhanceSource,
    UpscaleAmount,
    available_amounts,
    plan_enhance,
    unavailable_reason,
)
from ...core.i18n.manager import I18nManager
from ...core.metadata.naiinfo import read_metadata
from ...core.metadata.reuse import ReusableSettings, extract_reusable
from .hidpi_image import HiDpiImageLabel

THUMB_HEIGHT = 150

#: 폴더 강화에서 훑을 확장자.
SUPPORTED_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")


class EnhancePanel(QGroupBox):
    """강화할 원본과 배율을 보관하고 UI로 노출한다."""

    changed = Signal()
    #: 폴더 대기열로 강화를 시작해 달라는 요청 (메인 윈도우가 잡을 만든다).
    folder_requested = Signal()
    #: 원본을 새로 불러왔다 — 메인 윈도우가 그 이미지의 설정을 위젯에 반영할 기회.
    source_loaded = Signal()

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._image: bytes | None = None
        self._size: tuple[int, int] | None = None
        self._settings: ReusableSettings | None = None
        self._sources: tuple[EnhanceSource, ...] = ()
        self._last_result: str | None = None
        self._busy = False  # 생성 중에는 폴더 강화를 시작할 수 없다
        self._syncing = False  # _refresh가 라디오를 바꿀 때 changed를 되쏘지 않도록
        # i2i 패널과 같은 규칙: 꺼져 있으면 이미지를 들고 있어도 요청에 반영하지 않는다.
        self._active = False

        layout = QVBoxLayout(self)

        self.thumbnail = HiDpiImageLabel()
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setMinimumHeight(THUMB_HEIGHT)
        layout.addWidget(self.thumbnail)

        self.plan_label = QLabel()
        self.plan_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.plan_label)

        # 고를 수 없는 배율이 있으면 왜인지 말해 준다 — 잠긴 버튼만 보여 주면
        # 사용자가 이유를 알 길이 없다.
        self.note_label = QLabel()
        self.note_label.setWordWrap(True)
        self.note_label.setStyleSheet("color: palette(link-visited);")
        self.note_label.setVisible(False)
        layout.addWidget(self.note_label)

        image_buttons = QHBoxLayout()
        self.choose_button = QPushButton()
        self.choose_button.clicked.connect(self.choose_image)
        self.use_last_button = QPushButton()
        self.use_last_button.clicked.connect(self.use_last_result)
        self.clear_button = QPushButton()
        self.clear_button.clicked.connect(self.clear_image)
        image_buttons.addWidget(self.choose_button)
        image_buttons.addWidget(self.use_last_button)
        image_buttons.addWidget(self.clear_button)
        layout.addLayout(image_buttons)

        self.amount_label = QLabel()
        amount_row = QHBoxLayout()
        amount_row.addWidget(self.amount_label)
        self.amount_group = QButtonGroup(self)
        self.amount_buttons: dict[UpscaleAmount, QRadioButton] = {}
        for index, amount in enumerate(UPSCALE_AMOUNTS):
            button = QRadioButton()
            self.amount_group.addButton(button, index)
            self.amount_buttons[amount] = button
            amount_row.addWidget(button)
        amount_row.addStretch(1)
        self.amount_buttons["1.5x"].setChecked(True)
        self.amount_group.idToggled.connect(lambda _id, on: on and self._on_changed())
        layout.addLayout(amount_row)

        form = QFormLayout()
        self.strength_spin = QDoubleSpinBox()
        self.strength_spin.setRange(0.01, 1.0)
        self.strength_spin.setSingleStep(0.05)
        self.strength_spin.setValue(DEFAULT_STRENGTH)
        self.noise_spin = QDoubleSpinBox()
        self.noise_spin.setRange(0.0, 0.99)
        self.noise_spin.setSingleStep(0.01)
        self.noise_spin.setValue(DEFAULT_NOISE)
        self.strength_label = QLabel()
        self.noise_label = QLabel()
        form.addRow(self.strength_label, self.strength_spin)
        form.addRow(self.noise_label, self.noise_spin)
        layout.addLayout(form)

        self.use_metadata_check = QCheckBox()
        self.use_metadata_check.setChecked(True)
        layout.addWidget(self.use_metadata_check)

        folder_row = QHBoxLayout()
        self.folder_button = QPushButton()
        self.folder_button.clicked.connect(self.choose_folder)
        self.folder_start_button = QPushButton()
        self.folder_start_button.clicked.connect(lambda: self.folder_requested.emit())
        self.folder_clear_button = QPushButton()
        self.folder_clear_button.clicked.connect(self.clear_folder)
        folder_row.addWidget(self.folder_button)
        folder_row.addWidget(self.folder_start_button)
        folder_row.addWidget(self.folder_clear_button)
        layout.addLayout(folder_row)

        self.folder_label = QLabel()
        self.folder_label.setStyleSheet("color: palette(mid);")
        self.folder_label.setWordWrap(True)
        layout.addWidget(self.folder_label)

        self._refresh()

    # ── 상태 ─────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        """보기 메뉴 토글. 꺼지면 화면에서 숨기고 요청에도 반영하지 않는다."""
        self._active = active
        self.setVisible(active)
        self.changed.emit()

    @property
    def image_bytes(self) -> bytes | None:
        return self._image if self._active else None

    @property
    def source_size(self) -> tuple[int, int] | None:
        return self._size if self._active else None

    @property
    def metadata_settings(self) -> ReusableSettings | None:
        """현재 원본에서 읽은 생성 설정 (없으면 None)."""
        return self._settings

    @property
    def sources(self) -> tuple[EnhanceSource, ...]:
        """폴더 강화 대기열 (패널이 꺼져 있으면 비어 있다)."""
        return self._sources if self._active else ()

    @property
    def amount(self) -> UpscaleAmount:
        for amount, button in self.amount_buttons.items():
            if button.isChecked():
                return amount
        return "1.5x"

    @property
    def strength(self) -> float:
        return self.strength_spin.value()

    @property
    def noise(self) -> float:
        return self.noise_spin.value()

    @property
    def use_metadata(self) -> bool:
        return self.use_metadata_check.isChecked()

    def plan(self) -> EnhancePlan | None:
        """현재 원본과 배율의 계획. 원본이 없거나 패널이 꺼져 있으면 None."""
        size = self.source_size
        return None if size is None else plan_enhance(size, self.amount)

    def diffusion_size(self) -> tuple[int, int] | None:
        """생성이 실제로 돌 해상도 (해상도 패널을 잠그는 값)."""
        plan = self.plan()
        return None if plan is None else plan.diffusion_size

    def is_ready(self) -> bool:
        return self.image_bytes is not None

    # ── 조작 ─────────────────────────────────────────────

    def choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, self._i18n.get_text("enhance.choose"), "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.load_image(path)

    def load_image(self, path: str) -> bool:
        """원본을 읽어 들이고 메타데이터도 함께 챙긴다. 실패하면 상태를 바꾸지 않는다."""
        tr = self._i18n.get_text
        try:
            with open(path, "rb") as f:
                data = f.read()
            with Image.open(io.BytesIO(data)) as img:
                size = img.size
                if img.format != "PNG":  # API는 PNG를 기대하므로 변환해 둔다
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="PNG")
                    data = buf.getvalue()
        except Exception as e:
            QMessageBox.warning(self, tr("errors.title"), tr("enhance.load_failed", e))
            return False

        self._image = data
        self._size = size
        self._settings = _reusable_from(path)
        self._refresh()
        self.changed.emit()
        self.source_loaded.emit()
        return True

    def clear_image(self) -> None:
        self._image = None
        self._size = None
        self._settings = None
        self._refresh()
        self.changed.emit()

    def choose_folder(self) -> None:
        """폴더 안의 이미지를 훑어 대기열을 만든다. 다시 고르면 대기열을 갈아 끼운다."""
        tr = self._i18n.get_text
        path = QFileDialog.getExistingDirectory(self, tr("enhance.folder"))
        if not path:
            return
        sources = scan_folder(path)
        self._sources = sources
        if not sources:
            QMessageBox.information(self, tr("errors.warning"), tr("enhance.folder_empty"))
        self._refresh()
        self.changed.emit()

    def set_busy(self, busy: bool) -> None:
        """생성이 도는 동안 폴더 강화 시작 버튼을 잠근다 (동시 실행은 서비스가 거부한다)."""
        self._busy = busy
        self._refresh()

    def set_last_result(self, path: str | None) -> None:
        """방금 생성한 이미지 경로 — "최근 결과" 버튼이 이걸 불러온다."""
        self._last_result = path
        self._refresh()

    def use_last_result(self) -> bool:
        """마지막으로 생성한 이미지를 원본으로 가져온다 (V4.5의 "현재 이미지 사용")."""
        if not self._last_result:
            return False
        return self.load_image(self._last_result)

    def clear_folder(self) -> None:
        self._sources = ()
        self._refresh()
        self.changed.emit()

    def _on_changed(self) -> None:
        if self._syncing:
            return
        self._refresh()
        self.changed.emit()

    # ── 표시 ─────────────────────────────────────────────

    def _refresh(self) -> None:
        tr = self._i18n.get_text
        has_image = self._image is not None
        self._sync_available_amounts()
        self.clear_button.setEnabled(has_image)
        self.use_last_button.setEnabled(bool(self._last_result))
        self.folder_start_button.setEnabled(bool(self._sources) and not self._busy)
        self.folder_clear_button.setEnabled(bool(self._sources))

        if has_image:
            pixmap = QPixmap()
            pixmap.loadFromData(self._image)
            self.thumbnail.show_at_height(pixmap, THUMB_HEIGHT)
            self.plan_label.setText(self._plan_text())
        else:
            self.thumbnail.setText(tr("enhance.no_image"))
            self.plan_label.setText(tr("enhance.no_source"))

        if self._sources:
            self.folder_label.setText(tr("enhance.folder_queued", len(self._sources)))
        else:
            self.folder_label.setText(tr("enhance.folder_none"))

    def _sync_available_amounts(self) -> None:
        """이 원본에서 고를 수 없는 배율은 잠그고, 왜인지 문구로 알린다.

        고른 배율이 잠기면 남은 것 중 가장 크게 키우는 쪽으로 옮긴다. 원본이 없으면
        전부 열어 둔다 (무엇이 가능한지는 이미지를 봐야 안다).
        """
        tr = self._i18n.get_text
        allowed = UPSCALE_AMOUNTS if self._size is None else available_amounts(self._size)
        self._syncing = True
        try:
            for amount, button in self.amount_buttons.items():
                button.setEnabled(amount in allowed)
            if not self.amount_buttons[self.amount].isEnabled():
                self.amount_buttons[allowed[-1]].setChecked(True)
        finally:
            self._syncing = False

        note = "" if self._size is None else self._unavailable_note(self._size)
        self.note_label.setText(note)
        self.note_label.setVisible(bool(note))
        self.amount_buttons["1.5x"].setToolTip(note or tr("enhance.amount_15x_tooltip"))

    def _unavailable_note(self, size: tuple[int, int]) -> str:
        """1.5x를 못 쓰는 이유 문구 (쓸 수 있으면 빈 문자열)."""
        tr = self._i18n.get_text
        reason = unavailable_reason(size, "1.5x")
        if reason is None:
            return ""
        if reason == "over_cap":
            return tr("enhance.no_15x_over_cap", *size)
        if reason == "already_max":
            return tr("enhance.no_upscale_left", *size)
        standard = ", ".join(f"{w}×{h}" for w, h in STANDARD_15X_SIZES)
        return tr("enhance.no_15x_custom", size[0], size[1], standard)

    def _plan_text(self) -> str:
        tr = self._i18n.get_text
        size = self._size
        if size is None:
            return tr("enhance.no_source")
        plan = plan_enhance(size, self.amount)
        if not plan.is_upscaling:
            return tr("enhance.plan_same", *plan.diffusion_size)
        return tr("enhance.plan", *plan.source_size, *plan.output_size, f"{plan.scale:.2f}")

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.setTitle(tr("enhance.title"))
        self.choose_button.setText(tr("enhance.choose"))
        self.use_last_button.setText(tr("enhance.use_last"))
        self.clear_button.setText(tr("enhance.clear"))
        self.amount_label.setText(tr("enhance.amount"))
        for amount, key in (("1x", "amount_1x"), ("1.5x", "amount_15x"), ("max", "amount_max")):
            self.amount_buttons[amount].setText(tr(f"enhance.{key}"))
        self.amount_buttons["max"].setToolTip(tr("enhance.amount_max_tooltip"))
        self.strength_label.setText(tr("enhance.strength"))
        self.strength_spin.setToolTip(tr("enhance.strength_tooltip"))
        self.noise_label.setText(tr("enhance.noise"))
        self.use_metadata_check.setText(tr("enhance.use_metadata"))
        self.use_metadata_check.setToolTip(tr("enhance.use_metadata_tooltip"))
        self.folder_button.setText(tr("enhance.folder"))
        self.folder_start_button.setText(tr("enhance.folder_start"))
        self.folder_clear_button.setText(tr("enhance.folder_clear"))
        self._refresh()


def _reusable_from(path: str) -> ReusableSettings | None:
    """PNG/WebP에서 생성 설정을 읽는다. 메타데이터가 없으면 None."""
    try:
        metadata = read_metadata(path)
    except Exception:
        return None
    if not metadata:
        return None
    settings = extract_reusable(metadata)
    return None if settings.is_empty else settings


def scan_folder(path: str) -> tuple[EnhanceSource, ...]:
    """폴더 안에서 강화할 수 있는 이미지를 모은다 (이름 순).

    이미지 바이트는 읽지 않는다 — 크기와 메타데이터만 챙겨 두고, 실제 파일은 그 장을
    생성할 때 읽는다 (수백 장짜리 폴더를 통째로 메모리에 올리지 않기 위해서).
    """
    sources: list[EnhanceSource] = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return ()
    for name in names:
        if not name.lower().endswith(SUPPORTED_SUFFIXES):
            continue
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        try:
            with Image.open(full) as img:
                size = img.size
        except Exception:
            continue  # 이미지가 아니거나 깨진 파일 — 조용히 건너뛴다
        sources.append(EnhanceSource(path=full, size=size, settings=_reusable_from(full)))
    return tuple(sources)
