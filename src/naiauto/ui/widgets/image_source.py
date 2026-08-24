"""i2i / 인페인팅 입력 위젯 — 원본 이미지 + 마스크 + 강도.

이미지가 없으면 t2i, 있으면 i2i, 마스크까지 있으면 인페인팅이 된다.
생성 크기는 원본 이미지 크기를 따른다 (스모크 테스트로 검증된 동작).
"""

from __future__ import annotations

import io

from PIL import Image
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from .mask_editor import MaskEditorDialog

THUMB_HEIGHT = 150


class ImageSourceWidget(QGroupBox):
    """선택된 원본/마스크를 보관하고 UI로 노출한다."""

    changed = Signal()

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        self._image: bytes | None = None
        self._mask: bytes | None = None
        self._size: tuple[int, int] | None = None
        # 자동 생성에서는 잘 쓰지 않는 기능이라 기본적으로 꺼 둔다 (보기 메뉴에서 켠다).
        # 꺼져 있으면 이미지를 들고 있어도 요청에 반영하지 않는다 — 숨은 상태가
        # 조용히 payload를 바꾸는 일이 없도록.
        self._active = False

        layout = QVBoxLayout(self)

        self.thumbnail = QLabel()
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setMinimumHeight(THUMB_HEIGHT)
        layout.addWidget(self.thumbnail)

        self.size_label = QLabel()
        self.size_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.size_label)

        image_buttons = QHBoxLayout()
        self.choose_button = QPushButton()
        self.choose_button.clicked.connect(self.choose_image)
        self.clear_button = QPushButton()
        self.clear_button.clicked.connect(self.clear_image)
        image_buttons.addWidget(self.choose_button)
        image_buttons.addWidget(self.clear_button)
        layout.addLayout(image_buttons)

        mask_row = QHBoxLayout()
        self.mask_status = QLabel()
        self.draw_mask_button = QPushButton()
        self.draw_mask_button.clicked.connect(self.edit_mask)
        self.clear_mask_button = QPushButton()
        self.clear_mask_button.clicked.connect(self.clear_mask)
        mask_row.addWidget(self.mask_status, stretch=1)
        mask_row.addWidget(self.draw_mask_button)
        mask_row.addWidget(self.clear_mask_button)
        layout.addLayout(mask_row)

        form = QFormLayout()
        self.strength_spin = QDoubleSpinBox()
        self.strength_spin.setRange(0.01, 0.99)
        self.strength_spin.setSingleStep(0.05)
        self.strength_spin.setValue(0.7)
        self.noise_spin = QDoubleSpinBox()
        self.noise_spin.setRange(0.0, 0.99)
        self.noise_spin.setSingleStep(0.01)
        self.noise_spin.setValue(0.0)
        self.strength_label = QLabel()
        self.noise_label = QLabel()
        form.addRow(self.strength_label, self.strength_spin)
        form.addRow(self.noise_label, self.noise_spin)
        layout.addLayout(form)

        # 인페인팅에서 마스크 밖을 원본 그대로 둘지 (NAI 웹UI의 "Overlay Original Image").
        # 기본은 해제 — 웹UI 캡처 2건 모두 false였다. 켜면 마스크 밖은 확실히 보존되지만
        # 생성된 안쪽과 만나는 경계가 이음선으로 드러난다.
        self.overlay_original_check = QCheckBox()
        self.overlay_original_check.setChecked(False)
        layout.addWidget(self.overlay_original_check)

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
    def mask_bytes(self) -> bytes | None:
        return self._mask if self.image_bytes is not None else None

    @property
    def size(self) -> tuple[int, int] | None:
        return self._size if self._active else None

    @property
    def strength(self) -> float:
        return self.strength_spin.value()

    @property
    def noise(self) -> float:
        return self.noise_spin.value()

    @property
    def add_original_image(self) -> bool:
        """마스크 밖을 원본으로 덮어쓸지 (인페인팅에만 의미가 있다)."""
        return self.overlay_original_check.isChecked()

    def action(self) -> str:
        if self.image_bytes is None:
            return "generate"
        return "infill" if self._mask is not None else "img2img"

    # ── 조작 ─────────────────────────────────────────────

    def choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, self._i18n.get_text("image_source.choose"), "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.load_image(path)

    def load_image(self, path: str) -> bool:
        tr = self._i18n.get_text
        try:
            data = open(path, "rb").read()
            with Image.open(io.BytesIO(data)) as img:
                size = img.size
                if img.format != "PNG":  # API는 PNG를 기대하므로 변환해 둔다
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="PNG")
                    data = buf.getvalue()
        except Exception as e:
            QMessageBox.warning(self, tr("errors.title"), tr("image_source.load_failed", e))
            return False
        self._image = data
        self._size = size
        self._mask = None  # 새 이미지에는 이전 마스크가 맞지 않는다
        self._refresh()
        self.changed.emit()
        return True

    def clear_image(self) -> None:
        self._image = None
        self._mask = None
        self._size = None
        self._refresh()
        self.changed.emit()

    def edit_mask(self) -> None:
        if self._image is None:
            return
        dialog = MaskEditorDialog(self._i18n, self._image, self._mask, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._mask = dialog.mask_bytes()
            self._refresh()
            self.changed.emit()

    def clear_mask(self) -> None:
        self._mask = None
        self._refresh()
        self.changed.emit()

    # ── 표시 ─────────────────────────────────────────────

    def _refresh(self) -> None:
        tr = self._i18n.get_text
        has_image = self._image is not None
        self.clear_button.setEnabled(has_image)
        self.draw_mask_button.setEnabled(has_image)
        self.clear_mask_button.setEnabled(self._mask is not None)

        if has_image:
            pixmap = QPixmap()
            pixmap.loadFromData(self._image)
            self.thumbnail.setPixmap(
                pixmap.scaledToHeight(THUMB_HEIGHT, Qt.TransformationMode.SmoothTransformation)
            )
            width, height = self._size or (0, 0)
            self.size_label.setText(tr("image_source.size_note", width, height))
            self.mask_status.setText(
                tr("image_source.mask_present") if self._mask else tr("image_source.mask_absent")
            )
        else:
            self.thumbnail.setPixmap(QPixmap())
            self.thumbnail.setText(tr("image_source.no_image"))
            self.size_label.setText("")
            self.mask_status.setText("")

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.setTitle(tr("image_source.title"))
        self.choose_button.setText(tr("image_source.choose"))
        self.clear_button.setText(tr("image_source.clear"))
        self.draw_mask_button.setText(tr("image_source.draw_mask"))
        self.clear_mask_button.setText(tr("image_source.clear_mask"))
        self.strength_label.setText(tr("image_source.strength"))
        self.noise_label.setText(tr("image_source.noise"))
        self.overlay_original_check.setText(tr("image_source.overlay_original"))
        self.overlay_original_check.setToolTip(tr("image_source.overlay_original_tooltip"))
        self._refresh()
