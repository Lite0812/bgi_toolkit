"""Audio conversion workspace with explicit FFmpeg selection and shared logging."""
from __future__ import annotations
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr
from pathlib import Path
from PyQt6.QtCore import QSettings, QStandardPaths, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from ..audio_tool import AudioOptions, AudioResult, default_audio_output, ffmpeg_version, run_audio_task
from .gdb import DropPathEdit
from .paths import PathLineEdit


class AudioWorker(QThread):
    log_signal = pyqtSignal(str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, operation, source, output, options, parent=None):
        super().__init__(parent)
        self.operation, self.source, self.output, self.options = operation, source, output, options

    def run(self):
        try:
            if self.operation == 'version':
                self.log_signal.emit(ffmpeg_version(self.options.ffmpeg))
                result = AudioResult(succeeded=1)
            else:
                result = run_audio_task(Path(self.source), Path(self.output) if self.output else None,
                    options=self.options, inspect=self.operation == 'info', log=self.log_signal.emit,
                    cancelled=self.isInterruptionRequested)
            self.completed.emit(result)
        except Exception as error:
            self.log_signal.emit(tr('失败：{0}', error))
            self.failed.emit(str(error))


class AudioToolWidget(QWidget):
    def __init__(self, parent=None, *, ffmpeg_path='ffmpeg'):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.worker = None
        folder = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)) / 'bgi_toolkit'
        folder.mkdir(parents=True, exist_ok=True)
        self.settings = QSettings(str(folder/'audio.ini'), QSettings.Format.IniFormat)
        self.manual_output = self.settings.value('manual_output', False, bool)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.task_tabs = QTabWidget()
        main = QWidget()
        box = QVBoxLayout(main)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(10)
        hint = ui_widget(QLabel, tr('支持 BW 与 WAV、FLAC、OGG、MP3、AAC、M4A、AIFF 互转。回写 BW 时自动读取音频旁的 JSON；无 JSON 也可新建。'))
        hint.setWordWrap(True)
        hint.setObjectName('TipLabel')
        box.addWidget(hint)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        self.input_edit, self.output_edit = DropPathEdit(), DropPathEdit()
        ui_call(self.input_edit, 'setText', self.settings.value('input', '', str))
        ui_call(self.output_edit, 'setText', self.settings.value('output', '', str))
        ui_call(self.input_edit, 'setPlaceholderText', tr('拖入音频文件或所在目录'))
        ui_call(self.output_edit, 'setPlaceholderText', tr('留空时自动生成；目录输入对应输出目录'))
        row = QHBoxLayout()
        row.addWidget(self.input_edit, 1)
        self.file_button, self.folder_button = ui_widget(QPushButton, tr('选择文件')), ui_widget(QPushButton, tr('选择目录'))
        self.file_button.clicked.connect(self._browse_input)
        self.folder_button.clicked.connect(self._browse_directory)
        row.addWidget(self.file_button)
        row.addWidget(self.folder_button)
        ui_call(form, 'addRow', tr('输入路径'), row)
        row = QHBoxLayout()
        row.addWidget(self.output_edit, 1)
        self.output_button = ui_widget(QPushButton, tr('选择输出'))
        self.output_button.clicked.connect(self._browse_output)
        row.addWidget(self.output_button)
        ui_call(form, 'addRow', tr('输出路径'), row)
        row = QHBoxLayout()
        self.format_combo = QComboBox()
        for title, value in ((tr('原音频（无损）'),'original'), ('WAV','wav'), ('FLAC','flac'), ('OGG','ogg'), ('MP3','mp3'), ('AAC','aac'), ('M4A','m4a'), ('AIFF','aiff'), ('BW · Ogg','bw'), ('BW · PCM','bw_pcm')):
            ui_call(self.format_combo, 'addItem', title, value)
        self.format_combo.setCurrentIndex(max(0, self.format_combo.findData(self.settings.value('format', 'original', str))))
        self.format_combo.setMinimumWidth(160)
        self.format_combo.setMaximumWidth(180)
        self.ffmpeg_edit = PathLineEdit()
        ui_call(self.ffmpeg_edit, 'setText', self.settings.value('ffmpeg', ffmpeg_path or 'ffmpeg', str))
        ui_call(self.ffmpeg_edit, 'setPlaceholderText', tr('FFmpeg 路径或命令名'))
        self.ffmpeg_button = ui_widget(QPushButton, tr('选择 FFmpeg'))
        self.ffmpeg_button.clicked.connect(self._browse_ffmpeg)
        row.addWidget(self.format_combo)
        row.addWidget(self.ffmpeg_edit, 1)
        row.addWidget(self.ffmpeg_button)
        ui_call(form, 'addRow', tr('转换设置'), row)
        box.addLayout(form)
        row = QHBoxLayout()
        self.run_button = ui_widget(QPushButton, tr('开始转换'))
        self.run_button.setObjectName('PrimaryButton')
        self.run_button.clicked.connect(lambda: self._start('convert'))
        self.info_button = ui_widget(QPushButton, tr('查看信息'))
        self.info_button.clicked.connect(lambda: self._start('info'))
        self.cancel_button = ui_widget(QPushButton, tr('取消'))
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        self.status = ui_widget(QLabel, tr('就绪'))
        self.status.setObjectName('TipLabel')
        row.addWidget(self.run_button)
        row.addWidget(self.info_button)
        row.addWidget(self.cancel_button)
        row.addStretch()
        row.addWidget(self.status)
        box.addLayout(row)
        box.addStretch()
        ui_call(self.task_tabs, 'addTab', main, tr('音频转换'))
        advanced = QWidget()
        box = QVBoxLayout(advanced)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(12)
        hint = ui_widget(QLabel, tr('BW 支持 Ogg Vorbis 与 16 位 PCM。回写时保留 JSON 中的未知头字段和循环设置，并更新长度、帧数、采样率与声道。'))
        hint.setObjectName('TipLabel')
        hint.setWordWrap(True)
        box.addWidget(hint)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        self.rate_combo, self.channels_combo, self.bitrate_combo, self.bits_combo = QComboBox(), QComboBox(), QComboBox(), QComboBox()
        ui_call(self.rate_combo, 'addItem', tr('保持采样率'), None)
        for rate in (22050, 44100, 48000, 96000):
            ui_call(self.rate_combo, 'addItem', f'{rate} Hz', rate)
        for text, value in ((tr('保持声道'),None), (tr('单声道'),1), (tr('立体声'),2)):
            ui_call(self.channels_combo, 'addItem', text, value)
        for rate in (64, 96, 128, 160, 192, 256, 320):
            ui_call(self.bitrate_combo, 'addItem', f'{rate} kbps', rate)
        for bits in (16, 24, 32):
            ui_call(self.bits_combo, 'addItem', tr('WAV {0} 位', bits), bits)
        for key, combo, default in (('sample_rate', self.rate_combo, 0), ('channels', self.channels_combo, 0),
                ('bitrate', self.bitrate_combo, 192), ('wav_bits', self.bits_combo, 16)):
            selected = self.settings.value(key, default, int)
            combo.setCurrentIndex(max(0, combo.findData(selected or None)))
            combo.setMaximumWidth(190)
        row = QHBoxLayout()
        row.addWidget(self.rate_combo)
        row.addWidget(self.channels_combo)
        row.addStretch()
        ui_call(form, 'addRow', tr('音频参数'), row)
        row = QHBoxLayout()
        row.addWidget(self.bitrate_combo)
        row.addWidget(self.bits_combo)
        row.addStretch()
        ui_call(form, 'addRow', tr('编码质量'), row)
        self.metadata_edit = PathLineEdit()
        ui_call(self.metadata_edit, 'setPlaceholderText', tr('留空自动读取“输入音频.json”；可选择原音频导出的 JSON'))
        ui_call(self.metadata_edit, 'setText', self.settings.value('metadata_path', '', str))
        row = QHBoxLayout()
        row.addWidget(self.metadata_edit, 1)
        self.metadata_button = ui_widget(QPushButton, tr('选择 JSON'))
        self.metadata_button.clicked.connect(self._browse_metadata)
        row.addWidget(self.metadata_button)
        ui_call(form, 'addRow', tr('BW 头信息'), row)
        box.addLayout(form)
        row = QHBoxLayout()
        self.recursive, self.force, self.metadata = ui_widget(QCheckBox, tr('包含子目录')), ui_widget(QCheckBox, tr('允许覆盖已有输出')), ui_widget(QCheckBox, tr('保留 BW 头信息 JSON'))
        for name, widget, default in (('recursive',self.recursive,True), ('force',self.force,False), ('metadata',self.metadata,True)):
            widget.setChecked(self.settings.value(name, default, bool))
            row.addWidget(widget)
        row.addStretch()
        box.addLayout(row)
        row = QHBoxLayout()
        self.version_button = ui_widget(QPushButton, tr('检测 FFmpeg'))
        self.version_button.clicked.connect(lambda: self._start('version'))
        row.addWidget(self.version_button)
        row.addStretch()
        box.addLayout(row)
        note = ui_widget(QLabel, tr('BW 的头字段与循环设置可另存为语义化 JSON；没有 JSON 也可回写，日志会提示所用默认值。批处理保留相对目录；单个文件失败后继续处理其余文件。'))
        note.setObjectName('TipLabel')
        note.setWordWrap(True)
        box.addWidget(note)
        box.addStretch()
        ui_call(self.task_tabs, 'addTab', advanced, tr('高级设置'))
        splitter.addWidget(self.task_tabs)
        self.log_view = QTextEdit()
        self.log_view.setObjectName('LogConsole')
        self.log_view.setReadOnly(True)
        splitter.addWidget(self.log_view)
        splitter.setSizes([390, 260])
        layout.addWidget(splitter)
        self.input_edit.textChanged.connect(self._update_output)
        self.output_edit.textEdited.connect(lambda text: setattr(self, 'manual_output', bool(text.strip())))
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        self._format_changed()

    def _format_changed(self, *_):
        self._update_output()
        self._enable_encoding()

    def _enable_encoding(self):
        kind = self.format_combo.currentData()
        self.rate_combo.setEnabled(kind != 'original')
        self.channels_combo.setEnabled(kind != 'original')
        self.bitrate_combo.setEnabled(kind in ('mp3', 'ogg', 'aac', 'm4a', 'bw'))
        self.bits_combo.setEnabled(kind == 'wav')
        self.metadata_edit.setEnabled(kind in ('bw','bw_pcm'))
        self.metadata_button.setEnabled(kind in ('bw','bw_pcm'))

    def _update_output(self, *_):
        if not self.manual_output:
            text = self.input_edit.text().strip()
            ui_call(self.output_edit, 'setText', str(default_audio_output(Path(text), self.format_combo.currentData())) if text else '')

    def _browse_input(self):
        path, _ = QFileDialog.getOpenFileName(self, tr('选择音频文件'), self.input_edit.text(), tr('所有文件 (*);;音频 (*.bw *._bw *.ogg *.wav *.mp3 *.flac *.m4a *.aac)'))
        if path:
            ui_call(self.input_edit, 'setText', path)

    def _browse_directory(self):
        path = QFileDialog.getExistingDirectory(self, tr('选择音频目录'), self.input_edit.text())
        if path:
            ui_call(self.input_edit, 'setText', path)

    def _browse_output(self):
        if Path(self.input_edit.text()).is_dir():
            path = QFileDialog.getExistingDirectory(self, tr('选择输出目录'), self.output_edit.text())
        else:
            path, _ = QFileDialog.getSaveFileName(self, tr('选择输出音频'), self.output_edit.text(), tr('所有文件 (*)'))
        if path:
            self.manual_output = True
            ui_call(self.output_edit, 'setText', path)

    def _browse_metadata(self):
        path, _ = QFileDialog.getOpenFileName(self, tr('选择 BW 头信息 JSON'), self.metadata_edit.text(), tr('JSON (*.json);;所有文件 (*)'))
        if path:
            ui_call(self.metadata_edit, 'setText', path)

    def _browse_ffmpeg(self):
        path, _ = QFileDialog.getOpenFileName(self, tr('选择 FFmpeg'), self.ffmpeg_edit.text(), tr('FFmpeg (ffmpeg.exe);;所有文件 (*)'))
        if path:
            ui_call(self.ffmpeg_edit, 'setText', path)

    def _options(self):
        return AudioOptions(format=self.format_combo.currentData(), ffmpeg=self.ffmpeg_edit.text().strip() or 'ffmpeg',
            sample_rate=self.rate_combo.currentData(), channels=self.channels_combo.currentData(),
            bitrate=self.bitrate_combo.currentData(), wav_bits=self.bits_combo.currentData(),
            recursive=self.recursive.isChecked(), force=self.force.isChecked(), metadata=self.metadata.isChecked(),
            metadata_path=Path(self.metadata_edit.text()) if self.metadata_edit.text().strip() and self.format_combo.currentData() in ('bw','bw_pcm') else None)

    def _save_settings(self):
        for name, widget in (('input',self.input_edit), ('output',self.output_edit), ('ffmpeg',self.ffmpeg_edit), ('metadata_path',self.metadata_edit)):
            self.settings.setValue(name, widget.text())
        options = self._options()
        for name in ('format','sample_rate','channels','bitrate','wav_bits','recursive','force','metadata'):
            self.settings.setValue(name, getattr(options,name) if getattr(options,name) is not None else 0)
        self.settings.setValue('manual_output', self.manual_output)
        self.settings.sync()

    def _set_busy(self, busy):
        for widget in self.task_tabs.findChildren(QWidget):
            if isinstance(widget, (QPushButton, QLineEdit, QComboBox, QCheckBox)):
                widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        if not busy:
            self._enable_encoding()

    def _start(self, operation):
        if self.worker is not None:
            return
        if operation != 'version' and not self.input_edit.text().strip():
            self.log_view.append(tr('请先选择音频文件或目录。'))
            self.input_edit.setFocus()
            return
        self._save_settings()
        self.worker = AudioWorker(operation, self.input_edit.text(), self.output_edit.text(), self._options(), self)
        self.worker.log_signal.connect(self._log)
        self.worker.completed.connect(self._completed)
        self.worker.failed.connect(lambda _: ui_call(self.status, 'setText', tr('处理失败')))
        self.worker.finished.connect(self._finished)
        self._set_busy(True)
        ui_call(self.status, 'setText', tr('正在处理…'))
        self.worker.start()

    def _log(self, message):
        self.log_view.append(message)
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _completed(self, result):
        ui_call(self.status, 'setText', tr('完成 {0} 个 · 失败 {1} 个', result.succeeded, len(result.errors)) + (tr(' · 已取消') if result.cancelled else ''))

    def _finished(self):
        worker = self.worker
        self.worker = None
        self._set_busy(False)
        worker.deleteLater()

    def _cancel(self):
        if self.worker is not None:
            self.worker.requestInterruption()

    def dragEnterEvent(self, event):
        self.input_edit.dragEnterEvent(event)

    def dropEvent(self, event):
        self.input_edit.dropEvent(event)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            if not self.worker.wait(2000):
                event.ignore()
                return
        self._save_settings()
        super().closeEvent(event)
