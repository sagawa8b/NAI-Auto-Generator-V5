"""씬 큐 창 — 씬마다 설정과 매수를 정해 두고 한 번에 돌린다.

자료와 실행 순서는 `core/scene_queue.py`가 갖는다. 이 창은 씬 목록을 고치고, 메인 창에
"이 씬을 화면에 적용해 달라"·"이 큐를 돌려 달라"고 알리기만 한다 — 생성은 메인 창의
생성 경로가 한다 (그래야 결과 미리보기·갤러리·히스토리에 그대로 뜬다).

목록은 고칠 때마다 자동으로 저장된다 (`default_queue_path()`). 창을 닫거나 앱을 껐다 켜도
큐가 남는다. `세트 저장…`은 그와 별개로, 이름 붙여 따로 보관해 둘 때 쓴다.

창은 모드리스다 — 띄워 둔 채 메인 창에서 프롬프트를 고치고 `현재 설정으로 추가`를
누르는 것이 기본 흐름이다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import scene_queue as scene_queue_store
from ..core import settings_file
from ..core.i18n.manager import I18nManager
from ..core.presets import PresetError
from ..core.scene_queue import (
    SCENE_COUNT_RANGE,
    Scene,
    SceneSet,
    load_scene_set,
    load_scene_set_or_empty,
    plan_steps,
    save_scene_set,
    total_images,
    unique_name,
)
from .widgets.window_chrome import enable_window_controls

COL_NAME, COL_COUNT, COL_SUMMARY = range(3)
_SUMMARY_LENGTH = 80


class SceneQueueDialog(QDialog):
    """씬 목록 편집 + 실행 요청.

    Signals:
        apply_requested(dict): 이 씬의 설정을 메인 화면에 적용해 달라.
        run_requested(object): 이 `SceneSet`을 돌려 달라.
    """

    apply_requested = Signal(dict)
    run_requested = Signal(object)

    def __init__(
        self,
        i18n: I18nManager,
        capture: Callable[[], dict],
        *,
        default_dir: str = "",
        queue_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        #: 지금 메인 화면의 생성 설정을 dict로 (`settings_file.to_dict` 형식).
        self._capture = capture
        self._default_dir = default_dir
        # 모듈을 거쳐 부른다 — 테스트가 이 함수를 임시 폴더로 바꿔 끼운다
        self._queue_path = queue_path or scene_queue_store.default_queue_path()
        self._set = load_scene_set_or_empty(self._queue_path)
        self._can_run = True
        #: 표를 코드로 채우는 중인가 — 그때의 `itemChanged`는 사용자 편집이 아니다.
        self._filling = False

        self.table = QTableWidget(0, 3)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_COUNT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_SUMMARY, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(COL_NAME, 180)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._refresh_buttons)

        self.add_button = QPushButton()
        self.add_button.clicked.connect(self.add_current)
        self.import_button = QPushButton()
        self.import_button.clicked.connect(self._on_import_files)
        self.update_button = QPushButton()
        self.update_button.clicked.connect(self.update_selected)
        self.apply_button = QPushButton()
        self.apply_button.clicked.connect(self.apply_selected)
        self.up_button = QPushButton("↑")
        self.up_button.clicked.connect(lambda: self.move_selected(-1))
        self.down_button = QPushButton("↓")
        self.down_button.clicked.connect(lambda: self.move_selected(1))
        self.remove_button = QPushButton()
        self.remove_button.clicked.connect(self.remove_selected)

        self.interleave_check = QCheckBox()
        self.interleave_check.setChecked(self._set.interleave)
        self.interleave_check.toggled.connect(self._on_interleave_toggled)
        self.total_label = QLabel()
        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: gray;")

        self.save_set_button = QPushButton()
        self.save_set_button.clicked.connect(self._on_save_set)
        self.load_set_button = QPushButton()
        self.load_set_button.clicked.connect(self._on_load_set)
        self.run_button = QPushButton()
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self._on_run)
        self.close_button = QPushButton()
        self.close_button.clicked.connect(self.close)

        edit_row = QHBoxLayout()
        for button in (self.add_button, self.import_button, self.update_button, self.apply_button):
            edit_row.addWidget(button)
        edit_row.addStretch(1)
        for button in (self.up_button, self.down_button, self.remove_button):
            edit_row.addWidget(button)

        option_row = QHBoxLayout()
        option_row.addWidget(self.interleave_check)
        option_row.addStretch(1)
        option_row.addWidget(self.total_label)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.save_set_button)
        bottom_row.addWidget(self.load_set_button)
        bottom_row.addStretch(1)
        bottom_row.addWidget(self.run_button)
        bottom_row.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addLayout(edit_row)
        layout.addLayout(option_row)
        layout.addWidget(self.hint_label)
        layout.addLayout(bottom_row)

        self.resize(760, 480)
        enable_window_controls(self)
        self.retranslate()
        self._refill()

    # ── 상태 ─────────────────────────────────────────────

    @property
    def scene_set(self) -> SceneSet:
        return self._set

    def set_can_run(self, can_run: bool) -> None:
        """메인 창이 생성할 수 있는 상태인지 (로그인됨 · 다른 생성이 돌고 있지 않음)."""
        self._can_run = can_run
        self._refresh_buttons()

    def selected_row(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _persist(self) -> None:
        """자동 저장. 실패해도 편집은 계속할 수 있어야 한다 — 다음 변경 때 다시 쓴다."""
        try:
            save_scene_set(self._queue_path, self._set)
        except OSError:
            pass

    # ── 표 ───────────────────────────────────────────────

    def _refill(self, select: int | None = None) -> None:
        self._filling = True
        try:
            self.table.setRowCount(len(self._set.scenes))
            for row, scene in enumerate(self._set.scenes):
                name_item = QTableWidgetItem(scene.name)
                name_item.setFlags(name_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                name_item.setCheckState(Qt.CheckState.Checked if scene.enabled else Qt.CheckState.Unchecked)
                count_item = QTableWidgetItem(str(scene.clamped_count()))
                count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                summary_item = QTableWidgetItem(_summary(scene))
                summary_item.setFlags(summary_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                summary_item.setToolTip(str(scene.settings.get("prompt", "")))
                self.table.setItem(row, COL_NAME, name_item)
                self.table.setItem(row, COL_COUNT, count_item)
                self.table.setItem(row, COL_SUMMARY, summary_item)
        finally:
            self._filling = False
        if select is not None and 0 <= select < len(self._set.scenes):
            self.table.selectRow(select)
        self._refresh_total()
        self._refresh_buttons()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        row = item.row()
        if not 0 <= row < len(self._set.scenes):
            return
        scene = self._set.scenes[row]
        if item.column() == COL_NAME:
            scene.enabled = item.checkState() == Qt.CheckState.Checked
            others = [other.name for index, other in enumerate(self._set.scenes) if index != row]
            scene.name = unique_name(item.text(), others)
        elif item.column() == COL_COUNT:
            low, high = SCENE_COUNT_RANGE
            try:
                scene.count = max(low, min(high, int(item.text().strip())))
            except ValueError:
                pass  # 숫자가 아니면 전 값으로 되돌린다 (아래 _refill)
        self._persist()
        self._refill(select=row)

    def _refresh_total(self) -> None:
        steps = plan_steps(self._set.scenes, interleave=self._set.interleave)
        enabled = sum(1 for scene in self._set.scenes if scene.enabled)
        self.total_label.setText(self._i18n.get_text("scene_queue.total", enabled, total_images(steps)))

    def _refresh_buttons(self) -> None:
        row = self.selected_row()
        has_row = row >= 0
        for button in (self.update_button, self.apply_button, self.remove_button):
            button.setEnabled(has_row)
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < len(self._set.scenes) - 1)
        runnable = any(scene.enabled for scene in self._set.scenes)
        self.run_button.setEnabled(self._can_run and runnable)
        self.save_set_button.setEnabled(bool(self._set.scenes))

    # ── 편집 ─────────────────────────────────────────────

    def add_scene(self, name: str, settings: dict, count: int = 1) -> Scene:
        """씬을 끝에 붙인다. 이름이 겹치면 번호를 붙인다."""
        scene = Scene(
            name=unique_name(name, [existing.name for existing in self._set.scenes]),
            count=count,
            settings=dict(settings),
        )
        self._set.scenes.append(scene)
        self._persist()
        self._refill(select=len(self._set.scenes) - 1)
        return scene

    def add_current(self) -> Scene:
        """지금 메인 화면의 설정을 새 씬으로."""
        base = self._i18n.get_text("scene_queue.default_name")
        return self.add_scene(base, self._capture())

    def import_files(self, paths: list[str]) -> int:
        """세팅 파일(`파일 → 설정 저장`으로 만든 것)을 씬으로 가져온다. 가져온 개수.

        읽을 수 없는 파일은 건너뛰고, 다 끝난 뒤 한 번에 알린다. 파일 이름이 씬 이름이
        된다. 파일에 없는 항목은 지금 화면 값으로 채운다 (V4 파일은 항목이 빠져 있다).
        """
        defaults = {key: value for key, value in self._capture().items() if key not in ("format", "version")}
        failed: list[str] = []
        imported = 0
        for path in paths:
            try:
                loaded = settings_file.load(Path(path), defaults=defaults)
            except PresetError:
                failed.append(Path(path).name)
                continue
            self.add_scene(Path(path).stem, settings_file.to_dict(loaded.preset))
            imported += 1
        if failed:
            tr = self._i18n.get_text
            QMessageBox.warning(
                self, tr("errors.warning"), tr("scene_queue.import_failed", ", ".join(failed))
            )
        return imported

    def update_selected(self) -> bool:
        """고른 씬의 설정을 지금 화면 값으로 바꾼다 (이름·매수는 그대로)."""
        row = self.selected_row()
        if row < 0:
            return False
        self._set.scenes[row].settings = self._capture()
        self._persist()
        self._refill(select=row)
        return True

    def apply_selected(self) -> bool:
        """고른 씬을 메인 화면에 적용해 달라고 알린다."""
        row = self.selected_row()
        if row < 0:
            return False
        self.apply_requested.emit(dict(self._set.scenes[row].settings))
        return True

    def move_selected(self, delta: int) -> bool:
        row = self.selected_row()
        target = row + delta
        scenes = self._set.scenes
        if row < 0 or not 0 <= target < len(scenes):
            return False
        scenes[row], scenes[target] = scenes[target], scenes[row]
        self._persist()
        self._refill(select=target)
        return True

    def remove_selected(self) -> bool:
        row = self.selected_row()
        if row < 0:
            return False
        del self._set.scenes[row]
        self._persist()
        self._refill(select=min(row, len(self._set.scenes) - 1))
        return True

    def _on_interleave_toggled(self, on: bool) -> None:
        self._set.interleave = on
        self._persist()
        self._refresh_total()

    # ── 파일 ─────────────────────────────────────────────

    def _file_filter(self) -> str:
        tr = self._i18n.get_text
        return f"{tr('scene_queue.set_filter')} (*.json);;{tr('menu.all_files_filter')} (*)"

    def _on_import_files(self) -> None:
        tr = self._i18n.get_text
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            tr("scene_queue.import_files"),
            self._default_dir,
            f"{tr('menu.settings_file_filter')} (*.json *.txt);;{tr('menu.all_files_filter')} (*)",
        )
        if paths:
            self.import_files(paths)

    def _on_save_set(self) -> None:
        tr = self._i18n.get_text
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("scene_queue.save_set"),
            str(Path(self._default_dir) / "scenes.json"),
            self._file_filter(),
        )
        if not path:
            return
        try:
            save_scene_set(Path(path), self._set)
        except OSError as e:
            QMessageBox.warning(self, tr("errors.title"), f"{tr('scene_queue.save_set')}\n\n{e}")

    def _on_load_set(self) -> None:
        tr = self._i18n.get_text
        path, _ = QFileDialog.getOpenFileName(
            self, tr("scene_queue.load_set"), self._default_dir, self._file_filter()
        )
        if path:
            self.load_set(Path(path))

    def load_set(self, path: Path) -> bool:
        """세트 파일로 지금 큐를 **바꾼다**. 읽지 못하면 그대로 두고 False."""
        tr = self._i18n.get_text
        try:
            loaded = load_scene_set(path)
        except PresetError as e:
            text = tr(e.i18n_key, *e.i18n_args) if e.i18n_key else str(e)
            QMessageBox.warning(self, tr("errors.title"), text)
            return False
        self._set = loaded
        self.interleave_check.blockSignals(True)
        self.interleave_check.setChecked(loaded.interleave)
        self.interleave_check.blockSignals(False)
        self._persist()
        self._refill(select=0)
        return True

    def _on_run(self) -> None:
        self.run_requested.emit(self._set.model_copy(deep=True))

    # ── i18n ─────────────────────────────────────────────

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.setWindowTitle(tr("scene_queue.title"))
        self.table.setHorizontalHeaderLabels(
            [tr("scene_queue.col_name"), tr("scene_queue.col_count"), tr("scene_queue.col_prompt")]
        )
        self.add_button.setText(tr("scene_queue.add_current"))
        self.add_button.setToolTip(tr("scene_queue.add_current_hint"))
        self.import_button.setText(tr("scene_queue.import_files"))
        self.import_button.setToolTip(tr("scene_queue.import_files_hint"))
        self.update_button.setText(tr("scene_queue.update"))
        self.update_button.setToolTip(tr("scene_queue.update_hint"))
        self.apply_button.setText(tr("scene_queue.apply"))
        self.apply_button.setToolTip(tr("scene_queue.apply_hint"))
        self.up_button.setToolTip(tr("scene_queue.move_up"))
        self.down_button.setToolTip(tr("scene_queue.move_down"))
        self.remove_button.setText(tr("scene_queue.remove"))
        self.interleave_check.setText(tr("scene_queue.interleave"))
        self.interleave_check.setToolTip(tr("scene_queue.interleave_hint"))
        self.hint_label.setText(tr("scene_queue.hint"))
        self.save_set_button.setText(tr("scene_queue.save_set"))
        self.load_set_button.setText(tr("scene_queue.load_set"))
        self.run_button.setText(tr("scene_queue.run"))
        self.close_button.setText(tr("scene_queue.close"))
        self._refresh_total()


def _summary(scene: Scene) -> str:
    """표에 보일 프롬프트 한 줄 (줄바꿈을 지우고 앞부분만)."""
    prompt = " ".join(str(scene.settings.get("prompt", "")).split())
    return prompt if len(prompt) <= _SUMMARY_LENGTH else prompt[: _SUMMARY_LENGTH - 1] + "…"


__all__ = ["SceneQueueDialog"]
