from __future__ import annotations
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr

import os
import re
from pathlib import Path

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QSizePolicy,
    QWidget,
)

from ..anmdef_csv import AnimationFormatError, binary_to_csv, csv_to_binary


_ANMDEF_NAME = re.compile(r"^(?:animation|animationset)\d{3}$", re.IGNORECASE)


def _common_parent(paths: list[Path]) -> Path:
    if not paths:
        return Path.cwd()
    try:
        return Path(os.path.commonpath([str(path.resolve().parent) for path in paths]))
    except ValueError:
        return paths[0].resolve().parent


def _directory_files(directory: Path, operation: str) -> list[Path]:
    try:
        files = [path for path in directory.rglob("*") if path.is_file()]
    except OSError:
        return []
    if operation == "import":
        return [path for path in files if path.suffix.lower() == ".csv"]
    return [path for path in files if _ANMDEF_NAME.fullmatch(path.name)]


def _default_output_directory(source: Path, operation: str) -> Path:
    folder_name = "anmdef_csv" if operation == "export" else "anmdef_bin"
    return source / folder_name


def _default_single_output(source: Path, operation: str) -> Path:
    if operation == "export":
        return source.with_name(source.name + ".csv")
    if source.suffix.lower() == ".csv":
        return source.with_suffix("")
    return source.with_name(source.name + ".bin")


def _destination_for(source: Path, output_root: Path, common_parent: Path, operation: str) -> Path:
    try:
        relative = source.resolve().relative_to(common_parent)
    except ValueError:
        relative = Path(source.name)
    if operation == "export":
        relative = relative.with_name(relative.name + ".csv")
    elif relative.suffix.lower() == ".csv":
        relative = relative.with_suffix("")
    return output_root / relative


class AnmdefWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(
        self,
        operation: str,
        sources: list[str],
        output_target: str,
        encoding: str = "cp932",
        batch: bool = True,
    ) -> None:
        super().__init__()
        self.operation = operation
        self.sources = [Path(source).expanduser() for source in sources]
        self.output_target = Path(output_target).expanduser()
        self.encoding = encoding.strip() or "cp932"
        self.batch = batch

    def run(self) -> None:
        succeeded = 0
        try:
            if not self.sources:
                raise FileNotFoundError(tr('没有找到输入文件'))
            missing = [str(path) for path in self.sources if not path.is_file()]
            if missing:
                raise FileNotFoundError(tr('输入文件不存在：{0}', missing[0]))

            if self.batch:
                common_parent = _common_parent(self.sources)
                self.output_target.mkdir(parents=True, exist_ok=True)
            else:
                common_parent = self.sources[0].resolve().parent
                self.output_target.parent.mkdir(parents=True, exist_ok=True)

            for source in self.sources:
                destination = (
                    _destination_for(source, self.output_target, common_parent, self.operation)
                    if self.batch
                    else self.output_target
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if self.operation == "export":
                    binary_to_csv(source, destination, encoding=self.encoding)
                else:
                    csv_to_binary(source, destination)
                succeeded += 1
                self.log_signal.emit(tr('完成：{0} -> {1}', source, destination))

            label = tr('批处理') if self.batch else tr('单文件处理')
            self.finished_signal.emit(True, tr('{0}完成：{1} 个文件', label, succeeded))
        except (AnimationFormatError, OSError, UnicodeError) as error:
            self.log_signal.emit(tr('错误：{0}', error))
            self.finished_signal.emit(False, tr('处理失败，已完成 {0} 个文件', succeeded))


from .paths import PathLineEdit


class PathEdit(PathLineEdit):
    paths_dropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        ui_call(self, 'setPlaceholderText', tr('可直接拖入文件或文件夹...'))

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()
            return
        event.ignore()


class AnmdefToolWidget(QWidget):
    """GUI for single-file and directory-based animation-definition conversion."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MainBackground")
        self.worker: AnmdefWorker | None = None
        self._pages: dict[str, dict[str, QWidget]] = {}
        self._path_edits: list[QWidget] = []
        self._action_buttons: list[QPushButton] = []
        self._manual_output_edits: set[QLineEdit] = set()
        self._updating_auto_output = False
        self._build_ui()

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 8, 12, 12)
        main_layout.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.task_tabs = QTabWidget()
        ui_call(self.task_tabs, 'addTab', self._build_page('export'), tr('导出 CSV'))
        ui_call(self.task_tabs, 'addTab', self._build_page('import'), tr('导入二进制'))
        splitter.addWidget(self.task_tabs)

        log_frame = QFrame()
        log_frame.setObjectName("CardFrame")
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)
        log_header = QWidget()
        log_header.setObjectName("LogHeader")
        header_layout = QHBoxLayout(log_header)
        header_layout.setContentsMargins(10, 5, 10, 5)
        header_layout.addWidget(ui_widget(QLabel, tr('运行日志')))
        header_layout.addStretch()
        clear_button = ui_widget(QPushButton, tr('清除'))
        clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_button.setStyleSheet("border:none; font-weight:bold; color: #888;")
        clear_button.clicked.connect(self.log_view_clear)
        header_layout.addWidget(clear_button)
        log_layout.addWidget(log_header)
        self.log_view = QTextEdit()
        self.log_view.setObjectName("LogConsole")
        self.log_view.setReadOnly(True)
        log_layout.addWidget(self.log_view)
        splitter.addWidget(log_frame)
        splitter.setSizes([480, 500])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        main_layout.addWidget(splitter)

    def _build_page(self, operation: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        card = QFrame()
        card.setObjectName("CardFrame")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 14)
        card_layout.setSpacing(8)
        title = tr('Definition 导出为 CSV') if operation == "export" else tr('CSV 导入为 Definition')
        heading = ui_widget(QLabel, title)
        heading.setObjectName("SectionTitle")
        card_layout.addWidget(heading)
        hint = ui_widget(QLabel, tr('输入文件自动处理单个文件，输入目录自动递归批量处理。'))
        hint.setObjectName("TipLabel")
        hint.setWordWrap(True)
        card_layout.addWidget(hint)

        input_edit = PathEdit()
        input_edit.paths_dropped.connect(
            lambda paths, op=operation: self._set_dropped_input(op, paths)
        )
        card_layout.addWidget(ui_widget(QLabel, tr('输入文件或目录')))
        input_row = QHBoxLayout()
        input_row.addWidget(input_edit, 1)
        file_button = ui_widget(QPushButton, tr('文件'))
        file_button.setObjectName("SecondaryButton")
        file_button.clicked.connect(lambda _checked=False, op=operation: self._browse_file(op))
        input_row.addWidget(file_button)
        directory_button = ui_widget(QPushButton, tr('目录'))
        directory_button.setObjectName("SecondaryButton")
        directory_button.clicked.connect(
            lambda _checked=False, op=operation: self._browse_directory(op)
        )
        input_row.addWidget(directory_button)
        card_layout.addLayout(input_row)
        self._path_edits.append(input_edit)
        self._action_buttons.extend((file_button, directory_button))

        if operation == "export":
            encoding_row = QHBoxLayout()
            encoding_row.setSpacing(3)
            encoding_row.setContentsMargins(0, 0, 0, 0)
            encoding_row.addWidget(ui_widget(QLabel, tr('文本编码:')))
            encoding_edit = QComboBox()
            encoding_edit.setEditable(True)
            ui_call(encoding_edit, 'addItems', ['cp932', 'utf-8', 'gbk', 'big5'])
            encoding_edit.setCurrentText("cp932")
            encoding_edit.setMinimumWidth(130)
            encoding_edit.setMaximumWidth(180)
            encoding_edit.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            encoding_row.addWidget(encoding_edit)
            encoding_row.addStretch(1)
            card_layout.addLayout(encoding_row)
            self._path_edits.append(encoding_edit)
        else:
            encoding_edit = None

        mode_stack = QStackedWidget()
        single_page, single_output, single_run = self._build_single_page(operation)
        batch_page, batch_output, batch_run = self._build_batch_page(operation)
        mode_stack.addWidget(single_page)
        mode_stack.addWidget(batch_page)
        card_layout.addWidget(mode_stack)
        self._action_buttons.extend((single_run, batch_run))
        self._path_edits.extend((single_output, batch_output))

        input_edit.textChanged.connect(lambda _text, op=operation: self._sync_input_mode(op))
        self._register_output(single_output, operation)
        self._register_output(batch_output, operation)

        layout.addWidget(card)
        layout.addStretch()
        self._pages[operation] = {
            "input": input_edit,
            "mode_stack": mode_stack,
            "single_output": single_output,
            "batch_output": batch_output,
            "encoding": encoding_edit,
        }
        return page

    def _build_single_page(self, operation: str) -> tuple[QWidget, QLineEdit, QPushButton]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        output_label = tr('CSV 输出文件') if operation == "export" else tr('二进制输出文件')
        layout.addWidget(ui_widget(QLabel, output_label))
        output_edit, browse_button = self._output_selector(layout, file_mode=True)
        run_button = ui_widget(QPushButton, tr('执行单文件处理'))
        run_button.setObjectName("PrimaryButton")
        run_button.clicked.connect(lambda _checked=False, op=operation: self._start(op, False))
        layout.addWidget(run_button)
        return page, output_edit, run_button

    def _build_batch_page(self, operation: str) -> tuple[QWidget, QLineEdit, QPushButton]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(ui_widget(QLabel, tr('输出目录')))
        output_edit, browse_button = self._output_selector(layout, file_mode=False)
        run_button = ui_widget(QPushButton, tr('执行批量处理'))
        run_button.setObjectName("PrimaryButton")
        run_button.clicked.connect(lambda _checked=False, op=operation: self._start(op, True))
        layout.addWidget(run_button)
        return page, output_edit, run_button

    def _output_selector(self, layout: QVBoxLayout, file_mode: bool) -> tuple[QLineEdit, QPushButton]:
        row = QHBoxLayout()
        output_edit = PathLineEdit()
        ui_call(output_edit, 'setPlaceholderText', tr('留空会自动补充输出路径'))
        row.addWidget(output_edit, 1)
        browse_button = ui_widget(QPushButton, tr('浏览'))
        browse_button.setObjectName("SecondaryButton")
        browse_button.clicked.connect(
            lambda _checked=False, edit=output_edit, is_file=file_mode: self._browse_output(
                edit, is_file
            )
        )
        row.addWidget(browse_button)
        layout.addLayout(row)
        self._action_buttons.append(browse_button)
        return output_edit, browse_button

    def _register_output(self, edit: QLineEdit, operation: str) -> None:
        edit.textChanged.connect(
            lambda _text, target=edit, op=operation: self._output_changed(target, op)
        )

    def _browse_file(self, operation: str) -> None:
        filter_text = (
            tr('动画定义文件 (*);;所有文件 (*.*)')
            if operation == "export"
            else tr('语义 CSV (*.csv);;所有文件 (*.*)')
        )
        input_edit = self._pages[operation]["input"]
        current = input_edit.text().strip()
        start = str(Path(current).parent) if current else str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, tr('选择输入文件'), start, filter_text)
        if path:
            ui_call(input_edit, 'setText', str(Path(path)))

    def _browse_directory(self, operation: str) -> None:
        input_edit = self._pages[operation]["input"]
        path = QFileDialog.getExistingDirectory(
            self, tr('选择输入文件夹'), input_edit.text().strip() or str(Path.cwd())
        )
        if path:
            ui_call(input_edit, 'setText', str(Path(path)))

    def _browse_output(self, edit: QLineEdit, file_mode: bool) -> None:
        current = edit.text().strip()
        if file_mode:
            path, _ = QFileDialog.getSaveFileName(
                self, tr('选择输出文件'), current or str(Path.cwd()), tr('所有文件 (*.*)')
            )
        else:
            path = QFileDialog.getExistingDirectory(self, tr('选择输出文件夹'), current or str(Path.cwd()))
        if path:
            ui_call(edit, 'setText', str(Path(path)))

    def _set_dropped_input(self, operation: str, paths: list[str]) -> None:
        if paths:
            ui_call(self._pages[operation]['input'], 'setText', str(Path(paths[0])))
            if len(paths) > 1:
                self.append_log(tr('输入框只使用拖入的第一个路径；需要批处理时请拖入文件夹。'))

    def _input_is_batch(self, operation: str) -> bool:
        text = self._pages[operation]["input"].text().strip()
        return bool(text) and Path(text).is_dir()

    def _sync_input_mode(self, operation: str) -> None:
        page = self._pages[operation]
        input_text = page["input"].text().strip()
        batch = self._input_is_batch(operation)
        page["mode_stack"].setCurrentIndex(1 if batch else 0)
        if not input_text:
            return
        source = Path(input_text)
        output_edit = page["batch_output"] if batch else page["single_output"]
        output = (
            _default_output_directory(source, operation)
            if batch
            else _default_single_output(source, operation)
        )
        if output_edit not in self._manual_output_edits:
            self._updating_auto_output = True
            ui_call(output_edit, 'setText', str(output))
            self._updating_auto_output = False

    def _output_changed(self, edit: QLineEdit, operation: str) -> None:
        if self._updating_auto_output:
            return
        if edit.text().strip():
            self._manual_output_edits.add(edit)
        else:
            self._manual_output_edits.discard(edit)
            self._sync_input_mode(operation)

    def _start(self, operation: str, batch: bool) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.append_log(tr('已有任务正在运行，请等待完成。'))
            return
        page = self._pages[operation]
        input_text = page["input"].text().strip()
        if not input_text:
            self.append_log(tr('请先选择输入文件或文件夹。'))
            return
        source = Path(input_text)
        if batch:
            if not source.is_dir():
                self.append_log(tr('批量处理要求输入路径是文件夹。'))
                return
            sources = _directory_files(source, operation)
            if not sources:
                self.append_log(tr('输入文件夹中没有可处理的文件。'))
                return
            output = page["batch_output"].text().strip()
        else:
            if not source.is_file():
                self.append_log(tr('单文件处理要求输入路径是文件。'))
                return
            sources = [source]
            output = page["single_output"].text().strip()
        if not output:
            self._sync_input_mode(operation)
            output = (page["batch_output"] if batch else page["single_output"]).text().strip()
        encoding_edit = page["encoding"]
        encoding = encoding_edit.currentText().strip() if encoding_edit is not None else "cp932"
        self._set_enabled(False)
        self.append_log(tr('开始处理：{0} 个文件', len(sources)))
        self.worker = AnmdefWorker(operation, [str(path) for path in sources], output, encoding, batch)
        self.worker.log_signal.connect(self.append_log)
        self.worker.finished_signal.connect(self._finished)
        self.worker.start()

    def _set_enabled(self, enabled: bool) -> None:
        self.task_tabs.setEnabled(enabled)
        for edit in self._path_edits:
            edit.setEnabled(enabled)
        for button in self._action_buttons:
            button.setEnabled(enabled)

    def _finished(self, success: bool, message: str) -> None:
        self._set_enabled(True)
        self.append_log(message)
        self.worker = None

    def append_log(self, message: str) -> None:
        self.log_view.append(message)
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)

    def log_view_clear(self) -> None:
        ui_call(self.log_view, 'clear')

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            operation = "export" if self.task_tabs.currentIndex() == 0 else "import"
            self._set_dropped_input(operation, paths)
            event.acceptProposedAction()
            return
        event.ignore()

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.worker is not None and self.worker.isRunning():
            self.worker.wait(1000)
        super().closeEvent(event)
