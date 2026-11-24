"""씬 에디터 창 (옛 이름 씬 큐) — 씬마다 설정과 매수를 정해 두고 한 번에 돌린다.

자료와 실행 순서는 `core/scene_queue.py`가 갖는다. 이 창은 씬 목록을 고치고, 메인 창에
"이 씬을 화면에 적용해 달라"·"이 큐를 돌려 달라"고 알리기만 한다 — 생성은 메인 창의
생성 경로가 한다 (그래야 결과 미리보기·갤러리·히스토리에 그대로 뜬다).

목록은 고칠 때마다 자동으로 저장된다 (`default_queue_path()`). 창을 닫거나 앱을 껐다 켜도
큐가 남는다. `세트 저장…`은 그와 별개로, 이름 붙여 따로 보관해 둘 때 쓴다.

창은 모드리스다 — 띄워 둔 채 메인 창에서 프롬프트를 고치고 `현재 설정으로 추가`를
누르는 것이 기본 흐름이다. 그 밖에:

- 이미지(.png/.webp)나 세팅 파일을 표에 끌어다 놓으면 씬으로 담는다 (이미지는
  NovelAI 메타데이터를 읽는다)
- 행을 끌어 순서를 바꾼다
- 프롬프트 칸을 두 번 누르면 프롬프트 편집 창이 뜬다 — 메인 창과 같은 구문 강조·
  자동완성(와일드카드 이름 포함)이 붙고, `미리보기`로 와일드카드·랜덤 문법을 한 번
  전개해 볼 수 있다. 실제 전개는 생성할 때 장마다 한다 (메인 창의 생성 경로 그대로).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import scene_queue as scene_queue_store
from ..core import settings_file
from ..core.i18n.manager import I18nManager
from ..core.presets import PresetError
from ..core.scene_queue import (
    SCENE_COUNT_RANGE,
    SCENE_IMAGE_SUFFIXES,
    SCENE_SETTINGS_SUFFIXES,
    Scene,
    SceneSet,
    load_scene_set,
    load_scene_set_or_empty,
    move_scene,
    plan_steps,
    read_scene_from_image,
    save_scene_set,
    scene_is_dynamic,
    total_images,
    unique_name,
)
from .widgets.prompt_highlighter import PromptHighlighter
from .widgets.window_chrome import enable_window_controls

COL_NAME, COL_COUNT, COL_SUMMARY = range(3)
_SUMMARY_LENGTH = 80
#: 와일드카드·랜덤 문법이 든 씬의 프롬프트 칸 앞에 붙는 표시.
DYNAMIC_MARK = "🎲 "

#: 편집기 하나에 자동완성을 붙이고, 뗄 때 부를 함수를 돌려준다 (메인 창이 준다).
EditorDecorator = Callable[[QPlainTextEdit], Callable[[], None]]


def dropped_paths(mime: QMimeData | None) -> list[str]:
    """끌어다 놓은 것 중 씬으로 담을 수 있는 파일 (이미지·세팅 파일) 경로."""
    if mime is None or not mime.hasUrls():
        return []
    accepted = SCENE_IMAGE_SUFFIXES + SCENE_SETTINGS_SUFFIXES
    paths = []
    for url in mime.urls():
        local = url.toLocalFile()
        if local and Path(local).suffix.lower() in accepted:
            paths.append(local)
    return paths


class SceneTable(QTableWidget):
    """행을 끌어 순서를 바꾸고, 밖에서 끌어온 파일을 받는 표.

    `QTableWidget`의 기본 내부 이동은 칸을 덮어쓰거나 원본 행을 지워 버린다 — 여기서는
    놓을 자리만 계산해 알리고, 실제 순서는 다이얼로그가 자료(`SceneSet`)에서 바꾼 뒤 표를
    다시 채운다.

    Signals:
        row_moved(int, int): `source`행을 `target` 자리(빼기 전 기준, 0 ~ 행 수)로.
        files_dropped(list): 밖에서 끌어다 놓은 파일 경로.
        delete_pressed(): 칸을 고치는 중이 아닐 때 Delete 키를 눌렀다.
    """

    row_moved = Signal(int, int)
    files_dropped = Signal(list)
    delete_pressed = Signal()

    def __init__(self, rows: int, columns: int, parent: QWidget | None = None) -> None:
        super().__init__(rows, columns, parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropOverwriteMode(False)
        self.setDropIndicatorShown(True)

    def insertion_row(self, y: int) -> int:
        """뷰포트 y 좌표에 놓으면 끼워질 자리 — 행의 위쪽 절반이면 그 행 앞, 아래쪽이면 뒤."""
        row = self.rowAt(y)
        if row < 0:
            return self.rowCount()
        rect = self.visualRect(self.model().index(row, 0))
        return row + 1 if y > rect.center().y() else row

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        # 칸을 고치는 중에는 편집기가 키를 먼저 받는다 — 글자를 지우는 Delete는 여기 오지 않는다
        if event.key() == Qt.Key.Key_Delete and self.state() != QAbstractItemView.State.EditingState:
            self.delete_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        if event.source() is self:
            super().dragEnterEvent(event)
        elif dropped_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        if event.source() is self:
            super().dragMoveEvent(event)
            event.accept()
        elif dropped_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        if event.source() is self:
            rows = self.selectionModel().selectedRows()
            target = self.insertion_row(int(event.position().y()))
            # 이동(Move)으로 끝나면 Qt가 원본 행을 지운다 — 복사로 마무리하고 순서는 우리가 바꾼다
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            if rows:
                self.row_moved.emit(rows[0].row(), target)
            return
        paths = dropped_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
        else:
            event.ignore()


class ScenePromptDialog(QDialog):
    """씬 하나의 프롬프트 편집 — 메인·네거티브, 캐릭터 슬롯마다 프롬프트·UC.

    `decorate`가 있으면 편집기마다 메인 창과 같은 자동완성(태그·와일드카드 이름)을 붙인다.
    `preview`가 있으면 `미리보기`가 지금 탭의 글을 한 번 전개해 아래에 보여 준다.

    모든 위젯을 만들 때부터 이 창에 매단다 (부모 없는 레이아웃에 먼저 넣었다가 나중에
    옮기지 않는다) — 파이썬 쪽 객체와 Qt 객체의 수명이 어긋날 틈을 남기지 않기 위해서다.
    창은 닫히면 지워진다(`WA_DeleteOnClose`). 여는 쪽은 `exec()` 대신 `open()`을 쓴다
    (`SceneQueueDialog.edit_prompt` 참고).
    """

    def __init__(
        self,
        i18n: I18nManager,
        scene: Scene,
        *,
        decorate: EditorDecorator | None = None,
        preview: Callable[[str], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        tr = i18n.get_text
        self._preview = preview
        self._cleanups: list[Callable[[], None]] = []
        self._highlighters: list[PromptHighlighter] = []
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(tr("scene_queue.edit_prompt_title", scene.name))

        settings = scene.settings
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)
        self.prompt_edit = self._editor(str(settings.get("prompt", "")), decorate)
        self.negative_edit = self._editor(str(settings.get("negative_prompt", "")), decorate)
        self.tabs.addTab(self.prompt_edit, tr("scene_queue.edit_prompt_tab"))
        self.tabs.addTab(self.negative_edit, tr("scene_queue.edit_negative_tab"))
        #: 캐릭터 슬롯마다 (프롬프트, UC) 편집기.
        self.character_edits: list[tuple[QPlainTextEdit, QPlainTextEdit]] = []
        for index, character in enumerate(settings.get("characters") or []):
            character = character if isinstance(character, dict) else {}
            prompt = self._editor(str(character.get("prompt", "")), decorate)
            uc = self._editor(str(character.get("uc", "")), decorate)
            uc.setPlaceholderText(tr("scene_queue.edit_character_uc"))
            splitter = QSplitter(Qt.Orientation.Vertical, self.tabs)
            splitter.addWidget(prompt)
            splitter.addWidget(uc)
            splitter.setSizes([3, 1])
            self.tabs.addTab(splitter, tr("scene_queue.edit_character_tab", index + 1))
            self.character_edits.append((prompt, uc))

        self.preview_view = QPlainTextEdit(self)
        self.preview_view.setReadOnly(True)
        self.preview_view.setPlaceholderText(tr("scene_queue.preview_placeholder"))
        self.preview_view.setMaximumHeight(110)
        self.preview_view.setVisible(preview is not None)
        layout.addWidget(self.preview_view)
        self.hint_label = QLabel(tr("scene_queue.edit_prompt_hint"), self)
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: gray;")
        layout.addWidget(self.hint_label)

        # 아래 줄 레이아웃은 위젯을 넣기 전에 먼저 창의 레이아웃에 매단다
        bottom = QHBoxLayout()
        layout.addLayout(bottom)
        self.preview_button = QPushButton(tr("scene_queue.preview"), self)
        self.preview_button.setToolTip(tr("scene_queue.preview_hint"))
        self.preview_button.clicked.connect(self.show_preview)
        self.preview_button.setVisible(preview is not None)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        bottom.addWidget(self.preview_button)
        bottom.addStretch(1)
        bottom.addWidget(self.buttons)
        self.resize(620, 460)
        enable_window_controls(self)
        self.prompt_edit.setFocus()

    def _editor(self, text: str, decorate: EditorDecorator | None) -> QPlainTextEdit:
        edit = QPlainTextEdit(self)
        edit.setPlainText(text)
        self._highlighters.append(PromptHighlighter(edit.document()))
        if decorate is not None:
            self._cleanups.append(decorate(edit))
        return edit

    def current_text(self) -> str:
        """지금 탭의 프롬프트 글 (캐릭터 탭이면 그 슬롯의 프롬프트)."""
        index = self.tabs.currentIndex()
        if index == 0:
            return self.prompt_edit.toPlainText()
        if index == 1:
            return self.negative_edit.toPlainText()
        return self.character_edits[index - 2][0].toPlainText()

    def show_preview(self) -> str:
        if self._preview is None:
            return ""
        text = self._preview(self.current_text())
        self.preview_view.setPlainText(text)
        return text

    def apply_to(self, scene: Scene) -> None:
        """편집한 글을 씬 설정에 쓴다 (프롬프트 밖의 값은 건드리지 않는다)."""
        settings = dict(scene.settings)
        settings["prompt"] = self.prompt_edit.toPlainText()
        settings["negative_prompt"] = self.negative_edit.toPlainText()
        characters = [dict(c) if isinstance(c, dict) else {} for c in settings.get("characters") or []]
        for character, (prompt, uc) in zip(characters, self.character_edits, strict=False):
            character["prompt"] = prompt.toPlainText()
            character["uc"] = uc.toPlainText()
        if characters:
            settings["characters"] = characters
        scene.settings = settings

    def done(self, result: int) -> None:
        # 자동완성 드롭다운은 메인 창이 들고 있다 — 편집기가 사라지기 전에 떼어 낸다
        self.detach_editors()
        super().done(result)

    def detach_editors(self) -> None:
        """편집기에 붙인 자동완성을 뗀다. 두 번 불러도 된다."""
        cleanups, self._cleanups = self._cleanups, []
        for cleanup in cleanups:
            cleanup()


class SceneQueueDialog(QDialog):
    """씬 에디터 — 씬 목록 편집 + 실행 요청.

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
        decorate_editor: EditorDecorator | None = None,
        preview: Callable[[str], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._i18n = i18n
        #: 프롬프트 편집 창의 편집기에 자동완성을 붙이는 함수 / 한 번 전개해 보는 함수.
        self._decorate_editor = decorate_editor
        self._preview = preview
        #: 지금 메인 화면의 생성 설정을 dict로 (`settings_file.to_dict` 형식).
        self._capture = capture
        self._default_dir = default_dir
        # 모듈을 거쳐 부른다 — 테스트가 이 함수를 임시 폴더로 바꿔 끼운다
        self._queue_path = queue_path or scene_queue_store.default_queue_path()
        self._set = load_scene_set_or_empty(self._queue_path)
        self._can_run = True
        #: 떠 있는 프롬프트 편집 창 (한 번에 하나).
        self._prompt_editor: ScenePromptDialog | None = None
        #: 표를 코드로 채우는 중인가 — 그때의 `itemChanged`는 사용자 편집이 아니다.
        self._filling = False

        self.table = SceneTable(0, 3)
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
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.row_moved.connect(self.move_scene)
        self.table.files_dropped.connect(self.import_files)
        self.table.delete_pressed.connect(self.remove_selected)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        # 표 밖(버튼 줄 등)에 놓아도 받는다
        self.setAcceptDrops(True)

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
        self.single_folder_check = QCheckBox()
        self.single_folder_check.setChecked(self._set.single_folder)
        self.single_folder_check.toggled.connect(self._on_single_folder_toggled)
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
        option_row.addWidget(self.single_folder_check)
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
                dynamic = scene_is_dynamic(scene)
                summary_item = QTableWidgetItem((DYNAMIC_MARK if dynamic else "") + _summary(scene))
                summary_item.setFlags(summary_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tooltip = str(scene.settings.get("prompt", ""))
                if dynamic:
                    tooltip = f"{self._i18n.get_text('scene_queue.dynamic_hint')}\n\n{tooltip}"
                summary_item.setToolTip(tooltip)
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
        """세팅 파일·이미지를 씬으로 가져온다. 가져온 개수.

        - 세팅 파일(`파일 → 설정 저장`으로 만든 것) — 파일에 없는 항목은 지금 화면 값으로
          채운다 (V4 파일은 항목이 빠져 있다).
        - 이미지(.png/.webp) — NovelAI 메타데이터를 읽어 지금 화면 값 위에 얹는다
          (`scene_settings_from_image`).

        파일 이름이 씬 이름이 된다. 읽을 수 없는 파일은 건너뛰고, 다 끝난 뒤 한 번에 알린다.
        """
        current = self._capture()
        defaults = {key: value for key, value in current.items() if key not in ("format", "version")}
        failed: list[str] = []
        imported = 0
        for path in paths:
            if Path(path).suffix.lower() in SCENE_IMAGE_SUFFIXES:
                data = read_scene_from_image(Path(path), current)
                if data is None:
                    failed.append(Path(path).name)
                    continue
                self.add_scene(Path(path).stem, data)
                imported += 1
                continue
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

    def move_scene(self, source: int, target: int) -> bool:
        """`source`번 씬을 `target` 자리(빼기 전 기준)로 옮긴다 — 행 끌어 놓기."""
        moved = move_scene(self._set.scenes, source, target)
        if moved < 0 or moved == source:
            return False
        self._persist()
        self._refill(select=moved)
        return True

    def edit_prompt(self, row: int) -> ScenePromptDialog | None:
        """`row`번 씬의 프롬프트 편집 창을 띄운다 (돌려주는 창은 이미 떠 있다).

        `exec()`로 막고 기다리지 않고 `open()`으로 띄운 뒤 `finished`에서 결과를 받는다.
        v0.21.0은 표의 더블클릭 처리 **안에서** `exec()`의 중첩 이벤트 루프를 돌렸는데,
        PySide 6.12 실행 파일에서 그 창이 뜨는 순간 네이티브 크래시가 났다 (접근 위반,
        `naiauto.crash.log`). 중첩 루프를 없애고, 여는 것도 표의 이벤트가 끝난 뒤로 미룬다
        (`_on_cell_double_clicked`). 이미 편집 창이 떠 있으면 그 창을 앞으로 가져온다.
        """
        if self._prompt_editor is not None:
            self._prompt_editor.raise_()
            self._prompt_editor.activateWindow()
            return self._prompt_editor
        if not 0 <= row < len(self._set.scenes):
            return None
        scene = self._set.scenes[row]
        dialog = ScenePromptDialog(
            self._i18n, scene, decorate=self._decorate_editor, preview=self._preview, parent=self
        )
        self._prompt_editor = dialog
        dialog.finished.connect(lambda result: self._on_prompt_editor_finished(dialog, scene, result))
        dialog.open()
        return dialog

    def _on_prompt_editor_finished(self, dialog: ScenePromptDialog, scene: Scene, result: int) -> None:
        self._prompt_editor = None
        if result != QDialog.DialogCode.Accepted:
            return
        # 창이 떠 있는 동안 목록이 바뀌었을 수 있다 — 행 번호가 아니라 씬으로 찾는다
        row = next((index for index, item in enumerate(self._set.scenes) if item is scene), -1)
        if row < 0:
            return
        dialog.apply_to(scene)
        self._persist()
        self._refill(select=row)

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        # 이름·매수 칸은 그 자리에서 고친다. 프롬프트 칸만 편집 창을 띄운다 — 표가 더블클릭을
        # 다 처리한 다음에 (표의 마우스 이벤트 처리 도중에 창을 띄우지 않는다).
        if column == COL_SUMMARY:
            QTimer.singleShot(0, lambda: self.edit_prompt(row))

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

    def _on_single_folder_toggled(self, on: bool) -> None:
        self._set.single_folder = on
        self._persist()
        self._refresh_set_name()

    def _refresh_set_name(self) -> None:
        """창 제목에 지금 세트 이름을, 한 폴더 체크의 툴팁에 저장될 폴더를 보인다."""
        tr = self._i18n.get_text
        name = self._set.set_name
        title = tr("scene_queue.title")
        self.setWindowTitle(f"{title} — {name}" if name else title)
        hint = tr("scene_queue.single_folder_hint")
        target = (
            tr("scene_queue.single_folder_target", name) if name else tr("scene_queue.single_folder_unsaved")
        )
        self.single_folder_check.setToolTip(f"{hint}\n\n{target}")

    # ── 오른쪽 클릭 메뉴 ─────────────────────────────────

    def context_menu(self, row: int) -> QMenu:
        """`row`번 씬의 메뉴 — 편집 · 삭제. 행이 없으면 빈 메뉴."""
        tr = self._i18n.get_text
        menu = QMenu(self)
        if 0 <= row < len(self._set.scenes):
            # 메뉴가 닫힌 다음에 연다 (메뉴의 이벤트 루프 안에서 창을 띄우지 않는다)
            menu.addAction(
                tr("scene_queue.menu_edit"), lambda: QTimer.singleShot(0, lambda: self.edit_prompt(row))
            )
            delete = menu.addAction(tr("scene_queue.remove"), self.remove_selected)
            delete.setShortcut(Qt.Key.Key_Delete)  # 표시용 — 실제 키는 SceneTable이 받는다
        return menu

    def _on_context_menu(self, pos: QPoint) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        self.table.selectRow(row)  # 메뉴의 `삭제`가 지울 행을 오른쪽 클릭한 행으로 맞춘다
        self.context_menu(row).exec(self.table.viewport().mapToGlobal(pos))

    # ── 끌어다 놓기 (표 밖) ───────────────────────────────

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        if dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        if dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt 콜백 이름)
        paths = dropped_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.import_files(paths)

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
            f"{tr('scene_queue.import_filter')} (*.json *.txt *.png *.webp);;"
            f"{tr('menu.settings_file_filter')} (*.json *.txt);;"
            f"{tr('scene_queue.image_filter')} (*.png *.webp);;"
            f"{tr('menu.all_files_filter')} (*)",
        )
        if paths:
            self.import_files(paths)

    def _on_save_set(self) -> bool:
        """세트를 파일로 저장한다. 저장했으면 True (취소·실패면 False)."""
        tr = self._i18n.get_text
        default_name = f"{self._set.set_name or 'scenes'}.json"
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("scene_queue.save_set"),
            str(Path(self._default_dir) / default_name),
            self._file_filter(),
        )
        if not path:
            return False
        return self.save_set(Path(path))

    def save_set(self, path: Path) -> bool:
        """세트를 `path`에 저장하고, 그 파일 이름을 세트 이름으로 삼는다."""
        tr = self._i18n.get_text
        previous = self._set.set_name
        self._set.set_name = Path(path).stem
        try:
            save_scene_set(Path(path), self._set)
        except OSError as e:
            self._set.set_name = previous
            QMessageBox.warning(self, tr("errors.title"), f"{tr('scene_queue.save_set')}\n\n{e}")
            return False
        self._persist()
        self._refresh_set_name()
        return True

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
        # 세트 이름은 파일 안의 값이 아니라 지금 고른 파일의 이름이다 (이름을 바꿔 복사한 파일 등)
        loaded.set_name = Path(path).stem
        self._set = loaded
        for check, value in (
            (self.interleave_check, loaded.interleave),
            (self.single_folder_check, loaded.single_folder),
        ):
            check.blockSignals(True)
            check.setChecked(value)
            check.blockSignals(False)
        self._refresh_set_name()
        self._persist()
        self._refill(select=0)
        return True

    def _on_run(self) -> None:
        # 한 폴더 저장은 세트 이름이 폴더 이름이다 — 저장한 적 없으면 먼저 저장하게 한다
        if self._set.single_folder and not self._set.set_name.strip():
            tr = self._i18n.get_text
            answer = QMessageBox.question(
                self,
                tr("errors.warning"),
                tr("scene_queue.save_before_run"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes or not self._on_save_set():
                return
        self.run_requested.emit(self._set.model_copy(deep=True))

    # ── i18n ─────────────────────────────────────────────

    def retranslate(self) -> None:
        tr = self._i18n.get_text
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
        self.single_folder_check.setText(tr("scene_queue.single_folder"))
        self.hint_label.setText(tr("scene_queue.hint"))
        self.save_set_button.setText(tr("scene_queue.save_set"))
        self.load_set_button.setText(tr("scene_queue.load_set"))
        self.run_button.setText(tr("scene_queue.run"))
        self.close_button.setText(tr("scene_queue.close"))
        self._refresh_set_name()
        self._refresh_total()


def _summary(scene: Scene) -> str:
    """표에 보일 프롬프트 한 줄 (줄바꿈을 지우고 앞부분만)."""
    prompt = " ".join(str(scene.settings.get("prompt", "")).split())
    return prompt if len(prompt) <= _SUMMARY_LENGTH else prompt[: _SUMMARY_LENGTH - 1] + "…"


__all__ = ["SceneQueueDialog", "ScenePromptDialog", "SceneTable"]
