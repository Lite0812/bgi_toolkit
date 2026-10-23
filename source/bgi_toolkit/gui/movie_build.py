"""Frame sequence / video to BM page, sharing the BM tool's log."""
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr
from pathlib import Path
from PyQt6.QtCore import QSettings, QStandardPaths, QThread, pyqtSignal
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QPushButton,
    QSpinBox,QComboBox,QCheckBox,QFileDialog,QScrollArea,QFrame)
from .paths import PathLineEdit
from .controls import QualitySlider
from .layouts import FormGrid
from ..audio_tool import AudioCancelled
from ..movie_encode import MovieEncodeOptions,encode_movie


class MovieBuildWorker(QThread):
    log_signal=pyqtSignal(str)
    completed=pyqtSignal(object)
    def __init__(self,source,output,options,parent=None):
        super().__init__(parent)
        self.source,self.output,self.options=source,output,options
    def run(self):
        try:
            encode_movie(self.source,self.output,options=self.options,log=self.log_signal.emit,cancelled=self.isInterruptionRequested)
            self.completed.emit(tr('BM 生成完成'))
        except AudioCancelled:
            self.log_signal.emit(tr('已取消，临时文件已清理'))
            self.completed.emit(tr('已取消'))
        except Exception as error:
            self.log_signal.emit(tr('失败：{0}', error))
            self.completed.emit(tr('生成失败'))


class MovieBuildWidget(QWidget):
    def __init__(self,owner):
        super().__init__()
        self.owner,self.worker=owner,None
        folder=Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))/'bgi_toolkit'
        folder.mkdir(parents=True,exist_ok=True)
        self.settings=QSettings(str(folder/'movie_build.ini'),QSettings.Format.IniFormat)
        layout=QVBoxLayout(self);layout.setContentsMargins(8,8,8,8)
        scroll=QScrollArea();scroll.setObjectName('ToolPageScrollArea');scroll.viewport().setObjectName('ToolPageViewport');scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame)
        content=QWidget();content.setObjectName('ToolPageSurface');box=QVBoxLayout(content);box.setSpacing(12)
        hint=ui_widget(QLabel, tr('将帧图片目录或视频编码为 BM。帧文件按数字顺序排列，尺寸须一致。当前写入完整帧，文件较大；颜色为有损 DCT，透明通道无损。视频音轨请用音频工具单独处理。'))
        hint.setWordWrap(True);hint.setObjectName('TipLabel');box.addWidget(hint)
        form=FormGrid()
        self.input_edit,self.output_edit,self.ffmpeg_edit,self.metadata_edit=[PathLineEdit() for _ in range(4)]
        for key,edit,default in (('input',self.input_edit,''),('output',self.output_edit,''),
            ('ffmpeg',self.ffmpeg_edit,owner.ffmpeg_edit.text()),('metadata',self.metadata_edit,'')):
            ui_call(edit, 'setText', self.settings.value(key, default, str))
        ui_call(self.input_edit, 'setPlaceholderText', tr('拖入帧目录或视频'))
        ui_call(self.output_edit, 'setPlaceholderText', tr('留空时自动生成 .bm 输出路径'))
        ui_call(self.metadata_edit, 'setPlaceholderText', tr('可选；目录内 movie.json 会自动读取'))
        def row(edit,*buttons):
            wrapper=QWidget();row=QHBoxLayout(wrapper);row.setContentsMargins(0,0,0,0);row.addWidget(edit,1)
            for title,callback in buttons:
                button=ui_widget(QPushButton, title);button.clicked.connect(callback);row.addWidget(button)
            return wrapper
        ui_call(form, 'addRow', tr('帧目录或视频'), row(self.input_edit, (tr('选择目录'), self._browse_folder), (tr('选择视频'), self._browse_video)))
        ui_call(form, 'addRow', tr('输出 BM'), row(self.output_edit, (tr('选择输出'), self._browse_output)))
        ui_call(form, 'addRow', 'FFmpeg', row(self.ffmpeg_edit, (tr('选择 FFmpeg'), self._browse_ffmpeg)))
        ui_call(form, 'addRow', tr('元信息 JSON'), row(self.metadata_edit, (tr('选择 JSON'), self._browse_metadata)))
        self.fps=QSpinBox();self.fps.setRange(0,240);self.fps.setSpecialValueText(tr('自动 / 30'))
        self.quality=QualitySlider()
        self.bits=QComboBox();ui_call(self.bits, 'addItem', tr('自动 / 32 位'), None);ui_call(self.bits, 'addItem', tr('24 位'), 24);ui_call(self.bits, 'addItem', tr('32 位（透明）'), 32)
        self.limit=QSpinBox();self.limit.setRange(0,1000000);self.limit.setSpecialValueText(tr('全部帧'))
        for name,widget in (('fps',self.fps),('quality',self.quality),('limit',self.limit)):
            widget.setValue(self.settings.value(name,0,int))
            if name!='quality':widget.setMaximumWidth(220)
        self.bits.setCurrentIndex(max(0,self.bits.findData(self.settings.value('bpp',0,int) or None)))
        self.bits.setMaximumWidth(180)
        ui_call(form, 'addRow', tr('帧率'), self.fps)
        ui_call(form, 'addRow', tr('画质'), self.quality)
        ui_call(form, 'addRow', tr('位深'), self.bits)
        ui_call(form, 'addRow', tr('最大帧数'), self.limit)
        self.timing_notice=ui_widget(QLabel);self.timing_notice.setWordWrap(True);self.timing_notice.setObjectName('TipLabel')
        ui_call(form, 'addRow', '', self.timing_notice)
        self.force=ui_widget(QCheckBox, tr('允许覆盖已有输出'));self.force.setChecked(self.settings.value('force',False,bool))
        ui_call(form, 'addRow', '', self.force);box.addLayout(form);box.addStretch();scroll.setWidget(content);layout.addWidget(scroll,1)
        row=QHBoxLayout();self.status=ui_widget(QLabel, tr('就绪'));row.addWidget(self.status);row.addStretch()
        self.cancel_button=ui_widget(QPushButton, tr('取消'));self.cancel_button.setEnabled(False);self.cancel_button.clicked.connect(self.cancel)
        self.run_button=ui_widget(QPushButton, tr('生成 BM'));self.run_button.setObjectName('PrimaryButton');self.run_button.clicked.connect(self.start)
        row.addWidget(self.cancel_button);row.addWidget(self.run_button);layout.addLayout(row)
        self.input_edit.textChanged.connect(self._sync_timing_controls)
        self._sync_timing_controls()
    def _sync_timing_controls(self, *_):
        text=self.input_edit.text().strip()
        sequence=bool(text and Path(text).is_dir())
        self.fps.setEnabled(sequence and self.worker is None)
        self.limit.setEnabled(sequence and self.worker is None)
        ui_call(self.timing_notice, 'setText', tr('图片序列：可设置帧率和最大帧数。') if sequence else tr('视频：跟随原帧率，保留全部帧；手动帧率和最大帧数仅对图片序列生效。'))
        ui_call(self.fps, 'setToolTip', tr('仅图片序列可设置；视频自动读取原帧率。'))
        ui_call(self.limit, 'setToolTip', tr('仅图片序列可限制帧数；视频保留全部帧。'))

    def _choose(self,edit,title,save=False,filter=tr('所有文件 (*)')):
        method=QFileDialog.getSaveFileName if save else QFileDialog.getOpenFileName
        path,_=method(self,title,edit.text(),filter)
        if path:ui_call(edit, 'setText', path)
    def _browse_folder(self):
        path=QFileDialog.getExistingDirectory(self,tr('选择帧目录'),self.input_edit.text())
        if path:ui_call(self.input_edit, 'setText', path)
    def _browse_video(self):self._choose(self.input_edit,tr('选择视频'))
    def _browse_output(self):self._choose(self.output_edit,tr('输出 BM'),True,tr('BM (*.bm);;所有文件 (*)'))
    def _browse_ffmpeg(self):self._choose(self.ffmpeg_edit,tr('选择 FFmpeg'),filter=tr('FFmpeg (ffmpeg.exe);;所有文件 (*)'))
    def _browse_metadata(self):self._choose(self.metadata_edit,tr('选择 BM 元信息'),filter=tr('JSON (*.json);;所有文件 (*)'))
    def start(self):
        if self.worker is not None:return
        if self.owner.worker and self.owner.worker.isRunning():
            self.owner.log_view.append(tr('请先结束正在进行的 BM 导出任务。'));return
        source=self.input_edit.text().strip();output=self.output_edit.text().strip()
        if not source:self.owner.log_view.append(tr('请选择帧目录或视频。'));return
        sequence=Path(source).is_dir()
        options=MovieEncodeOptions(fps=(self.fps.value() or None) if sequence else None,bpp=self.bits.currentData(),
            quality=self.quality.value() or None,limit=(self.limit.value() or None) if sequence else None,
            ffmpeg=self.ffmpeg_edit.text().strip() or 'ffmpeg',force=self.force.isChecked(),
            metadata_path=Path(self.metadata_edit.text()) if self.metadata_edit.text().strip() else None)
        for key,edit in (('input',self.input_edit),('output',self.output_edit),('ffmpeg',self.ffmpeg_edit),('metadata',self.metadata_edit)):
            self.settings.setValue(key,edit.text())
        for name,value in (('fps',self.fps.value()),('quality',self.quality.value()),('limit',self.limit.value()),
            ('bpp',self.bits.currentData() or 0),('force',self.force.isChecked())):self.settings.setValue(name,value)
        self.settings.sync()
        self.worker=MovieBuildWorker(source,output or None,options,self)
        self.worker.log_signal.connect(self.owner.log_view.append)
        self.worker.completed.connect(ui_slot(self.status, 'setText'));self.worker.finished.connect(self._finished)
        self.run_button.setEnabled(False);self.owner.start_btn.setEnabled(False);self.cancel_button.setEnabled(True)
        ui_call(self.status, 'setText', tr('正在编码…'));self._sync_timing_controls();self.worker.start()
    def cancel(self):
        if self.worker:self.worker.requestInterruption()
    def _finished(self):
        worker=self.worker;self.worker=None
        self.run_button.setEnabled(True);self.owner.start_btn.setEnabled(True);self.cancel_button.setEnabled(False)
        self._sync_timing_controls()
        worker.deleteLater()
