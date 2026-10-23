from __future__ import annotations
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr

from pathlib import Path

from PyQt6.QtCore import QSettings, QStandardPaths, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QTextCursor
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter, QSpinBox, QScrollArea, QFrame, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from ..asset_service import run_asset_task
from ..legacy_gui import bm_gui
from .gdb import DropPathEdit
from .controls import QualitySlider
from .layouts import FormGrid


class AssetWorker(QThread):
    log_signal = pyqtSignal(str)
    completed = pyqtSignal(bool, str)

    def __init__(self, operation, source, output, options):
        super().__init__()
        self.operation, self.source, self.output, self.options = operation, source, output, options

    def run(self):
        try:
            outputs = run_asset_task(self.operation, [Path(self.source)],
                Path(self.output) if self.output else None, options=self.options,
                log=self.log_signal.emit, cancelled=self.isInterruptionRequested)
            self.completed.emit(True, tr('完成，生成 {0} 项输出', len(outputs)) if outputs else tr('检查完成'))
        except Exception as error:
            self.completed.emit(False, str(error))


class AssetToolWidget(QWidget):
    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind, self.worker = kind, None
        self.setAcceptDrops(True)
        config = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)) / 'bgi_toolkit'
        config.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings(str(config / 'assets.ini'), QSettings.Format.IniFormat)
        self._pages = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.task_tabs = QTabWidget()
        splitter.addWidget(self.task_tabs)
        operations = {
            'image': [('image-export', tr('导出 PNG + JSON')), ('image-encode', tr('图片回写'))],
            'archive': [('archive-unpack', tr('ARC 解包')), ('archive-pack', tr('ARC 封包')), ('archive-list', tr('查看索引'))],
            'sdc': [('sdc-decompress', tr('解压')), ('sdc-compress', tr('压缩'))],
            'dsc': [('dsc-decompress', tr('解压')), ('dsc-compress', tr('压缩'))],
        }[kind]
        for operation, title in operations:
            ui_call(self.task_tabs, 'addTab', self._build_page(operation, title), title)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName('LogConsole')
        splitter.addWidget(self.log_view)
        splitter.setSizes([400, 260])
        layout.addWidget(splitter)

    def _build_page(self, operation, title):
        page = QWidget()
        outer = QVBoxLayout(page)
        scroll = QScrollArea()
        scroll.setObjectName('ToolPageScrollArea')
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.viewport().setObjectName('ToolPageViewport')
        content = QWidget(); content.setObjectName('ToolPageSurface')
        layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        descriptions = {
            'image-export': tr('支持 CBG、无压缩 BGI、DSC 图片。PNG 旁保存 JSON，保留基点 origin.x/y、未知头字段和封装信息。'),
            'image-encode': tr('支持 CBG V1 / V2 重新编码。可选 JSON 保留基点、量化表与头字段；无 JSON 时提示并使用默认值。V2 为有损压缩，透明通道无损。'),
            'archive-unpack': tr('解包 PackFile / BURIKO ARC20，自动展开 DSC / BSE；同名条目分别保存，清单保留原名与顺序。图片转换请使用图片工具。'),
            'archive-pack': tr('按清单保留文件名、顺序和索引字段，将目录中的原始资源封装为 ARC。'),
            'archive-list': tr('查看封包版本、条目名称和资源大小。'),
            'sdc-decompress': tr('解压 SDC 文件，支持单文件或目录。可同时保存封装信息 JSON，供修改后重新压缩。'),
            'dsc-decompress': tr('解压 DSC 文件，支持单文件或目录。可同时保存原始头和尾部信息，供修改后重新压缩。'),
            'sdc-compress': tr('将文件压缩为 SDC。默认读取同名 JSON 中的 seed 与尾部信息，也可手动指定 JSON。'),
            'dsc-compress': tr('将文件压缩为 DSC。默认读取同名 JSON 中的原始头与尾部信息，也可手动指定 JSON。')}
        hint = ui_widget(QLabel, descriptions[operation]); hint.setWordWrap(True)
        layout.addWidget(hint)
        source = DropPathEdit()
        ui_call(source, 'setText', self.settings.value(f'{operation}/input', '', str))
        row = QHBoxLayout(); row.addWidget(source, 1)
        file_button = ui_widget(QPushButton, tr('选择文件'), page); folder_button = ui_widget(QPushButton, tr('选择目录'), page)
        row.addWidget(file_button); row.addWidget(folder_button)
        file_button.clicked.connect(lambda: self._browse(source, False, False))
        folder_button.clicked.connect(lambda: self._browse(source, True, False))
        file_button.setVisible(operation != 'archive-pack')
        folder_button.setVisible(operation not in ('archive-unpack', 'archive-list'))
        layout.addWidget(ui_widget(QLabel, tr('输入文件或目录'))); layout.addLayout(row)
        output = None
        if operation != 'archive-list':
            output = DropPathEdit()
            ui_call(output, 'setText', self.settings.value(f'{operation}/output', '', str))
            out_row = QHBoxLayout(); out_row.addWidget(output, 1)
            browse = ui_widget(QPushButton, tr('选择输出')); out_row.addWidget(browse)
            browse.clicked.connect(lambda: self._browse_output(operation, source, output))
            layout.addWidget(ui_widget(QLabel, tr('输出文件或目录') if operation.startswith(('sdc-', 'dsc-')) else tr('输出封包') if operation == 'archive-pack' else tr('输出目录')))
            layout.addLayout(out_row)
        controls = {}
        if operation == 'image-encode':
            combo = QComboBox(); ui_call(combo, 'addItem', tr('保持 JSON 中的原格式'), 'original')
            ui_call(combo, 'addItem', 'CBG V1', 'cbg'); ui_call(combo, 'addItem', 'CBG V2', 'cbg2'); ui_call(combo, 'addItem', tr('BGI 无压缩图片'), 'bgi')
            form = FormGrid(); ui_call(form, 'addRow', tr('输出格式'), combo); layout.addLayout(form)
            controls['format'] = combo
            quality = QualitySlider(); ui_call(form, 'addRow', tr('V2 质量'), quality); controls['quality'] = quality
        if operation == 'archive-pack':
            combo = QComboBox(); ui_call(combo, 'addItem', tr('跟随原始清单（新封包默认 ARC20）'), None)
            ui_call(combo, 'addItem', 'BURIKO ARC20', 2); ui_call(combo, 'addItem', 'PackFile', 1)
            layout.addWidget(combo); controls['version'] = combo
        if operation.startswith(('sdc-', 'dsc-')):
            row = QHBoxLayout()
            for key, label, default in (('recursive', tr('包含子目录'), True), ('force', tr('允许覆盖已有输出'), False)):
                control = ui_widget(QCheckBox, label)
                control.setChecked(self.settings.value(f'{operation}/{key}', default, bool))
                controls[key] = control
                row.addWidget(control)
            if operation in ('sdc-decompress', 'dsc-decompress'):
                control = ui_widget(QCheckBox, tr('保存封装信息 JSON'))
                control.setChecked(self.settings.value(f'{operation}/metadata', True, bool))
                controls['metadata'] = control
                row.addWidget(control)
            row.addStretch()
            layout.addLayout(row)
            if operation in ('sdc-compress', 'dsc-compress'):
                metadata_edit = DropPathEdit()
                ui_call(metadata_edit, 'setText', self.settings.value(f'{operation}/metadata_path', '', str))
                ui_call(metadata_edit, 'setPlaceholderText', tr('留空自动读取“输入文件.json”；手动指定时仅用于单文件'))
                row = QHBoxLayout()
                row.addWidget(metadata_edit, 1)
                button = ui_widget(QPushButton, tr('选择 JSON'))
                button.clicked.connect(lambda: self._browse(metadata_edit, False, False))
                row.addWidget(button)
                layout.addWidget(ui_widget(QLabel, tr('封装信息 JSON（可选）')))
                layout.addLayout(row)
                controls['metadata_path'] = metadata_edit
        buttons = QHBoxLayout()
        run = ui_widget(QPushButton, tr('开始处理') if operation != 'archive-list' else tr('查看索引'))
        run.setObjectName('PrimaryButton'); run.clicked.connect(lambda: self._start(operation))
        stop = ui_widget(QPushButton, tr('取消')); stop.clicked.connect(self._cancel)
        run.setFixedWidth(128); stop.setFixedWidth(84)
        if output is not None:
            open_output = ui_widget(QPushButton, tr('打开输出目录'))
            open_output.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(
                str(Path(output.text()).parent if operation == 'archive-pack' or
                    (operation.startswith(('sdc-', 'dsc-')) and not Path(source.text()).is_dir()) else Path(output.text())))))
            buttons.addWidget(open_output)
        buttons.addStretch(1); buttons.addWidget(stop); buttons.addWidget(run)
        buttons.setSpacing(8)
        layout.addStretch(); outer.addLayout(buttons)
        self._pages[operation] = {'input': source, 'output': output, 'controls': controls,
                                 'run': run, 'last_auto': ''}
        source.textChanged.connect(lambda: self._sync_output(operation))
        if operation in ('sdc-compress', 'dsc-compress'):
            controls['metadata_path'].textChanged.connect(lambda: self._sync_output(operation))
        self._sync_output(operation)
        return page

    def _sync_output(self, operation):
        page = self._pages[operation]; output = page['output']
        if output is None or (output.text() and output.text() != page['last_auto']):
            return
        text = page['input'].text().strip().strip('"')
        if not text:
            return
        source = Path(text)
        if operation.startswith(('sdc-', 'dsc-')):
            from ..sdc_tool import default_sdc_output
            controls = page['controls']
            target = default_sdc_output(source, operation[4:],
                operation[:3],
                metadata_path=controls['metadata_path'].text().strip() or None if 'metadata_path' in controls else None)
        elif operation == 'archive-pack':
            target = source.with_name(source.name + '.arc')
        else:
            suffix = '_png' if operation == 'image-export' else '_rebuilt' if operation == 'image-encode' else '_unpacked'
            target = source.with_name(source.stem + suffix)
        page['last_auto'] = str(target)
        ui_call(output, 'setText', str(target))

    def _browse_output(self, operation, source, output):
        if operation.startswith(('sdc-', 'dsc-')) and not Path(source.text()).is_dir():
            path, _ = QFileDialog.getSaveFileName(self, tr('选择输出文件'), output.text(), tr('所有文件 (*)'))
            if path:
                ui_call(output, 'setText', path)
        else:
            self._browse(output, operation != 'archive-pack', True)

    def _browse(self, edit, directory, save):
        if directory:
            path = QFileDialog.getExistingDirectory(self, tr('选择目录'), edit.text())
        elif save:
            path, _ = QFileDialog.getSaveFileName(self, tr('输出封包'), edit.text(), tr('ARC (*.arc);;所有文件 (*)'))
        else:
            path, _ = QFileDialog.getOpenFileName(self, tr('选择文件'), edit.text(), tr('所有文件 (*)'))
        if path:
            ui_call(edit, 'setText', path)

    def _start(self, operation):
        if self.worker and self.worker.isRunning():
            return
        page = self._pages[operation]
        source = page['input'].text().strip().strip('"')
        if not source:
            self._log(tr('请选择输入文件或目录')); return
        output = page['output'].text().strip().strip('"') if page['output'] is not None else ''
        if page['output'] is not None and not output:
            self._log(tr('请选择输出位置')); return
        options = {name: control.isChecked() if isinstance(control, QCheckBox) else (control.value() or None) if isinstance(control,(QSpinBox,QualitySlider)) else (control.text().strip() or None) if isinstance(control, QLineEdit) else control.currentData()
                   for name, control in page['controls'].items()}
        for name, value in options.items():
            self.settings.setValue(f'{operation}/{name}', value if value is not None else '')
        options['output_directory'] = operation.startswith('image-')
        for item in self._pages.values(): item['run'].setEnabled(False)
        self.settings.setValue(f'{operation}/input', source)
        self.settings.setValue(f'{operation}/output', output); self.settings.sync()
        self.worker = AssetWorker(operation, source, output, options)
        self.worker.log_signal.connect(self._log)
        self.worker.completed.connect(self._completed)
        self.worker.start()

    def _log(self, text):
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        self.log_view.insertPlainText(text + '\n')
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _completed(self, success, message):
        self._log((tr('完成：') if success else tr('错误：')) + message)
        for page in self._pages.values(): page['run'].setEnabled(True)

    def _cancel(self):
        if self.worker and self.worker.isRunning(): self.worker.requestInterruption()

    def dragEnterEvent(self, event):
        self._pages[list(self._pages)[self.task_tabs.currentIndex()]]['input'].dragEnterEvent(event)

    def dropEvent(self, event):
        self._pages[list(self._pages)[self.task_tabs.currentIndex()]]['input'].dropEvent(event)

    def closeEvent(self, event):
        self._cancel()
        if self.worker and not self.worker.wait(2000):
            event.ignore(); return
        super().closeEvent(event)


class BmToolWindow(bm_gui.MainWindow):
    def __init__(self):
        super().__init__()
        from .layouts import rebuild_bm
        rebuild_bm(self)
        from .movie_build import MovieBuildWidget
        self.build_widget = MovieBuildWidget(self)
        ui_call(self.task_tabs, 'addTab', self.build_widget, tr('生成 BM'))
        export_footer = self.status_label.parentWidget()
        self.task_tabs.currentChanged.connect(lambda index: export_footer.setVisible(index != self.task_tabs.indexOf(self.build_widget)))

    def toggle_mode(self, btn=None):
        is_video = self.radio_video.isChecked()
        if hasattr(self, 'video_opts_frame'):
            self.video_opts_frame.setVisible(is_video)
            self.video_opts_frame.setEnabled(is_video)
        if hasattr(self, 'btn_ffmpeg'):
            self.btn_ffmpeg.show()

    def apply_theme(self, theme_name=None):
        # The unified window owns the stylesheet.
        self.setStyleSheet('')

    def dragEnterEvent(self, event):
        self.input_edit.dragEnterEvent(event)

    def dropEvent(self, event):
        self.input_edit.dropEvent(event)

    def closeEvent(self, event):
        self.build_widget.cancel()
        if self.build_widget.worker and not self.build_widget.worker.wait(2000):
            event.ignore(); return
        for worker in (self.worker, self.benchmark_worker):
            if worker and worker.isRunning():
                worker.stop()
                if not worker.wait(2000):
                    event.ignore(); return
        super().closeEvent(event)
