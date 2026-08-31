"""태그 Options_Page (KEY=`"tags"`, Req 7.1, 7.2, 7.4–7.6).

두 가지를 다룬다: 태그 자동완성 DB와 **WD14 태거 모델**(폴더·모델·내려받기). V4의
`태그 자동완성` 화면과 같은 구성이다 — 태거 모델은 태그 기능이라 폴더 화면이 아니라
여기에 둔다. 모델 내려받기는 수백 MB를 받으므로 스레드에서 진행률과 함께 돌린다.

아래는 태그 자동완성 부분의 원래 설명이다.

경로 입력란 + `찾아보기` + 읽기 전용 상태 문구뿐이다. 입력란이 비면 앱에 동봉된 기본 태그 DB를
쓴다 (`core.tag_completer.resolve_database_path`). 상태 문구는 경로가 바뀔 때마다 **임시**
`TagCompleter`로 시험 로드해서 만든다 — 라이브 완성기는 저장 후 Main_Window가 다시 로드한다
(Req 7.3). 시험 로드가 실패해도 저장은 막지 않는다: 상태 문구만 비활성으로 바뀌고 나머지 항목은
그대로 반영된다 (Req 7.5).

시험 로드 결과는 `_status`에 상태로 저장하고 문구 생성은 `retranslate`가 담당한다. 언어가 바뀔
때마다 태그 DB를 다시 읽지 않기 위해서다 (파일이 수십만 행일 수 있다).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.i18n.manager import I18nManager
from ...core.settings.schema import AppSettings, default_wd14_dir
from ...core.tag_completer import TagCompleter, resolve_database_path
from ...core.wd14_tagger import (
    KNOWN_MODELS,
    WD14DownloadCancelled,
    WD14Error,
    download_model,
    installed_models,
    runtime_error,
)
from . import OptionsPage, register_page

logger = logging.getLogger(__name__)

#: 찾아보기 파일 필터 (Req 7.2). 확장자는 `TagCompleter`가 읽는 두 형식이다.
TAG_FILE_FILTER = "Tag database (*.csv *.json);;All files (*)"

#: 상태 문구 i18n 키 — 경로가 비었을 때(= 내장 DB) / 로드 성공 / 로드 실패.
STATUS_BUNDLED_KEY = "options.tags_bundled"
STATUS_EMPTY_KEY = "options.tags_disabled_empty"
STATUS_LOADED_KEY = "options.tags_loaded"
STATUS_ERROR_KEY = "options.tags_disabled_error"

__all__ = [
    "ModelDownloadWorker",
    "STATUS_BUNDLED_KEY",
    "STATUS_EMPTY_KEY",
    "STATUS_ERROR_KEY",
    "STATUS_LOADED_KEY",
    "TAG_FILE_FILTER",
    "TagsPage",
]


class ModelDownloadWorker(QThread):
    """WD14 모델 한 벌을 내려받는 스레드.

    UI 스레드에서 받으면 수백 MB 동안 창이 얼어붙는다. 취소는 플래그로 알리고
    (`cancel()`), 받던 파일은 `core.wd14_tagger`가 지운다.
    """

    progress = Signal(int, int)  # (받은 바이트, 전체 바이트 — 모르면 0)
    finished_ok = Signal(str)  # 모델 이름
    failed = Signal(str)  # 오류 문구
    cancelled = Signal()

    def __init__(self, name: str, directory: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._directory = directory
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            download_model(
                self._name,
                self._directory,
                on_progress=lambda received, total: self.progress.emit(received, total),
                should_cancel=lambda: self._cancel,
            )
        except WD14DownloadCancelled:
            self.cancelled.emit()
        except WD14Error as e:
            self.failed.emit(str(e))
        except Exception as e:  # 예상 못 한 오류로 옵션 창이 죽지 않도록
            logger.exception("WD14 model download failed")
            self.failed.emit(str(e))
        else:
            self.finished_ok.emit(self._name)


@register_page
class TagsPage(OptionsPage):
    """태그 자동완성 DB(Req 7.1) + WD14 태거 모델 폴더·모델·내려받기."""

    KEY = "tags"

    def __init__(self, i18n: I18nManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._i18n = i18n
        #: 마지막 시험 로드 결과: (상태 키, 태그 개수). 문구는 여기서 만들어진다.
        self._status: tuple[str, int] = (STATUS_EMPTY_KEY, 0)
        #: 시험 로드를 두 번 하지 않기 위한 직전 입력값.
        self._probed_text: str | None = None
        #: 진행 중인 모델 내려받기 (없으면 None).
        self._download_worker: ModelDownloadWorker | None = None
        self._progress_dialog: QProgressDialog | None = None

        layout = QVBoxLayout(self)

        self.enabled_check = QCheckBox(self)
        layout.addWidget(self.enabled_check)

        form = QFormLayout()
        self.path_label = QLabel(self)
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(self)
        self.browse_button = QPushButton(self)
        self.browse_button.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)
        form.addRow(self.path_label, path_row)
        layout.addLayout(form)

        self.status_label = QLabel(self)  # 읽기 전용 (Req 7.1)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(self.status_label)

        # ── WD14 태거 모델 (V4의 `단부루 태거 모델`과 같은 자리) ──────────
        layout.addSpacing(12)
        self.wd14_title = QLabel(self)
        self.wd14_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.wd14_title)

        wd14_form = QFormLayout()

        self.wd14_dir_label = QLabel(self)
        wd14_dir_row = QHBoxLayout()
        self.wd14_dir_edit = QLineEdit(self)
        self.wd14_dir_edit.setPlaceholderText(str(default_wd14_dir()))
        self.wd14_browse_button = QPushButton(self)
        self.wd14_browse_button.clicked.connect(self._browse_wd14_dir)
        wd14_dir_row.addWidget(self.wd14_dir_edit, 1)
        wd14_dir_row.addWidget(self.wd14_browse_button)
        wd14_form.addRow(self.wd14_dir_label, wd14_dir_row)

        self.wd14_model_label = QLabel(self)
        model_row = QHBoxLayout()
        self.wd14_model_combo = QComboBox(self)
        self.wd14_download_button = QPushButton(self)
        self.wd14_download_button.clicked.connect(self._download_model)
        model_row.addWidget(self.wd14_model_combo, 1)
        model_row.addWidget(self.wd14_download_button)
        wd14_form.addRow(self.wd14_model_label, model_row)
        layout.addLayout(wd14_form)

        self.wd14_status_label = QLabel(self)
        self.wd14_status_label.setWordWrap(True)
        layout.addWidget(self.wd14_status_label)

        layout.addStretch(1)

        # 경로가 바뀔 때마다 시험 로드로 상태 문구를 갱신한다 (Req 7.4–7.6).
        self.path_edit.textChanged.connect(self._refresh_status)
        # 모델 폴더가 바뀌면 그 폴더에 있는 모델로 목록을 다시 채운다 (V4와 같다).
        self.wd14_dir_edit.textChanged.connect(self._refresh_models)
        self.wd14_model_combo.currentIndexChanged.connect(self._refresh_wd14_status)

        self.retranslate()

    # ── OptionsPage 계약 ────────────────────────────────────────────────

    def load(self, draft: AppSettings) -> None:
        self.enabled_check.setChecked(draft.tag_autocomplete_enabled)
        self.path_edit.setText(draft.tag_database_path)
        self.wd14_dir_edit.setText(draft.wd14_dir)
        self._refresh_models(selected=draft.wd14_model)
        # 값이 그대로면 textChanged가 오지 않으므로 직접 갱신한다.
        self._refresh_status()

    def commit(self, draft: AppSettings) -> None:
        """공백만 남은 입력은 기본값으로 정규화한다 (태그 DB는 내장본, 모델 폴더는 기본 폴더)."""
        draft.tag_autocomplete_enabled = self.enabled_check.isChecked()
        draft.tag_database_path = self.path_edit.text().strip()
        draft.wd14_dir = self.wd14_dir_edit.text().strip() or str(default_wd14_dir())
        draft.wd14_model = self.wd14_model_combo.currentData() or ""

    def retranslate(self) -> None:
        tr = self._i18n.get_text
        self.enabled_check.setText(tr("options.tags_autocomplete_enabled"))
        self.path_label.setText(tr("options.tag_database_path"))
        self.browse_button.setText(tr("options.browse"))
        key, count = self._status
        self.status_label.setText(
            tr(key, count) if key in (STATUS_LOADED_KEY, STATUS_BUNDLED_KEY) else tr(key)
        )
        self.wd14_title.setText(tr("options.wd14_title"))
        self.wd14_dir_label.setText(tr("options.folder_wd14_dir"))
        self.wd14_model_label.setText(tr("options.wd14_model"))
        self.wd14_browse_button.setText(tr("options.browse"))
        self.wd14_download_button.setText(tr("options.wd14_download"))
        self._relabel_models()
        self._refresh_wd14_status()

    # ── 내부 ────────────────────────────────────────────────────────────

    def _browse(self) -> None:
        """CSV·JSON을 대상으로 하는 파일 선택 다이얼로그 (Req 7.2)."""
        tr = self._i18n.get_text
        current = self.path_edit.text().strip()
        start_dir = str(Path(current).parent) if current else ""
        chosen, _filter = QFileDialog.getOpenFileName(
            self,
            tr("options.choose_file", tr("options.tag_database_path")),
            start_dir,
            TAG_FILE_FILTER,
        )
        if chosen:
            self.path_edit.setText(str(Path(chosen).resolve()))

    # ── WD14 태거 모델 ──────────────────────────────────────────────────

    def _wd14_dir(self) -> Path:
        """입력란이 가리키는 폴더 (비어 있으면 기본 폴더)."""
        text = self.wd14_dir_edit.text().strip()
        return Path(text) if text else default_wd14_dir()

    def _browse_wd14_dir(self) -> None:
        tr = self._i18n.get_text
        chosen = QFileDialog.getExistingDirectory(
            self,
            tr("options.choose_folder", tr("options.folder_wd14_dir")),
            str(self._wd14_dir()),
        )
        if chosen:
            self.wd14_dir_edit.setText(str(Path(chosen).resolve()))

    def _refresh_models(self, selected: str | None = None) -> None:
        """폴더에 있는 모델 + 내려받을 수 있는 모델로 콤보를 다시 채운다.

        항목의 `data`는 모델 이름 그대로이고, 보이는 글자는 설치 여부에 따라
        `retranslate`(정확히는 `_relabel_models`)가 붙인다. 이름이 목록에 없는
        모델을 직접 넣어 둔 폴더도 그대로 고를 수 있다 (V4와 같다).
        """
        if selected is None:
            selected = self.wd14_model_combo.currentData() or ""
        installed = installed_models(self._wd14_dir())
        names = list(KNOWN_MODELS) + [name for name in installed if name not in KNOWN_MODELS]

        blocked = self.wd14_model_combo.blockSignals(True)
        self.wd14_model_combo.clear()
        for name in names:
            self.wd14_model_combo.addItem(name, name)
        self.wd14_model_combo.blockSignals(blocked)

        index = self.wd14_model_combo.findData(selected) if selected else -1
        if index < 0:
            # 고른 모델이 없으면 설치된 것 중 첫 번째를 고른다 — 바로 쓸 수 있는 쪽으로.
            first_installed = next((name for name in names if name in installed), None)
            index = self.wd14_model_combo.findData(first_installed) if first_installed else 0
        self.wd14_model_combo.setCurrentIndex(max(0, index))
        self._relabel_models()
        self._refresh_wd14_status()

    def _relabel_models(self) -> None:
        """설치된 모델에 `(설치됨)` 표시를 붙인다."""
        tr = self._i18n.get_text
        installed = set(installed_models(self._wd14_dir()))
        for index in range(self.wd14_model_combo.count()):
            name = self.wd14_model_combo.itemData(index)
            label = tr("options.wd14_model_installed", name) if name in installed else name
            self.wd14_model_combo.setItemText(index, label)

    def _refresh_wd14_status(self) -> None:
        """고른 모델이 설치돼 있는지 알려 주고, 내려받기 버튼을 열고 닫는다.

        onnxruntime을 쓸 수 없으면 모델이 있어도 태깅은 안 된다 — 모델을 다 받아 놓고
        도구 창에서야 그 사실을 아는 일이 없도록 여기서 먼저 알린다.
        """
        tr = self._i18n.get_text
        name = self.wd14_model_combo.currentData() or ""
        installed = name in set(installed_models(self._wd14_dir()))
        busy = self._download_worker is not None
        failure = runtime_error()
        if failure:
            self.wd14_status_label.setText(tr("options.wd14_runtime_missing", failure))
        elif installed:
            self.wd14_status_label.setText(tr("options.wd14_installed", name))
        else:
            self.wd14_status_label.setText(tr("options.wd14_not_installed"))
        self.wd14_download_button.setEnabled(
            bool(name) and not installed and not busy and name in KNOWN_MODELS
        )

    def _download_model(self) -> None:
        """고른 모델을 내려받는다 (수백 MB — 스레드 + 진행률 창)."""
        tr = self._i18n.get_text
        name = self.wd14_model_combo.currentData() or ""
        if not name or self._download_worker is not None:
            return

        directory = self._wd14_dir()
        self._progress_dialog = QProgressDialog(
            tr("options.wd14_downloading", name), tr("options.cancel"), 0, 0, self
        )
        self._progress_dialog.setWindowTitle(tr("options.wd14_download"))
        self._progress_dialog.setMinimumDuration(0)
        self._progress_dialog.setAutoClose(False)
        self._progress_dialog.setAutoReset(False)

        worker = ModelDownloadWorker(name, directory, self)
        self._download_worker = worker
        worker.progress.connect(self._on_download_progress)
        worker.finished_ok.connect(self._on_download_done)
        worker.failed.connect(self._on_download_failed)
        worker.cancelled.connect(self._on_download_cancelled)
        self._progress_dialog.canceled.connect(worker.cancel)
        self._refresh_wd14_status()
        worker.start()
        self._progress_dialog.show()

    def _on_download_progress(self, received: int, total: int) -> None:
        if self._progress_dialog is None:
            return
        if total > 0:
            self._progress_dialog.setMaximum(total // 1024)
            self._progress_dialog.setValue(received // 1024)
        self._progress_dialog.setLabelText(
            self._i18n.get_text(
                "options.wd14_download_progress",
                self.wd14_model_combo.currentData() or "",
                received // (1 << 20),
                total // (1 << 20),
            )
        )

    def _finish_download(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.close()
            self._progress_dialog = None
        self._download_worker = None
        self._refresh_models()

    def _on_download_done(self, name: str) -> None:
        self._finish_download()
        tr = self._i18n.get_text
        index = self.wd14_model_combo.findData(name)
        if index >= 0:
            self.wd14_model_combo.setCurrentIndex(index)
        QMessageBox.information(self, tr("options.wd14_download"), tr("options.wd14_download_done", name))

    def _on_download_failed(self, message: str) -> None:
        self._finish_download()
        tr = self._i18n.get_text
        QMessageBox.warning(self, tr("errors.title"), f"{tr('options.wd14_download_failed')}\n\n{message}")

    def _on_download_cancelled(self) -> None:
        self._finish_download()

    # ── 태그 자동완성 ───────────────────────────────────────────────────

    def _refresh_status(self) -> None:
        """임시 `TagCompleter`로 시험 로드해 상태를 갱신한다 (Req 7.4–7.6)."""
        text = self.path_edit.text().strip()
        if self._probed_text == text:
            return
        self._probed_text = text
        self._status = self._probe(resolve_database_path(text), bundled=not text)
        self.retranslate()

    @staticmethod
    def _probe(path: Path, *, bundled: bool = False) -> tuple[str, int]:
        """경로 하나를 시험 로드한다. 어떤 파일이든 다이얼로그를 죽이면 안 된다 (Req 7.5).

        `bundled`는 입력란이 비어 앱에 동봉된 기본 DB를 쓰는 경우다 — 문구만 달라진다.
        """
        probe = TagCompleter(path)
        try:
            ok = probe.load()
        except Exception:  # 이진 파일 등 TagCompleter가 예상하지 못한 입력
            logger.debug("tag database probe failed: %s", path, exc_info=True)
            return (STATUS_ERROR_KEY, 0)
        if not ok:
            return (STATUS_EMPTY_KEY, 0) if bundled else (STATUS_ERROR_KEY, 0)
        return (STATUS_BUNDLED_KEY if bundled else STATUS_LOADED_KEY, probe.tag_count)
