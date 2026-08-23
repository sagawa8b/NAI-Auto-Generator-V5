"""프리셋 매니저 다이얼로그 — 생성 파라미터 프리셋 CRUD UI.

PresetStore를 통해 프리셋 목록 조회, 저장, 불러오기, 이름변경, 삭제를 수행한다.
모델 키 검증은 ModelSpec 레지스트리를 참조하며, 알 수 없는 모델은 경고만 표시하고
다른 필드는 정상 적용한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.api.model_specs import MODEL_REGISTRY
from ..core.presets import GenerationPreset, PresetError, PresetStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class PresetManagerDialog(QDialog):
    """프리셋 관리 다이얼로그.

    Signals:
        preset_loaded(GenerationPreset): 프리셋이 정상 로드되었을 때 방출.
            수신 측은 이 프리셋을 UI에 원자적으로 적용해야 한다.
    """

    preset_loaded = Signal(object)  # carries GenerationPreset

    def __init__(
        self,
        store: PresetStore,
        get_current_config: Callable[[], GenerationPreset],
        parent: QWidget | None = None,
    ) -> None:
        """PresetManagerDialog 초기화.

        Args:
            store: 프리셋 CRUD를 담당하는 PresetStore 인스턴스.
            get_current_config: 현재 UI 상태를 GenerationPreset으로 반환하는 콜백.
            parent: 부모 위젯.
        """
        super().__init__(parent)
        self._store = store
        self._get_current_config = get_current_config

        self.setWindowTitle("Preset Manager")
        self.setMinimumSize(400, 350)

        self._build_ui()
        self._refresh_list()

    # ──────────────────────────────────────────────────────────────
    # UI Construction
    # ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)

        # Left: preset list
        self._list_widget = QListWidget()
        self._list_widget.setMinimumWidth(200)
        layout.addWidget(self._list_widget, stretch=1)

        # Right: action buttons
        btn_layout = QVBoxLayout()

        self._btn_save = QPushButton("Save")
        self._btn_save.setToolTip("Save current configuration as a preset")
        self._btn_save.clicked.connect(self._on_save)
        btn_layout.addWidget(self._btn_save)

        self._btn_load = QPushButton("Load")
        self._btn_load.setToolTip("Load the selected preset into the UI")
        self._btn_load.clicked.connect(self._on_load)
        btn_layout.addWidget(self._btn_load)

        self._btn_rename = QPushButton("Rename")
        self._btn_rename.setToolTip("Rename the selected preset")
        self._btn_rename.clicked.connect(self._on_rename)
        btn_layout.addWidget(self._btn_rename)

        self._btn_delete = QPushButton("Delete")
        self._btn_delete.setToolTip("Delete the selected preset")
        self._btn_delete.clicked.connect(self._on_delete)
        btn_layout.addWidget(self._btn_delete)

        btn_layout.addStretch()

        self._btn_close = QPushButton("Close")
        self._btn_close.clicked.connect(self.close)
        btn_layout.addWidget(self._btn_close)

        layout.addLayout(btn_layout)

    # ──────────────────────────────────────────────────────────────
    # List Management
    # ──────────────────────────────────────────────────────────────

    def _refresh_list(self) -> None:
        """프리셋 목록을 다시 불러온다."""
        self._list_widget.clear()
        for name in self._store.list_presets():
            self._list_widget.addItem(name)

    def _selected_name(self) -> str | None:
        """현재 선택된 프리셋 이름을 반환. 없으면 None."""
        item = self._list_widget.currentItem()
        return item.text() if item else None

    # ──────────────────────────────────────────────────────────────
    # Actions
    # ──────────────────────────────────────────────────────────────

    def _on_save(self) -> None:
        """현재 설정을 프리셋으로 저장. 이름 입력 → 중복 시 확인 → 저장."""
        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        if not ok or not name.strip():
            return
        name = name.strip()

        # Overwrite confirmation (Requirement 6.3)
        if self._store.exists(name):
            reply = QMessageBox.question(
                self,
                "Overwrite Preset",
                f'A preset named "{name}" already exists.\nOverwrite it?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            config = self._get_current_config()
            preset = config.model_copy(update={"name": name})
            self._store.save(preset)
            self._refresh_list()
        except Exception as exc:
            logger.exception("Failed to save preset %r", name)
            QMessageBox.critical(self, "Save Error", f"Failed to save preset:\n{exc}")

    def _on_load(self) -> None:
        """선택된 프리셋을 로드. 원자적 적용 — 실패 시 UI 변경 없음.

        알 수 없는 모델 키는 경고만 표시하고, 나머지 필드는 정상 적용한다
        (모델 셀렉터는 변경하지 않음).
        """
        name = self._selected_name()
        if name is None:
            QMessageBox.information(self, "Load Preset", "No preset selected.")
            return

        try:
            preset = self._store.load(name)
        except PresetError as exc:
            # Requirement 6.10: corrupted preset → error message, UI unchanged
            QMessageBox.critical(
                self,
                "Load Error",
                f"Cannot load preset:\n{exc}",
            )
            return

        # Requirement 6.6: warn on unknown model key
        unknown_model = False
        if preset.model not in MODEL_REGISTRY:
            unknown_model = True
            QMessageBox.warning(
                self,
                "Unknown Model",
                f'The preset references model "{preset.model}" which is not recognized.\n'
                "Other settings will be loaded, but the model selector will remain unchanged.",
            )

        # Emit signal for the main window to apply atomically (Requirement 6.4)
        # The signal carries the preset; if unknown_model is True, the receiver
        # should skip the model field.
        if unknown_model:
            # Set model to empty string as a sentinel — receiver should ignore it
            preset = preset.model_copy(update={"model": ""})

        self.preset_loaded.emit(preset)

    def _on_rename(self) -> None:
        """선택된 프리셋의 이름을 변경한다."""
        old_name = self._selected_name()
        if old_name is None:
            QMessageBox.information(self, "Rename Preset", "No preset selected.")
            return

        new_name, ok = QInputDialog.getText(self, "Rename Preset", "New name:", text=old_name)
        if not ok or not new_name.strip():
            return
        new_name = new_name.strip()

        if new_name == old_name:
            return

        try:
            self._store.rename(old_name, new_name)
            self._refresh_list()
        except PresetError as exc:
            QMessageBox.critical(self, "Rename Error", f"Failed to rename preset:\n{exc}")

    def _on_delete(self) -> None:
        """선택된 프리셋을 삭제. 확인 다이얼로그 후 실행."""
        name = self._selected_name()
        if name is None:
            QMessageBox.information(self, "Delete Preset", "No preset selected.")
            return

        # Requirement 6.8: confirmation before deletion
        reply = QMessageBox.question(
            self,
            "Delete Preset",
            f'Are you sure you want to delete the preset "{name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self._store.delete(name)
            self._refresh_list()
        except PresetError as exc:
            QMessageBox.critical(self, "Delete Error", f"Failed to delete preset:\n{exc}")
