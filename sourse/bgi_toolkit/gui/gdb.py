from __future__ import annotations
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr

import json
from pathlib import Path

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..gdb_tool import (
    GdbFormatError,
    _default_decode_output,
    _default_encode_output,
    _pretty_json,
    _summary,
    decode_gdb,
    encode_gdb,
)


from .paths import PathLineEdit


class DropPathEdit(PathLineEdit):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        ui_call(self, 'setPlaceholderText', tr('可直接拖入文件路径...'))

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                ui_call(self, 'setText', path)
                event.acceptProposedAction()
                return
        event.ignore()


class GdbWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, operation: str, input_path: str, output_path: str = "") -> None:
        super().__init__()
        self.operation = operation
        self.input_path = Path(input_path.strip())
        self.output_path = Path(output_path.strip()) if output_path.strip() else None

    def log(self, text: str) -> None:
        self.log_signal.emit(text)

    def run(self) -> None:
        try:
            if not self.input_path.is_file():
                raise FileNotFoundError(tr('输入文件不存在：{0}', self.input_path))

            if self.operation == "decode":
                source = self.input_path.read_bytes()
                document = decode_gdb(source)
                output = self.output_path or _default_decode_output(self.input_path)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(_pretty_json(document) + "\n", encoding="utf-8", newline="\n")
                self.log(_summary(document, len(source)))
                self.finished_signal.emit(True, tr('已解码：{0}', output))
                return

            if self.operation == "encode":
                document = json.loads(self.input_path.read_text(encoding="utf-8-sig"))
                encoded = encode_gdb(document)
                output = self.output_path or _default_encode_output(self.input_path)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(encoded)
                self.log(tr('输出大小：{0} bytes', len(encoded)))
                self.finished_signal.emit(True, tr('已重建：{0}', output))
                return

            source = self.input_path.read_bytes()
            document = decode_gdb(source)
            if self.operation == "inspect":
                self.log(_summary(document, len(source)))
                self.finished_signal.emit(True, tr('检查完成'))
                return

            rebuilt = encode_gdb(document)
            if rebuilt != source:
                difference = next(
                    (index for index, pair in enumerate(zip(source, rebuilt)) if pair[0] != pair[1]),
                    min(len(source), len(rebuilt)),
                )
                raise GdbFormatError(
                    tr('往返结果不一致：原始 {0} bytes，重建 {1} bytes，首个差异位置 0x{2:x}', len(source), len(rebuilt), difference)
                )
            self.finished_signal.emit(True, tr('验证通过：字节级一致（{0} bytes）', len(source)))
        except (GdbFormatError, OSError, UnicodeError, json.JSONDecodeError) as error:
            self.log(tr('错误：{0}', error))
            self.finished_signal.emit(False, str(error))


class GdbToolWidget(QWidget):
    """GUI for lossless BURIKO GDB and semantic JSON conversion."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MainBackground")
        self.worker: GdbWorker | None = None
        self._path_edits: list[QLineEdit] = []
        self._action_buttons: list[QPushButton] = []
        self._auto_output_bindings: dict[QLineEdit, tuple[QLineEdit, str]] = {}
        self._manual_output_edits: set[QLineEdit] = set()
        self._updating_auto_output = False
        self._build_ui()

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 8, 12, 12)
        main_layout.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setHandleWidth(2)

        self.task_tabs = QTabWidget()
        ui_call(self.task_tabs, 'addTab', self._build_decode_page(), tr('解码 JSON'))
        ui_call(self.task_tabs, 'addTab', self._build_encode_page(), tr('重建 GDB'))
        ui_call(self.task_tabs, 'addTab', self._build_inspect_page(), tr('检查与验证'))
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
        header_layout.addWidget(ui_widget(QLabel, tr('运行日志 (Log)')))
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
        splitter.setSizes([430, 500])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        main_layout.addWidget(splitter)

    def _page(self, title: str, description: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        card = QFrame()
        card.setObjectName("CardFrame")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 14)
        card_layout.setSpacing(8)
        heading = ui_widget(QLabel, title)
        heading.setObjectName("SectionTitle")
        card_layout.addWidget(heading)
        hint = ui_widget(QLabel, description)
        hint.setObjectName("TipLabel")
        hint.setWordWrap(True)
        card_layout.addWidget(hint)
        layout.addWidget(card)
        layout.addStretch()
        return page, card_layout

    def _path_row(self, layout: QVBoxLayout, label: str, file_filter: str, save: bool = False) -> QLineEdit:
        layout.addWidget(ui_widget(QLabel, label))
        row = QHBoxLayout()
        edit = DropPathEdit()
        button = ui_widget(QPushButton, tr('浏览'))
        button.setObjectName("SecondaryButton")
        button.clicked.connect(lambda: self._browse(edit, file_filter, save))
        row.addWidget(edit, 1)
        row.addWidget(button)
        layout.addLayout(row)
        self._path_edits.append(edit)
        return edit

    def _run_button(self, layout: QVBoxLayout, text: str, operation: str, input_edit: QLineEdit, output_edit: QLineEdit | None = None) -> None:
        button = ui_widget(QPushButton, text)
        button.setObjectName("PrimaryButton")
        button.clicked.connect(lambda: self._start(operation, input_edit, output_edit))
        layout.addWidget(button)
        self._action_buttons.append(button)

    def _build_decode_page(self) -> QWidget:
        page, layout = self._page(tr('GDB 解码为 JSON'), tr('读取 BURIKO GDB 存档，生成可编辑的 UTF-8 JSON。输出路径会随输入文件自动填写。'))
        input_edit = self._path_row(layout, tr('GDB 输入文件'), tr('GDB 文件 (*.gdb);;所有文件 (*.*)'))
        output_edit = self._path_row(layout, tr('JSON 输出文件'), tr('JSON 文件 (*.json);;所有文件 (*.*)'), save=True)
        self._bind_auto_output(input_edit, output_edit, "decode")
        self._run_button(layout, tr('执行 GDB 解码'), "decode", input_edit, output_edit)
        return page

    def _build_encode_page(self) -> QWidget:
        page, layout = self._page(tr('JSON 重建为 GDB'), tr('读取语义 JSON 并重建 SDC 封装的旧版或 BURIKO GDB 3.00 存档。输出路径会随输入文件自动填写。'))
        input_edit = self._path_row(layout, tr('JSON 输入文件'), tr('JSON 文件 (*.json);;所有文件 (*.*)'))
        output_edit = self._path_row(layout, tr('GDB 输出文件'), tr('GDB 文件 (*.gdb);;所有文件 (*.*)'), save=True)
        self._bind_auto_output(input_edit, output_edit, "encode")
        self._run_button(layout, tr('执行 GDB 重建'), "encode", input_edit, output_edit)
        return page

    def _build_inspect_page(self) -> QWidget:
        page, layout = self._page(tr('GDB 检查与验证'), tr('检查存档结构，或执行解码与重建并进行字节级比较。'))
        input_edit = self._path_row(layout, tr('GDB 输入文件'), tr('GDB 文件 (*.gdb);;所有文件 (*.*)'))
        row = QHBoxLayout()
        inspect_button = ui_widget(QPushButton, tr('查看摘要'))
        inspect_button.setObjectName("SecondaryButton")
        inspect_button.clicked.connect(lambda: self._start("inspect", input_edit))
        verify_button = ui_widget(QPushButton, tr('验证字节级往返'))
        verify_button.setObjectName("PrimaryButton")
        verify_button.clicked.connect(lambda: self._start("verify", input_edit))
        row.addWidget(inspect_button)
        row.addWidget(verify_button)
        layout.addLayout(row)
        self._action_buttons.extend((inspect_button, verify_button))
        return page

    def _browse(self, edit: QLineEdit, file_filter: str, save: bool) -> None:
        current = edit.text().strip()
        if save:
            path, _ = QFileDialog.getSaveFileName(self, tr('选择输出文件'), current or str(Path.cwd()), file_filter)
        else:
            path, _ = QFileDialog.getOpenFileName(self, tr('选择输入文件'), current or str(Path.cwd()), file_filter)
        if path:
            ui_call(edit, 'setText', path)

    def _bind_auto_output(self, input_edit: QLineEdit, output_edit: QLineEdit, operation: str) -> None:
        self._auto_output_bindings[output_edit] = (input_edit, operation)
        input_edit.textChanged.connect(lambda _text, target=output_edit: self._sync_auto_output(target))
        output_edit.textChanged.connect(lambda _text, target=output_edit: self._output_path_changed(target))

    def _output_path_changed(self, output_edit: QLineEdit) -> None:
        if self._updating_auto_output:
            return
        if output_edit.text().strip():
            self._manual_output_edits.add(output_edit)
        else:
            self._manual_output_edits.discard(output_edit)
            self._sync_auto_output(output_edit)

    def _sync_auto_output(self, output_edit: QLineEdit) -> None:
        if output_edit in self._manual_output_edits:
            return
        input_edit, operation = self._auto_output_bindings[output_edit]
        input_text = input_edit.text().strip()
        if not input_text:
            return
        source = Path(input_text)
        output = _default_decode_output(source) if operation == "decode" else _default_encode_output(source)
        self._updating_auto_output = True
        ui_call(output_edit, 'setText', str(output))
        self._updating_auto_output = False

    def _start(self, operation: str, input_edit: QLineEdit, output_edit: QLineEdit | None = None) -> None:
        input_path = input_edit.text().strip()
        if not input_path:
            self.log_view.append(tr('请先选择输入文件。'))
            return
        if self.worker is not None and self.worker.isRunning():
            self.log_view.append(tr('已有任务正在运行，请等待完成。'))
            return
        self._set_enabled(False)
        self.log_view.append(tr('开始任务：{0}，输入：{1}', operation, input_path))
        self.worker = GdbWorker(operation, input_path, output_edit.text() if output_edit else "")
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

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.worker is not None and self.worker.isRunning():
            self.worker.wait(1000)
        super().closeEvent(event)
