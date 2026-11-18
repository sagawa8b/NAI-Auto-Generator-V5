"""아티스트 조합 편집 창 — 조합 폴더의 그룹을 만들고 고친다.

지금까지는 `{"그룹": ["artist:a", ...]}` JSON을 텍스트 편집기로 직접 써야 했다. 여기서는
왼쪽에서 그룹을 고르고 오른쪽에서 **한 줄에 태그 하나씩** 적는다. 읽기·쓰기 규칙은
`core/artist_combo_store.py`에 있고, 이 창은 그것을 화면에 올리기만 한다.

창은 모드리스다 — 띄워 둔 채 프롬프트에 `{artist:이름}`을 넣고 생성해 볼 수 있다.
저장할 때마다 `groups_changed`를 내고, 메인 창이 그걸 받아 생성 엔진의 조합을 다시 읽는다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.artist_combo_store import (
    DEFAULT_FILE,
    ComboGroup,
    ComboStoreError,
    delete_group,
    load_groups,
    parse_tags,
    placeholder,
    save_group,
)
from ..core.i18n.manager import I18nManager
from .widgets.window_chrome import enable_window_controls


class ArtistComboDialog(QDialog):
    """그룹 목록 + 태그 편집.

    Signals:
        groups_changed(): 파일이 바뀌었다 — 생성 엔진이 조합을 다시 읽어야 한다.
        insert_requested(str): 이 자리표시자를 프롬프트에 넣어 달라.
    """

    groups_changed = Signal()
    insert_requested = Signal(str)

    def __init__(self, combos_dir: str, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dir = combos_dir
        self._i18n = i18n
        self._groups: list[ComboGroup] = []
        #: 지금 편집 중인 그룹 (새 그룹이면 아직 파일에 없다).
        self._current: ComboGroup | None = None
        #: 편집 중인 그룹의 저장된 이름. None이면 아직 저장한 적 없는 새 그룹이다.
        self._original_name: str | None = None

        self.group_list = QListWidget()
        self.group_list.currentRowChanged.connect(self._on_group_selected)
        self.new_button = QPushButton()
        self.new_button.clicked.connect(self.new_group)
        self.delete_button = QPushButton()
        self.delete_button.clicked.connect(self.delete_current)

        self.name_label = QLabel()
        self.name_edit = QLineEdit()
        self.tags_label = QLabel()
        self.tags_edit = QPlainTextEdit()
        self.tags_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.tags_edit.textChanged.connect(self._refresh_status)
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: gray;")
        self.status_label.setWordWrap(True)

        self.save_button = QPushButton()
        self.save_button.clicked.connect(self.save_current)
        self.insert_button = QPushButton()
        self.insert_button.clicked.connect(lambda: self._request_insert(loop=False))
        self.insert_loop_button = QPushButton()
        self.insert_loop_button.clicked.connect(lambda: self._request_insert(loop=True))
        self.close_button = QPushButton()
        self.close_button.clicked.connect(self.close)

        left = QVBoxLayout()
        left.addWidget(self.group_list, 1)
        left_buttons = QHBoxLayout()
        left_buttons.addWidget(self.new_button)
        left_buttons.addWidget(self.delete_button)
        left.addLayout(left_buttons)

        right = QVBoxLayout()
        name_row = QHBoxLayout()
        name_row.addWidget(self.name_label)
        name_row.addWidget(self.name_edit, 1)
        right.addLayout(name_row)
        right.addWidget(self.tags_label)
        right.addWidget(self.tags_edit, 1)
        right.addWidget(self.status_label)
        action_row = QHBoxLayout()
        action_row.addWidget(self.save_button)
        action_row.addWidget(self.insert_button)
        action_row.addWidget(self.insert_loop_button)
        action_row.addStretch(1)
        action_row.addWidget(self.close_button)
        right.addLayout(action_row)

        layout = QHBoxLayout(self)
        layout.addLayout(left, 1)
        layout.addLayout(right, 2)

        self.resize(720, 460)
        enable_window_controls(self)
        self.retranslate()
        self.reload()

    # ── 목록 ─────────────────────────────────────────────

    def set_directory(self, combos_dir: str) -> None:
        """옵션에서 조합 폴더를 바꿨을 때 — 그 폴더의 그룹으로 다시 채운다."""
        if combos_dir != self._dir:
            self._dir = combos_dir
            self.reload()

    def reload(self, select: str | None = None) -> None:
        """폴더를 다시 읽는다. `select`가 있으면 그 이름의 그룹을 고른다."""
        self._groups = load_groups(self._dir)
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for group in self._groups:
            item = QListWidgetItem(f"{group.name}  ({len(group.tags)})")
            item.setData(Qt.ItemDataRole.UserRole, group.name)
            item.setToolTip(group.file)
            self.group_list.addItem(item)
        self.group_list.blockSignals(False)

        names = [group.name for group in self._groups]
        row = names.index(select) if select in names else (0 if names else -1)
        self.group_list.setCurrentRow(row)
        if row < 0:
            self._show_group(None, None)
        else:
            self._on_group_selected(row)

    def group_names(self) -> list[str]:
        return [group.name for group in self._groups]

    def _on_group_selected(self, row: int) -> None:
        if 0 <= row < len(self._groups):
            group = self._groups[row]
            self._show_group(group, group.name)

    def _show_group(self, group: ComboGroup | None, original_name: str | None) -> None:
        self._current = group
        self._original_name = original_name
        editable = group is not None
        for widget in (self.name_edit, self.tags_edit, self.save_button):
            widget.setEnabled(editable)
        self.name_edit.setText(group.name if group else "")
        self.tags_edit.setPlainText("\n".join(group.tags) if group else "")
        self._refresh_status()

    # ── 조작 ─────────────────────────────────────────────

    def new_group(self) -> None:
        """새 그룹을 편집 칸에 올린다. `저장`을 눌러야 파일에 들어간다."""
        tr = self._i18n.get_text
        name, accepted = QInputDialog.getText(
            self, tr("artist_combo.new_group"), tr("artist_combo.name_prompt")
        )
        if not accepted or not name.strip():
            return
        self.group_list.blockSignals(True)
        self.group_list.setCurrentRow(-1)
        self.group_list.blockSignals(False)
        self._show_group(ComboGroup(name=name.strip(), tags=[], file=DEFAULT_FILE), None)
        self.tags_edit.setFocus()

    def save_current(self) -> bool:
        """편집 칸의 내용을 파일에 쓴다. 저장했으면 True."""
        if self._current is None:
            return False
        group = ComboGroup(
            name=self.name_edit.text().strip(),
            tags=parse_tags(self.tags_edit.toPlainText()),
            file=self._current.file,
        )
        try:
            save_group(self._dir, group, original_name=self._original_name)
        except ComboStoreError as e:
            self._warn(e)
            return False
        self.reload(select=group.name)
        self.groups_changed.emit()
        self.status_label.setText(self._i18n.get_text("artist_combo.saved", group.name))
        return True

    def delete_current(self) -> bool:
        """고른 그룹을 파일에서 뺀다. 뺐으면 True."""
        row = self.group_list.currentRow()
        if not 0 <= row < len(self._groups):
            return False
        group = self._groups[row]
        tr = self._i18n.get_text
        answer = QMessageBox.question(
            self,
            tr("artist_combo.delete_group"),
            tr("artist_combo.delete_confirm", group.name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            delete_group(self._dir, group)
        except ComboStoreError as e:
            self._warn(e)
            return False
        self.reload()
        self.groups_changed.emit()
        return True

    def _request_insert(self, *, loop: bool) -> None:
        """저장된 그룹의 자리표시자를 프롬프트로 보낸다.

        저장하지 않은 이름으로는 보내지 않는다 — 파일에 없는 그룹은 생성할 때 치환되지
        않고 `{artist:이름}`이 글자 그대로 서버에 간다.
        """
        if self._original_name is None:
            tr = self._i18n.get_text
            QMessageBox.information(self, tr("artist_combo.editor_title"), tr("artist_combo.save_first"))
            return
        self.insert_requested.emit(placeholder(self._original_name, loop=loop))

    def _warn(self, error: ComboStoreError) -> None:
        tr = self._i18n.get_text
        QMessageBox.warning(
            self,
            tr("errors.title"),
            tr(f"artist_combo.err_{error.code}", *error.args_for_message),
        )

    def _refresh_status(self) -> None:
        if self._current is None:
            self.status_label.setText(self._i18n.get_text("artist_combo.empty_hint"))
            return
        count = len(parse_tags(self.tags_edit.toPlainText()))
        self.status_label.setText(self._i18n.get_text("artist_combo.tag_count", count))

    # ── i18n ─────────────────────────────────────────────

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.setWindowTitle(tr("artist_combo.editor_title"))
        self.new_button.setText(tr("artist_combo.new_group"))
        self.delete_button.setText(tr("artist_combo.delete_group"))
        self.name_label.setText(tr("artist_combo.name"))
        self.name_edit.setToolTip(tr("artist_combo.name_hint"))
        self.tags_label.setText(tr("artist_combo.tags"))
        self.save_button.setText(tr("artist_combo.save"))
        self.insert_button.setText(tr("artist_combo.insert_random"))
        self.insert_button.setToolTip(tr("artist_combo.insert_random_hint"))
        self.insert_loop_button.setText(tr("artist_combo.insert_loop"))
        self.insert_loop_button.setToolTip(tr("artist_combo.insert_loop_hint"))
        self.close_button.setText(tr("artist_combo.close"))
        self._refresh_status()


__all__ = ["ArtistComboDialog"]
