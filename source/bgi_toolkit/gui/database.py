"""ScenarioDB / StringsDB conversion using the shared toolkit workspace."""
from __future__ import annotations
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr

from pathlib import Path
from PyQt6.QtCore import QSettings, QStandardPaths, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from ..db_text_tool import default_output, run_db_task
from .gdb import DropPathEdit


class DatabaseWorker(QThread):
    log_signal = pyqtSignal(str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, operation, source, output=None, *, kind=None, force=False, recursive=True, parent=None):
        super().__init__(parent)
        self.operation, self.source, self.output = operation, Path(source), Path(output) if output else None
        self.kind, self.force, self.recursive = kind, force, recursive

    def run(self):
        try:
            result = run_db_task(self.operation, self.source, self.output, kind=self.kind,
                force=self.force, recursive=self.recursive, log=self.log_signal.emit,
                cancelled=self.isInterruptionRequested)
            self.completed.emit(result)
        except Exception as error:
            self.log_signal.emit(tr('失败：{0}', error))
            self.failed.emit(str(error))


class DatabaseToolWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.worker = None
        self._pages = {}
        folder = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)) / 'bgi_toolkit'
        folder.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings(str(folder/'database.ini'), QSettings.Format.IniFormat)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.task_tabs = QTabWidget()
        for operation, title in (('export', tr('导出 JSON')), ('build', tr('重建数据库')), ('check', tr('检查与验证'))):
            ui_call(self.task_tabs, 'addTab', self._page(operation), title)
        splitter.addWidget(self.task_tabs)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName('LogConsole')
        splitter.addWidget(self.log_view)
        splitter.setSizes([390, 260])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        layout.addWidget(splitter)

    def _page(self, operation):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        descriptions = {
            'export': tr('读取 ARC 解包后的 ScenarioDB / StringsDB，导出可编辑的 UTF-8 JSON。选择目录可同时处理两种数据库。'),
            'build': tr('从 JSON 读取数据库类型，重建游戏资源。单文件默认生成 .rebuilt；目录批处理会在输出目录恢复原文件名。'),
            'check': tr('检查数据库结构，再执行“二进制 → JSON → 二进制”，确认重建结果与原文件逐字节一致。')}
        hint = ui_widget(QLabel, descriptions[operation])
        hint.setObjectName('TipLabel')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        source = DropPathEdit()
        ui_call(source, 'setPlaceholderText', tr('拖入数据库、JSON 或所在目录'))
        ui_call(source, 'setText', self.settings.value(f'{operation}/input', '', str))
        input_row = QHBoxLayout()
        input_row.addWidget(source, 1)
        file_button, folder_button = ui_widget(QPushButton, tr('选择文件')), ui_widget(QPushButton, tr('选择目录'))
        input_row.addWidget(file_button)
        input_row.addWidget(folder_button)
        file_button.clicked.connect(lambda: self._browse(operation, False, False))
        folder_button.clicked.connect(lambda: self._browse(operation, False, True))
        ui_call(form, 'addRow', tr('输入路径'), input_row)
        output = None
        if operation != 'check':
            output = DropPathEdit()
            ui_call(output, 'setPlaceholderText', tr('留空时自动生成，目录输入对应输出目录'))
            ui_call(output, 'setText', self.settings.value(f'{operation}/output', '', str))
            output_row = QHBoxLayout()
            output_row.addWidget(output, 1)
            output_button = ui_widget(QPushButton, tr('选择输出'))
            output_button.clicked.connect(lambda: self._browse(operation, True, False))
            output_row.addWidget(output_button)
            ui_call(form, 'addRow', tr('输出路径'), output_row)
        kind = None
        if operation != 'build':
            kind = QComboBox()
            ui_call(kind, 'addItem', tr('自动识别'), None)
            ui_call(kind, 'addItem', 'ScenarioDB', 'ScenarioDB')
            ui_call(kind, 'addItem', 'StringsDB', 'StringsDB')
            saved = self.settings.value(f'{operation}/kind', '', str)
            kind.setCurrentIndex(max(0, kind.findData(saved or None)))
            kind.setMinimumWidth(150)
            kind.setMaximumWidth(175)
        layout.addLayout(form)
        options = QHBoxLayout()
        options.setSpacing(12)
        if kind is not None:
            options.addWidget(kind)
        recursive = ui_widget(QCheckBox, tr('包含子目录'))
        recursive.setChecked(self.settings.value(f'{operation}/recursive', True, bool))
        options.addWidget(recursive)
        force = None
        if operation != 'check':
            force = ui_widget(QCheckBox, tr('允许覆盖已有输出'))
            force.setChecked(self.settings.value(f'{operation}/force', False, bool))
            options.addWidget(force)
        options.addStretch()
        ui_call(form, 'addRow', tr('类型') if kind is not None else tr('选项'), options)
        row = QHBoxLayout()
        title = {'export':tr('导出 JSON'), 'build':tr('重建数据库'), 'check':tr('验证无损往返')}[operation]
        run = ui_widget(QPushButton, title)
        run.setObjectName('PrimaryButton')
        run.clicked.connect(lambda: self._start(operation))
        cancel = ui_widget(QPushButton, tr('取消'))
        cancel.setEnabled(False)
        cancel.clicked.connect(self._cancel)
        status = ui_widget(QLabel, tr('就绪'))
        status.setObjectName('TipLabel')
        row.addWidget(run)
        row.addWidget(cancel)
        help_button = ui_widget(QPushButton, tr('使用说明'))
        help_button.clicked.connect(self._show_help)
        row.addWidget(help_button)
        row.addStretch()
        row.addWidget(status)
        layout.addLayout(row)
        layout.addStretch()
        controls = {'input':source, 'output':output, 'kind':kind, 'force':force, 'recursive':recursive,
            'run':run, 'cancel':cancel, 'status':status, 'page':page,
            'manual_output':self.settings.value(f'{operation}/manual_output', False, bool)}
        self._pages[operation] = controls
        source.textChanged.connect(lambda: self._update_output(operation))
        if output is not None:
            output.textEdited.connect(lambda text: controls.update(manual_output=bool(text.strip())))
            self._update_output(operation)
        return page

    def _show_help(self):
        QMessageBox.information(self, tr('数据库转换说明'),
            tr('导出：选择 ARC 解包后的 ScenarioDB / StringsDB，或选择所在目录。JSON 使用 UTF-8，可用文本编辑器修改。\n\n重建：从 JSON 读取数据库类型。单文件默认生成 .rebuilt；目录批处理会恢复原文件名，方便再用封包工具打包。\n\n检查：执行二进制 → JSON → 二进制，逐字节比较，不写出文件。\n\n二进制字符串使用 CP932。ScenarioDB 自动识别 64 字节和 128 字节格式：64 字节格式只有 label，最多 63 字节；128 字节格式的 resource 最多 95 字节、label 最多 31 字节。64 字节格式按记录顺序对应 Scenario0000 等资源，请保留顺序及 JSON 中的 record_size。StringsDB 的 text 最多 27 字节。按编码后的字节数计，超长或无法编码会报错。\n\nScenarioDB 最多 1024 条，StringsDB 最多 8192 条。key 的四段编号、重复编号和记录顺序都会保留。未知非零填充会报错，避免丢失数据。\n\n默认不覆盖已有输出。批处理中，一个文件失败会记入日志，其他文件继续处理。'))

    def _update_output(self, operation):
        controls = self._pages[operation]
        output = controls['output']
        if output is not None and not controls['manual_output']:
            text = controls['input'].text().strip()
            target = default_output(Path(text), operation) if text else None
            ui_call(output, 'setText', str(target) if target is not None else '')

    def _browse(self, operation, save, directory):
        controls = self._pages[operation]
        edit = controls['output'] if save else controls['input']
        current = edit.text().strip()
        if directory or (save and Path(controls['input'].text().strip()).is_dir()):
            value = QFileDialog.getExistingDirectory(self, tr('选择目录'), current)
        elif save:
            value, _ = QFileDialog.getSaveFileName(self, tr('选择输出文件'), current, tr('JSON (*.json);;所有文件 (*)') if operation == 'export' else tr('所有文件 (*)'))
        else:
            value, _ = QFileDialog.getOpenFileName(self, tr('选择输入文件'), current, tr('JSON (*.json);;所有文件 (*)') if operation == 'build' else tr('所有文件 (*)'))
        if value:
            if save:
                controls['manual_output'] = True
            ui_call(edit, 'setText', value)

    def _save_settings(self):
        for operation, controls in self._pages.items():
            for name in ('input', 'output'):
                if controls[name] is not None:
                    self.settings.setValue(f'{operation}/{name}', controls[name].text())
            for name in ('recursive', 'force'):
                if controls[name] is not None:
                    self.settings.setValue(f'{operation}/{name}', controls[name].isChecked())
            if controls['kind'] is not None:
                self.settings.setValue(f'{operation}/kind', controls['kind'].currentData() or '')
            self.settings.setValue(f'{operation}/manual_output', controls['manual_output'])
        self.settings.sync()

    def _set_busy(self, busy):
        for controls in self._pages.values():
            for widget in controls['page'].findChildren(QWidget):
                if isinstance(widget, (QPushButton, QLineEdit, QComboBox, QCheckBox)):
                    widget.setEnabled(not busy)
            controls['cancel'].setEnabled(busy)

    def _start(self, operation):
        if self.worker is not None:
            return
        controls = self._pages[operation]
        source = controls['input'].text().strip()
        if not source:
            self.log_view.append(tr('请先选择输入文件或目录。'))
            controls['input'].setFocus()
            return
        self._save_settings()
        self._operation = operation
        output = controls['output'].text().strip() if controls['output'] is not None else None
        self.worker = DatabaseWorker(operation, source, output,
            kind=controls['kind'].currentData() if controls['kind'] is not None else None,
            force=controls['force'].isChecked() if controls['force'] is not None else False,
            recursive=controls['recursive'].isChecked(), parent=self)
        self.worker.log_signal.connect(self._log)
        self.worker.completed.connect(self._completed)
        self.worker.failed.connect(lambda message: ui_call(controls['status'], 'setText', tr('处理失败')))
        self.worker.finished.connect(self._finished)
        self._set_busy(True)
        ui_call(controls['status'], 'setText', tr('正在处理…'))
        self.worker.start()

    def _log(self, message):
        self.log_view.append(message)
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _completed(self, result):
        text = tr('完成 {0} 个 · 失败 {1} 个', result.succeeded, len(result.errors))
        if result.cancelled:
            text += tr(' · 已取消')
        ui_call(self._pages[self._operation]['status'], 'setText', text)

    def _finished(self):
        worker = self.worker
        self.worker = None
        self._set_busy(False)
        worker.deleteLater()

    def _cancel(self):
        if self.worker is not None:
            self.worker.requestInterruption()

    def dragEnterEvent(self, event):
        self._active_input().dragEnterEvent(event)

    def dropEvent(self, event):
        self._active_input().dropEvent(event)

    def _active_input(self):
        return self._pages[('export', 'build', 'check')[self.task_tabs.currentIndex()]]['input']

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            if not self.worker.wait(2000):
                event.ignore()
                return
        self._save_settings()
        super().closeEvent(event)
