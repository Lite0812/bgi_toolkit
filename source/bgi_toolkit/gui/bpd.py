"""BPD decompilation with standalone and optional post-decompile function mapping."""
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr
from pathlib import Path

from PyQt6.QtCore import QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..function_mapping import OUTPUT_FILE, MappingCancelled, generate_function_mapping
from ..legacy_gui import bpd_gui
from .gdb import DropPathEdit
from .layouts import FormGrid


class FunctionMappingWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, target, output, definitions):
        super().__init__()
        self.target, self.output, self.definitions = target, output, definitions

    def run(self):
        try:
            result = generate_function_mapping(self.target, self.output or None, self.definitions or None,
                log=self.log_signal.emit, cancelled=self.isInterruptionRequested)
            self.finished_signal.emit(True, tr('函数映射完成：{0}', result.output_path))
        except MappingCancelled as error:
            self.finished_signal.emit(False, str(error))
        except Exception as error:
            self.finished_signal.emit(False, tr('警告：函数映射未生成：{0}', error))


class MappingDecompileWorker(bpd_gui.DecompileWorker):
    def __init__(self, *args, generate_mapping=False, mapping_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.generate_mapping, self.mapping_path = generate_mapping, mapping_path

    def _map_output(self, summary, target):
        if not self.generate_mapping:
            return summary
        if self.ir_only:
            self.log(tr('本次仅生成 IR，跳过函数映射。'))
            return summary
        self.log(tr('C 输出已完成，开始生成函数映射。'))
        try:
            result = generate_function_mapping(target, mapping_path=self.mapping_path,
                log=self.log, cancelled=self.isInterruptionRequested)
        except MappingCancelled:
            raise
        except Exception as error:
            self.log(tr('警告：C 反编译已完成，函数映射未生成：{0}', error))
            return tr('{0}；函数映射未生成，详见日志。', summary)
        return tr('{0}；函数映射：{1}', summary, result.output_path)

    def _run_single(self):
        summary = super()._run_single()
        source = Path(self.input_path)
        output = Path(self.output_path) if self.output_path else source.with_name(source.name + '.c')
        return self._map_output(summary, output)

    def _run_batch(self):
        summary = super()._run_batch()
        source = Path(self.input_path)
        output = Path(self.output_path) if self.output_path else source.parent / f'{source.name}_decompiled'
        return self._map_output(summary, output)


class BpdToolWindow(bpd_gui.MainWindow):
    def __init__(self):
        super().__init__()
        self._last_mapping_output = ''
        self._last_mapping_source = ''
        self.mapping_page = self._build_mapping_page()
        ui_call(self.content_tabs, 'insertTab', 1, self.mapping_page, tr('函数映射'))
        ui_call(self.mapping_input_edit, 'setText', self.settings.value('mapping/source', '', str))
        saved_output = self.settings.value('mapping/output', '', str)
        if saved_output:
            ui_call(self.mapping_output_edit, 'setText', saved_output)
        if self.settings.value('mapping/output_auto', True, bool):
            self._last_mapping_output = self.mapping_output_edit.text()
        ui_call(self.mapping_definitions_edit, 'setText', self.settings.value('mapping/definitions', '', str))
        self.mapping_after_checkbox.setChecked(self.settings.value('mapping/after_decompile', False, bool))
        for edit in (self.input_edit, self.single_output_edit, self.batch_output_edit):
            edit.textChanged.connect(self._suggest_mapping_source)
        self._suggest_mapping_source()

    def _build_task_page(self):
        page = super()._build_task_page()
        engine_layout = self.engine_combo.parentWidget().layout()
        for index in range(engine_layout.count()):
            row = engine_layout.itemAt(index).layout()
            if row is not None and row.indexOf(self.engine_combo) >= 0:
                engine_layout.takeAt(index)
                row.setParent(None)
                self._engine_settings_row = row
                break
        card = page.findChild(QFrame, 'CardFrame')
        self.mapping_after_checkbox = ui_widget(QCheckBox, tr('反编译后生成函数映射 CSV'))
        ui_call(self.mapping_after_checkbox, 'setToolTip', tr('扫描本次 C 输出，CSV 保存到 C 输出目录。映射失败时保留 C 输出并记录提示。仅生成 IR 时跳过。'))
        card.layout().insertWidget(card.layout().indexOf(self.mode_stack), self.mapping_after_checkbox)
        return page

    def _build_advanced_page(self):
        page = super()._build_advanced_page()
        group = QWidget()
        layout = QVBoxLayout(group)
        layout.setContentsMargins(0,0,0,0)
        layout.addLayout(self._engine_settings_row)
        page.layout().insertWidget(0,group)
        return page

    def _build_mapping_page(self):
        page = QWidget()
        layout = QVBoxLayout(page); layout.setContentsMargins(16, 16, 16, 16); layout.setSpacing(12)
        hint = ui_widget(QLabel, tr('扫描 C 代码中的 opcode 注册表，导出函数名、处理函数及来源文件/行号。支持旧版和新版注册写法。'))
        hint.setWordWrap(True); hint.setObjectName('TipLabel'); layout.addWidget(hint)
        form = FormGrid(); layout.addLayout(form)
        self.mapping_input_edit = DropPathEdit(); ui_call(self.mapping_input_edit, 'setPlaceholderText', tr('选择或拖入已反编译的 C 文件或目录'))
        self.mapping_definitions_edit = DropPathEdit(); ui_call(self.mapping_definitions_edit, 'setPlaceholderText', tr('留空使用内置 bss_mapping.json，也可选择自定义 JSON'))
        self.mapping_output_edit = DropPathEdit(); ui_call(self.mapping_output_edit, 'setPlaceholderText', tr('自动生成 CSV 路径，可手动修改'))
        def row(edit, *buttons):
            wrapper = QWidget(); wrapper.setObjectName('FormRow')
            box = QHBoxLayout(wrapper); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(8); box.addWidget(edit, 1)
            for title, callback in buttons:
                button = ui_widget(QPushButton, title); button.clicked.connect(callback); box.addWidget(button)
            return wrapper
        ui_call(form, 'addRow', tr('C 文件或目录'), row(self.mapping_input_edit, (tr('选择文件'), lambda: self._mapping_browse(self.mapping_input_edit, 'c')), (tr('选择目录'), lambda: self._mapping_browse(self.mapping_input_edit, 'directory'))))
        ui_call(form, 'addRow', tr('名称映射 JSON'), row(self.mapping_definitions_edit, (tr('选择文件'), lambda: self._mapping_browse(self.mapping_definitions_edit, 'json'))))
        ui_call(form, 'addRow', tr('输出 CSV'), row(self.mapping_output_edit, (tr('选择文件'), lambda: self._mapping_browse(self.mapping_output_edit, 'csv'))))
        buttons = QHBoxLayout()
        self.mapping_open_button = ui_widget(QPushButton, tr('打开输出目录')); self.mapping_open_button.clicked.connect(self._open_mapping_output)
        self.mapping_cancel_button = ui_widget(QPushButton, tr('取消')); self.mapping_cancel_button.setEnabled(False)
        self.mapping_cancel_button.clicked.connect(self._cancel_mapping)
        self.mapping_run_button = ui_widget(QPushButton, tr('生成函数映射')); self.mapping_run_button.setObjectName('PrimaryButton')
        self.mapping_run_button.clicked.connect(self._start_mapping)
        buttons.addWidget(self.mapping_open_button); buttons.addStretch()
        buttons.addWidget(self.mapping_cancel_button); buttons.addWidget(self.mapping_run_button)
        layout.addLayout(buttons)
        tip = ui_widget(QLabel, tr('CSV 包含 Opcode、OpcodeHex、FunctionName、Status、Handler、SourceFile、SourceLine 七列。'))
        tip.setWordWrap(True); tip.setObjectName('TipLabel'); layout.addWidget(tip); layout.addStretch()
        self.mapping_input_edit.textChanged.connect(self._sync_mapping_output)
        return page

    def _mapping_browse(self, edit, kind):
        current = edit.text().strip()
        if kind == 'directory':
            selected = QFileDialog.getExistingDirectory(self, tr('选择已反编译的 C 目录'), current)
        elif kind == 'csv':
            selected, _ = QFileDialog.getSaveFileName(self, tr('保存函数映射'), current, tr('CSV 文件 (*.csv);;所有文件 (*)'))
        else:
            filters = tr('C 源文件 (*.c *.C);;所有文件 (*)') if kind == 'c' else tr('映射 JSON (*.json);;所有文件 (*)')
            selected, _ = QFileDialog.getOpenFileName(self, tr('选择 C 代码') if kind == 'c' else tr('选择名称映射 JSON'), current, filters)
        if selected:
            ui_call(edit, 'setText', selected)

    def _sync_mapping_output(self, *_):
        text = self.mapping_input_edit.text().strip()
        current = self.mapping_output_edit.text()
        if not text or (current and current != self._last_mapping_output):
            return
        source = Path(text)
        root = source.parent if source.suffix.lower() == '.c' and not source.is_dir() else source
        self._last_mapping_output = str(root / OUTPUT_FILE)
        ui_call(self.mapping_output_edit, 'setText', self._last_mapping_output)

    def _suggest_mapping_source(self, *_):
        if not hasattr(self, 'mapping_input_edit') or not self.input_edit.text().strip():
            return
        output = self.batch_output_edit.text().strip() if self._input_is_batch() else self.single_output_edit.text().strip()
        if not output:
            return
        suggested = str(Path(output)) if self._input_is_batch() else str(Path(output).parent)
        current = self.mapping_input_edit.text()
        if not current or current == self._last_mapping_source:
            ui_call(self.mapping_input_edit, 'setText', suggested)
        self._last_mapping_source = suggested

    def _make_decompile_worker(self, *args, **kwargs):
        return MappingDecompileWorker(*args, generate_mapping=self.mapping_after_checkbox.isChecked(),
            mapping_path=self.mapping_definitions_edit.text().strip() or None, **kwargs)

    def _start_mapping(self):
        if self.worker is not None and self.worker.isRunning():
            self.append_log(tr('请等待当前 BPD 任务完成。')); return
        source = self.mapping_input_edit.text().strip()
        if not source:
            self.append_log(tr('请选择已反编译的 C 文件或目录。')); return
        self._save_settings()
        self.worker = FunctionMappingWorker(source, self.mapping_output_edit.text().strip(), self.mapping_definitions_edit.text().strip())
        self.worker.log_signal.connect(self.append_log)
        self.worker.finished_signal.connect(self._on_worker_finished)
        self._set_controls_enabled(False)
        self.append_log(tr('开始生成函数映射。'))
        self.worker.start()

    def _cancel_mapping(self):
        if isinstance(self.worker, FunctionMappingWorker) and self.worker.isRunning():
            self.worker.requestInterruption(); self.append_log(tr('正在取消函数映射生成…'))

    def _open_mapping_output(self):
        output = self.mapping_output_edit.text().strip()
        if output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(output).parent)))

    def _set_controls_enabled(self, enabled):
        super()._set_controls_enabled(enabled)
        self.mapping_after_checkbox.setEnabled(enabled)
        if hasattr(self, 'mapping_run_button'):
            self.mapping_run_button.setEnabled(enabled)
            self.mapping_cancel_button.setEnabled(not enabled and isinstance(self.worker, FunctionMappingWorker))

    def _on_worker_finished(self, success, summary):
        if not success and tr('已取消函数映射生成') in summary:
            self.append_log(tr('已取消函数映射生成。')); self._set_controls_enabled(True); self.worker = None
        elif not success and isinstance(self.worker, FunctionMappingWorker):
            self.append_log(summary)
            self._set_controls_enabled(True)
            self.worker = None
        else:
            super()._on_worker_finished(success, summary)

    def _save_settings(self):
        if hasattr(self, 'mapping_input_edit'):
            self.settings.setValue('mapping/source', self.mapping_input_edit.text())
            self.settings.setValue('mapping/output', self.mapping_output_edit.text())
            self.settings.setValue('mapping/output_auto', self.mapping_output_edit.text() == self._last_mapping_output)
            self.settings.setValue('mapping/definitions', self.mapping_definitions_edit.text())
            self.settings.setValue('mapping/after_decompile', self.mapping_after_checkbox.isChecked())
        super()._save_settings()

    def dragEnterEvent(self, event):
        if self.content_tabs.currentWidget() is self.mapping_page:
            self.mapping_input_edit.dragEnterEvent(event)
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        if self.content_tabs.currentWidget() is self.mapping_page:
            self.mapping_input_edit.dropEvent(event)
        else:
            super().dropEvent(event)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            if not self.worker.wait(2000):
                event.ignore(); return
        super().closeEvent(event)
