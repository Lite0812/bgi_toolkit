from __future__ import annotations
from ..gui.localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr

from datetime import datetime
from pathlib import Path
import sys
import traceback

try:
    from PyQt6.QtCore import QSettings, QStandardPaths, QThread, Qt, pyqtSignal
    from PyQt6.QtGui import QTextCursor
    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSplitter,
        QStackedWidget,
        QTabWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:
    raise SystemExit("启动 GUI 需要 PyQt6。请先安装: pip install PyQt6 darkdetect") from exc

try:
    import darkdetect

    HAS_DARKDETECT = True
except ImportError:
    HAS_DARKDETECT = False

if sys.platform.startswith("win"):
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

from ..bpd2c.opcode_semantics import load_opcode_semantics
from ..bp_engine import PROFILES, detect_engine
from ..bpd2c.pipeline import (
    decompile_file,
    decompile_files,
    iter_input_files,
    output_path_for_batch,
    write_ir_result,
    write_result,
)


def resolve_settings_path() -> Path:
    root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)) / "bgi_toolkit"
    root.mkdir(parents=True, exist_ok=True)
    return root / "bpd2c-gui.ini"


class CardFrame(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("CardFrame")
        self.setFrameShape(QFrame.Shape.NoFrame)


class ModernButton(QPushButton):
    def __init__(self, text: str, is_primary: bool = False) -> None:
        super().__init__(text)
        ui_call(self, "setText", text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("PrimaryButton" if is_primary else "SecondaryButton")
        self.setMinimumHeight(30)


from ..gui.paths import PathLineEdit


class DragDropLineEdit(PathLineEdit):
    def __init__(self, accept_mode: str = "any", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.accept_mode = accept_mode
        self.setAcceptDrops(True)
        if accept_mode == "file":
            ui_call(self, 'setPlaceholderText', tr('可直接拖入文件路径...'))
        elif accept_mode == "dir":
            ui_call(self, 'setPlaceholderText', tr('可直接拖入文件夹路径...'))
        else:
            ui_call(self, 'setPlaceholderText', tr('可直接拖入文件或文件夹...'))

    def _matches(self, path: Path) -> bool:
        if self.accept_mode == "file":
            return path.is_file() and path.suffix.lower() == ".bpd"
        if self.accept_mode == "dir":
            return path.is_dir()
        return path.exists()

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                local_path = url.toLocalFile()
                if local_path and self._matches(Path(local_path)):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        for url in event.mimeData().urls():
            local_path = url.toLocalFile()
            if local_path and self._matches(Path(local_path)):
                ui_call(self, 'setText', str(Path(local_path)))
                event.acceptProposedAction()
                return
        event.ignore()


class DecompileWorker(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(
        self,
        mode: str,
        input_path: str,
        output_path: str,
        ir_path: str,
        *,
        ir_only: bool = False,
        include_source_comments: bool = True,
        engine: str = 'auto',
    ) -> None:
        super().__init__()
        self.mode = mode
        self.input_path = input_path.strip()
        self.output_path = output_path.strip()
        self.ir_path = ir_path.strip()
        self.ir_only = ir_only
        self.include_source_comments = include_source_comments
        self.engine = engine

    def log(self, text: str) -> None:
        if text:
            self.log_signal.emit(text.rstrip())

    def run(self) -> None:
        try:
            summary = self._run_single() if self.mode == "single" else self._run_batch()
        except Exception:
            formatted = traceback.format_exc().rstrip()
            self.log(tr('执行失败，详细异常如下：'))
            for line in formatted.splitlines():
                self.log(line)
            self.finished_signal.emit(False, formatted.splitlines()[-1] if formatted else tr('执行失败'))
            return
        self.finished_signal.emit(True, summary)

    def _run_single(self) -> str:
        source = Path(self.input_path)
        if not source.is_file() or source.suffix.lower() != ".bpd":
            raise FileNotFoundError(tr('BPD 输入文件不存在：{0}', source))
        self.log(tr('单文件模式：开始解析 {0}', source))
        result = decompile_file(
            source,
            include_source_comments=self.include_source_comments,
            engine=self.engine,
        )
        if result.assembly.engine_detection:
            self.log(result.assembly.engine_detection.describe())
        stats = result.module.stats
        self.log(tr('CFG：{0} 个基本块，{1} 个原始标签', len(result.module.blocks), result.label_count))
        self.log(tr('语义提升：{0}/{1}，回退 VM 调用 {2}', stats.lifted_count, stats.instruction_count, stats.opaque_count))

        if not self.ir_only:
            output = Path(self.output_path) if self.output_path else source.with_name(source.name + ".c")
            written = write_result(result, output)
            self.log(tr('C 输出：{0}', written))
        if self.ir_only or self.ir_path:
            ir_output = Path(self.ir_path) if self.ir_path else source.with_name(source.name + ".ir.txt")
            ir_written = write_ir_result(result, ir_output)
            self.log(tr('IR 输出：{0}', ir_written))
        return tr('完成：{0}，{1} 条指令', source.name, stats.instruction_count)

    def _run_batch(self) -> str:
        input_dir = Path(self.input_path)
        if not input_dir.is_dir():
            raise NotADirectoryError(tr('BPD 输入目录不存在：{0}', input_dir))
        inputs = iter_input_files(input_dir, recursive=True)
        if not inputs:
            raise FileNotFoundError(tr('目录中没有 .bpd 文件：{0}', input_dir))
        output_dir = Path(self.output_path) if self.output_path else input_dir.parent / f"{input_dir.name}_decompiled"
        ir_dir = Path(self.ir_path) if self.ir_path else input_dir.parent / f"{input_dir.name}_ir"
        self.log(tr('扫描完成：共发现 {0} 个 BPD 文件', len(inputs)))

        results = decompile_files(
            inputs,
            progress=lambda message: self.log(tr('反编译进度：{0}', message)),
            include_source_comments=self.include_source_comments,
            engine=self.engine,
        )
        instruction_count = sum(result.module.stats.instruction_count for result in results)
        goto_count = sum(result.c_source.count("goto ") for result in results)
        for source, result in zip(inputs, results, strict=True):
            relative = source.relative_to(input_dir)
            self.log(tr('写出结果：{0}', relative))
            if not self.ir_only:
                write_result(result, output_path_for_batch(source, input_dir, output_dir))
            if self.ir_only or self.ir_path:
                write_ir_result(result, output_path_for_batch(source, input_dir, ir_dir, ir=True))

        if not self.ir_only:
            self.log(tr('C 输出目录：{0}', output_dir))
        if self.ir_only or self.ir_path:
            self.log(tr('IR 输出目录：{0}', ir_dir))
        return tr('完成：{0} 个文件，{1} 条指令，剩余 goto {2} 个', len(inputs), instruction_count, goto_count)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.worker: DecompileWorker | None = None
        self.input_edit: DragDropLineEdit
        self._auto_output_paths: dict[QLineEdit, str] = {}
        self._manual_output_edits: set[QLineEdit] = set()
        self._updating_auto_paths = False
        self._loading_settings = False
        self.settings_path = resolve_settings_path()
        self.settings = QSettings(str(self.settings_path), QSettings.Format.IniFormat)

        self.setObjectName("MainBackground")
        ui_call(self, 'setWindowTitle', 'bpd2c GUI')
        self.resize(800, 760)
        self.setAcceptDrops(True)

        self._init_ui()
        self._load_settings()
        self.detect_system_theme()

    def _init_ui(self) -> None:
        central_widget = QWidget()
        central_widget.setObjectName("MainBackground")
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(5)

        header_layout = QHBoxLayout()
        title_label = ui_widget(QLabel, 'bpd2c')
        title_label.setObjectName("AppTitle")
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        self.theme_combo = QComboBox()
        ui_call(self.theme_combo, 'addItems', ['跟随系统', '现代浅色', '现代深色', '赛博朋克'])
        self.theme_combo.currentTextChanged.connect(self.apply_theme)
        self.theme_combo.setFixedWidth(120)
        header_layout.addWidget(self.theme_combo)
        main_layout.addLayout(header_layout)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setHandleWidth(2)
        self.content_tabs = QTabWidget()
        ui_call(self.content_tabs, 'addTab', self._build_task_page(), tr('反编译'))
        ui_call(self.content_tabs, 'addTab', self._build_advanced_page(), tr('高级设置'))
        ui_call(self.content_tabs, 'addTab', self._build_help_page(), tr('说明'))
        splitter.addWidget(self.content_tabs)

        log_card = CardFrame()
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)
        log_header = QWidget()
        log_header.setObjectName("LogHeader")
        log_header_layout = QHBoxLayout(log_header)
        log_header_layout.setContentsMargins(10, 5, 10, 5)
        log_header_layout.addWidget(ui_widget(QLabel, tr('运行日志 (Log)')))
        log_header_layout.addStretch()
        clear_button = ui_widget(QPushButton, tr('清除'))
        clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_button.setStyleSheet("border:none; font-weight:bold; color: #888;")
        clear_button.clicked.connect(self.log_view_clear)
        log_header_layout.addWidget(clear_button)
        log_layout.addWidget(log_header)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("LogConsole")
        log_layout.addWidget(self.log_view)
        splitter.addWidget(log_card)
        splitter.setSizes([400, 500])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        main_layout.addWidget(splitter)

    def _build_task_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)
        task_card = CardFrame()
        task_layout = QVBoxLayout(task_card)
        task_layout.setContentsMargins(10, 10, 10, 10)
        task_layout.setSpacing(5)
        task_layout.addWidget(ui_widget(QLabel, tr('反编译')))
        self.input_edit = self._create_path_selector(
            task_layout,
            tr('输入 BPD 或目录'),
            "any",
            "open",
            True,
            "BPD Assembly (*.bpd);;All Files (*)",
        )
        self.single_input_edit = self.input_edit
        self.batch_input_edit = self.input_edit
        self.input_edit.textChanged.connect(self._sync_input_mode)
        task_layout.addWidget(self._tip_label(tr('输入文件时执行单文件反编译，输入目录时自动递归处理。')))

        engine_row = QHBoxLayout()
        engine_row.addWidget(ui_widget(QLabel, tr('BP 引擎版本')))
        self.engine_combo = QComboBox()
        self.engine_combo.setEditable(True)
        ui_call(self.engine_combo, 'addItem', tr('自动识别'), 'auto')
        for name in PROFILES:
            ui_call(self.engine_combo, 'addItem', tr('当前兼容配置（版本未知）') if name == 'current' else name, name)
        engine_row.addWidget(self.engine_combo, 1)
        self.engine_detect_button = ui_widget(QPushButton, tr('识别'))
        self.engine_detect_button.clicked.connect(self._show_engine_detection)
        engine_row.addWidget(self.engine_detect_button)
        task_layout.addLayout(engine_row)

        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(self._build_single_page())
        self.mode_stack.addWidget(self._build_batch_page())
        task_layout.addWidget(self.mode_stack)
        layout.addWidget(task_card)
        layout.addStretch()
        return page

    def _build_single_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.single_output_edit = self._create_path_selector(layout, tr('输出 C 文件'), "any", "save", False, "C Source (*.c);;All Files (*)")
        self.single_ir_edit = self._create_path_selector(layout, tr('输出 IR 文件（可选）'), "any", "save", False, "IR Text (*.txt);;All Files (*)")
        for edit in (self.single_output_edit,):
            self._register_output_edit(edit)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        ir_button = ModernButton(tr('仅生成 IR 诊断'))
        ir_button.clicked.connect(lambda: self._start_worker("single", True))
        self.single_ir_button = ir_button
        button_row.addWidget(ir_button)
        run_button = ModernButton(tr('执行单文件反编译'), is_primary=True)
        run_button.clicked.connect(lambda: self._start_worker("single", False))
        self.single_run_button = run_button
        button_row.addWidget(run_button)
        layout.addLayout(button_row)
        return page

    def _build_batch_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.batch_output_edit = self._create_path_selector(layout, tr('输出目录'), "dir", None, True)
        self.batch_ir_edit = self._create_path_selector(layout, tr('IR 输出目录（可选）'), "dir", None, True)
        for edit in (self.batch_output_edit,):
            self._register_output_edit(edit)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        ir_button = ModernButton(tr('仅生成 IR 诊断'))
        ir_button.clicked.connect(lambda: self._start_worker("batch", True))
        self.batch_ir_button = ir_button
        button_row.addWidget(ir_button)
        run_button = ModernButton(tr('执行批量反编译'), is_primary=True)
        run_button.clicked.connect(lambda: self._start_worker("batch", False))
        self.batch_run_button = run_button
        button_row.addWidget(run_button)
        layout.addLayout(button_row)
        return page

    def _build_advanced_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        semantics_card = CardFrame()
        semantics_layout = QVBoxLayout(semantics_card)
        semantics_layout.setContentsMargins(10, 10, 10, 10)
        semantics_layout.setSpacing(5)
        semantics_layout.addWidget(ui_widget(QLabel, tr('语义恢复')))
        definitions = load_opcode_semantics()
        group_count = sum(item.is_group for item in definitions.values())
        semantics_layout.addWidget(self._tip_label(tr('已加载 {0} 条 opcode 定义，其中组 opcode {1} 条。', len(definitions), group_count)))
        semantics_layout.addWidget(self._tip_label(tr('默认恢复表达式栈、code-ref 局部变量、组调用参数与返回值。')))
        semantics_layout.addWidget(self._tip_label(tr('CFG 区域分析会恢复 if/else、while 和 do/while；不可约边保留 goto。')))
        layout.addWidget(semantics_card)

        output_card = CardFrame()
        output_layout = QVBoxLayout(output_card)
        output_layout.setContentsMargins(10, 10, 10, 10)
        output_layout.setSpacing(5)
        output_layout.addWidget(ui_widget(QLabel, tr('C 输出')))
        self.source_comments_checkbox = ui_widget(QCheckBox, tr('输出 BPD 行号注释'))
        self.source_comments_checkbox.setChecked(True)
        output_layout.addWidget(self.source_comments_checkbox)
        output_layout.addWidget(self._tip_label(tr('关闭后仅隐藏 C 语句末尾的原始 BPD 行号；IR 仍保留完整来源行。')))
        layout.addWidget(output_card)

        source_card = CardFrame()
        source_layout = QVBoxLayout(source_card)
        source_layout.setContentsMargins(10, 10, 10, 10)
        source_layout.setSpacing(5)
        source_layout.addWidget(ui_widget(QLabel, tr('内置语义表')))
        semantics_path = Path(__file__).resolve().parent / "bpd2c" / "opcode_stack_effects.py"
        source_layout.addWidget(self._tip_label(str(semantics_path)))
        source_layout.addWidget(self._tip_label(tr('运行时只读取 Python 模块，不依赖外部定义文档或反编译数据库。')))
        source_layout.addWidget(self._tip_label(tr('INI 路径：{0}', self.settings_path)))
        layout.addWidget(source_card)
        layout.addStretch()
        return page

    def _build_help_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)
        help_card = CardFrame()
        help_layout = QVBoxLayout(help_card)
        help_layout.setContentsMargins(10, 10, 10, 10)
        help_layout.setSpacing(5)
        help_layout.addWidget(ui_widget(QLabel, tr('说明')))
        help_layout.addWidget(self._tip_label(tr('1. 输入 .bpd 时处理单个文件；输入目录时自动递归扫描并保持相对路径。')))
        help_layout.addWidget(self._tip_label(tr('2. C 输出默认使用结构化控制流，IR 文件保留完整基本块、后继边和源行号。')))
        help_layout.addWidget(self._tip_label(tr('3. 组 opcode 的输入输出来自内置 Python 语义表；不明确的栈契约仍保留 VM 回退。')))
        help_layout.addWidget(self._tip_label(tr('4. 日志会显示基本块、语义提升、剩余 VM 调用和 goto 数量。')))
        help_layout.addWidget(self._tip_label(tr('5. 也可在本目录执行 python -m bpd2c.cli <文件或目录>。')))
        layout.addWidget(help_card)
        layout.addStretch()
        return page

    def _tip_label(self, text: str) -> QLabel:
        label = ui_widget(QLabel, text)
        label.setWordWrap(True)
        label.setObjectName("TipLabel")
        return label

    def _create_path_selector(
        self,
        parent_layout: QVBoxLayout,
        label_text: str,
        accept_mode: str,
        file_mode: str | None,
        directory_mode: bool,
        filter_text: str = "All Files (*)",
    ) -> DragDropLineEdit:
        parent_layout.addWidget(ui_widget(QLabel, label_text))
        row = QHBoxLayout()
        row.setSpacing(5)
        edit = DragDropLineEdit(accept_mode)
        row.addWidget(edit, 1)
        if file_mode is not None:
            button = ModernButton(tr('文件'))
            button.setFixedWidth(50)
            button.clicked.connect(lambda: self._browse_file(edit, file_mode, filter_text))
            row.addWidget(button)
        if directory_mode:
            button = ModernButton(tr('目录'))
            button.setFixedWidth(50)
            button.clicked.connect(lambda: self._browse_directory(edit))
            row.addWidget(button)
        parent_layout.addLayout(row)
        return edit

    def _browse_file(self, edit: QLineEdit, mode: str, filter_text: str) -> None:
        current = edit.text().strip()
        start = str(Path(current).parent) if current else str(Path.cwd())
        if mode == "open":
            path, _ = QFileDialog.getOpenFileName(self, tr('选择文件'), start, filter_text)
        else:
            path, _ = QFileDialog.getSaveFileName(self, tr('选择输出文件'), current or start, filter_text)
        if path:
            ui_call(edit, 'setText', str(Path(path)))

    def _browse_directory(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, tr('选择文件夹'), edit.text().strip() or str(Path.cwd()))
        if path:
            ui_call(edit, 'setText', str(Path(path)))

    def _register_output_edit(self, edit: QLineEdit) -> None:
        edit.textChanged.connect(lambda _text, target=edit: self._on_output_path_changed(target))

    def _on_output_path_changed(self, edit: QLineEdit) -> None:
        if self._updating_auto_paths or self._loading_settings:
            return
        if edit.text().strip():
            self._manual_output_edits.add(edit)
        else:
            self._manual_output_edits.discard(edit)

    def _set_auto_output_path(self, edit: QLineEdit, path: Path) -> None:
        value = str(path)
        current = edit.text().strip()
        if edit not in self._manual_output_edits and current != value:
            self._updating_auto_paths = True
            ui_call(edit, 'setText', value)
            self._updating_auto_paths = False
        self._auto_output_paths[edit] = value

    def _input_is_batch(self) -> bool:
        text = self.input_edit.text().strip()
        if not text:
            return False
        path = Path(text)
        if path.is_dir():
            return True
        if path.is_file():
            return False
        return path.suffix.lower() != ".bpd"

    def _sync_input_mode(self) -> None:
        if self._loading_settings:
            return
        self.mode_stack.setCurrentIndex(1 if self._input_is_batch() else 0)
        self._sync_output_paths()

    def _sync_output_paths(self) -> None:
        if self._loading_settings:
            return
        text = self.input_edit.text().strip()
        if not text:
            return
        source = Path(text)
        if self._input_is_batch():
            self._set_auto_output_path(self.batch_output_edit, source.parent / f"{source.name}_decompiled")
            return
        self._set_auto_output_path(self.single_output_edit, source.with_name(source.name + ".c"))

    def _start_worker(self, mode: str, ir_only: bool) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, tr('任务运行中'), tr('请等待当前任务完成。'))
            return
        if mode == "single":
            input_path, output_path, ir_path = self.single_input_edit.text(), self.single_output_edit.text(), self.single_ir_edit.text()
        else:
            input_path, output_path, ir_path = self.batch_input_edit.text(), self.batch_output_edit.text(), self.batch_ir_edit.text()
        if not input_path.strip():
            QMessageBox.warning(self, tr('缺少输入'), tr('请先选择 BPD 文件或目录。'))
            return
        self._save_settings()
        self._set_controls_enabled(False)
        self.worker = self._make_decompile_worker(
            mode,
            input_path,
            output_path,
            ir_path,
            ir_only=ir_only,
            include_source_comments=self.source_comments_checkbox.isChecked(),
            engine=self._selected_engine(),
        )
        self.worker.log_signal.connect(self.append_log)
        self.worker.finished_signal.connect(self._on_worker_finished)
        self.append_log("-" * 72)
        self.append_log(tr('开始执行任务'))
        self.worker.start()

    def _make_decompile_worker(self, *args, **kwargs):
        return DecompileWorker(*args, **kwargs)

    def _selected_engine(self):
        text = self.engine_combo.currentText().strip()
        if text == self.engine_combo.itemText(self.engine_combo.currentIndex()):
            return self.engine_combo.currentData() or text or 'auto'
        return text or 'auto'

    def _show_engine_detection(self):
        try:
            path = Path(self.input_edit.text().strip())
            if not self.input_edit.text().strip() or not path.exists():
                raise ValueError(tr('请先选择存在的 BP / BPD 文件或目录'))
            self.append_log(detect_engine(path, self._selected_engine()).describe())
        except (OSError, ValueError) as error:
            self.append_log(tr('BP 引擎识别失败：{0}', error))

    def _on_worker_finished(self, success: bool, summary: str) -> None:
        self.append_log(summary)
        self.append_log(tr('任务完成') if success else tr('任务失败'))
        self._set_controls_enabled(True)
        self.worker = None
        if not success:
            QMessageBox.critical(self, tr('执行失败'), summary)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.single_run_button.setEnabled(enabled)
        self.single_ir_button.setEnabled(enabled)
        self.batch_run_button.setEnabled(enabled)
        self.batch_ir_button.setEnabled(enabled)
        self.source_comments_checkbox.setEnabled(enabled)
        self.engine_combo.setEnabled(enabled)
        self.engine_detect_button.setEnabled(enabled)

    def append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.append(f"[{timestamp}] {text}")
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)

    def log_view_clear(self) -> None:
        ui_call(self.log_view, 'clear')

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_dir() or (path.is_file() and path.suffix.lower() == ".bpd"):
                ui_call(self.input_edit, 'setText', str(path))
                event.acceptProposedAction()
                return
        event.ignore()

    def _load_settings(self) -> None:
        self._loading_settings = True
        saved_engine = self.settings.value('bp/engine', 'auto', str)
        index = self.engine_combo.findData(saved_engine)
        self.engine_combo.setCurrentIndex(max(0, index))
        if index < 0:
            self.engine_combo.setEditText(saved_engine)
        saved_mode = self.settings.value("ui/mode", "single", str)
        input_value = self.settings.value("input/path", "", str)
        if not input_value:
            legacy_key = "batch/input" if saved_mode == "batch" else "single/input"
            input_value = self.settings.value(legacy_key, "", str)
        ui_call(self.input_edit, 'setText', input_value)
        ui_call(self.single_output_edit, 'setText', self.settings.value('single/output', '', str))
        single_ir_value = self.settings.value("single/ir", "", str)
        if self.settings.value("auto_paths/single_ir", False, bool):
            single_ir_value = ""
        ui_call(self.single_ir_edit, 'setText', single_ir_value)
        ui_call(self.batch_output_edit, 'setText', self.settings.value('batch/output', '', str))
        batch_ir_value = self.settings.value("batch/ir", "", str)
        if self.settings.value("auto_paths/batch_ir", False, bool):
            batch_ir_value = ""
        ui_call(self.batch_ir_edit, 'setText', batch_ir_value)
        self.source_comments_checkbox.setChecked(
            self.settings.value("output/source_comments", True, bool)
        )
        self._loading_settings = False
        for key, edit in (
            ("single_output", self.single_output_edit),
            ("batch_output", self.batch_output_edit),
        ):
            if edit.text().strip() and not self.settings.value(f"auto_paths/{key}", False, bool):
                self._manual_output_edits.add(edit)
        self._sync_input_mode()

    def _save_settings(self) -> None:
        self.settings.setValue('bp/engine', self._selected_engine())
        self.settings.setValue("input/path", self.input_edit.text())
        self.settings.setValue("single/input", self.input_edit.text())
        self.settings.setValue("single/output", self.single_output_edit.text())
        self.settings.setValue("single/ir", self.single_ir_edit.text())
        self.settings.setValue("batch/input", self.input_edit.text())
        self.settings.setValue("batch/output", self.batch_output_edit.text())
        self.settings.setValue("batch/ir", self.batch_ir_edit.text())
        for key, edit in (
            ("single_output", self.single_output_edit),
            ("batch_output", self.batch_output_edit),
        ):
            self.settings.setValue(f"auto_paths/{key}", edit not in self._manual_output_edits)
        self.settings.setValue("auto_paths/single_ir", False)
        self.settings.setValue("auto_paths/batch_ir", False)
        self.settings.setValue("output/source_comments", self.source_comments_checkbox.isChecked())
        self.settings.setValue("ui/theme", self.theme_combo.currentText())
        self.settings.setValue("ui/mode", "batch" if self._input_is_batch() else "single")
        self.settings.sync()

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._save_settings()
        super().closeEvent(event)

    def detect_system_theme(self) -> None:
        theme = self.settings.value("ui/theme", "跟随系统", str)
        if theme not in {"跟随系统", "现代浅色", "现代深色", "赛博朋克"}:
            theme = "跟随系统"
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentText(theme)
        self.theme_combo.blockSignals(False)
        self.apply_theme(theme)

    def _is_system_dark_theme(self) -> bool:
        if HAS_DARKDETECT:
            try:
                detected = darkdetect.isDark()
                if detected is not None:
                    return bool(detected)
            except Exception:
                pass
        if winreg is not None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                    value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return int(value) == 0
            except Exception:
                pass
        return self.palette().window().color().lightness() < 128

    def _resolve_theme_name(self, theme_name: str) -> str:
        if theme_name == "跟随系统":
            return "现代深色" if self._is_system_dark_theme() else "现代浅色"
        return theme_name

    def apply_theme(self, theme_name: str) -> None:
        real_theme = self._resolve_theme_name(theme_name)
        light_qss = """
        QWidget { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; font-size: 10pt; color: #333333; }
        QWidget#MainBackground { background-color: #f5f7fa; }
        QFrame#CardFrame { background-color: #ffffff; border: 1px solid #e1e4e8; border-radius: 8px; }
        QLabel#AppTitle { font-size: 18pt; font-weight: bold; color: #2c3e50; }
        QLineEdit { padding: 8px; border: 1px solid #ced4da; border-radius: 4px; background: #ffffff; color: #333; }
        QLineEdit:focus { border: 1px solid #3498db; }
        QPushButton#SecondaryButton { background-color: #ffffff; border: 1px solid #dcdfe6; border-radius: 4px; color: #606266; padding: 6px 12px; }
        QPushButton#SecondaryButton:hover { border-color: #c6e2ff; color: #409eff; background-color: #ecf5ff; }
        QPushButton#PrimaryButton { background-color: #3498db; border: 1px solid #3498db; border-radius: 4px; color: #ffffff; font-weight: bold; padding: 8px 16px; }
        QPushButton#PrimaryButton:hover { background-color: #5dade2; border-color: #5dade2; }
        QTabWidget::pane { border: 1px solid #e1e4e8; background: #fff; border-radius: 5px; }
        QTabBar::tab { background: #e8ebf0; color: #666; padding: 10px 20px; margin-right: 2px; border-top-left-radius: 4px; border-top-right-radius: 4px; }
        QTabBar::tab:selected { background: #ffffff; color: #3498db; font-weight: bold; }
        QTextEdit#LogConsole { background-color: #fcfcfc; color: #333333; border: 1px solid #e1e4e8; font-family: 'Consolas', monospace; font-size: 9pt; }
        QWidget#LogHeader { background-color: #f1f1f1; border-bottom: 1px solid #ddd; }
        QComboBox { padding: 4px; color: #333; background: #fff; border: 1px solid #ced4da; border-radius: 4px; }
        QComboBox QAbstractItemView { background-color: #ffffff; color: #333333; selection-background-color: #e6f7ff; selection-color: #333333; }
        QLabel#TipLabel { font-size: 9pt; color: #888888; }
        """
        dark_qss = """
        QWidget { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; font-size: 10pt; color: #e0e0e0; }
        QWidget#MainBackground { background-color: #1e1e1e; }
        QFrame#CardFrame { background-color: #2d2d2d; border: 1px solid #444; border-radius: 8px; }
        QLabel { color: #e0e0e0; }
        QLabel#AppTitle { color: #ffffff; font-size: 18pt; font-weight: bold; }
        QLineEdit { background: #1a1a1a; border: 1px solid #555; border-radius: 4px; color: #ffffff; padding: 8px; }
        QLineEdit:focus { border: 1px solid #bb86fc; }
        QPushButton#SecondaryButton { background: #333; border: 1px solid #555; color: #ddd; border-radius: 4px; }
        QPushButton#SecondaryButton:hover { background: #444; border-color: #777; }
        QPushButton#PrimaryButton { background: #bb86fc; border: 1px solid #bb86fc; color: #121212; border-radius: 4px; font-weight:bold; }
        QPushButton#PrimaryButton:hover { background: #d0aaff; }
        QTabWidget::pane { border: 1px solid #444; background: #2d2d2d; }
        QTabBar::tab { background: #1e1e1e; color: #999; padding: 10px 20px; border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right:2px; }
        QTabBar::tab:selected { background: #2d2d2d; color: #bb86fc; font-weight:bold; }
        QTextEdit#LogConsole { background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #444; font-family: 'Consolas', monospace; font-size: 9pt; }
        QWidget#LogHeader { background-color: #252525; border-bottom: 1px solid #444; }
        QComboBox { padding: 4px; color: #e0e0e0; background: #333; border: 1px solid #555; border-radius: 4px; }
        QComboBox QAbstractItemView { background-color: #2d2d2d; color: #e0e0e0; selection-background-color: #bb86fc; selection-color: #121212; }
        QLabel#TipLabel { color: #999999; font-size: 9pt; }
        """
        cyber_qss = """
        QWidget { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; font-size: 10pt; color: #00ffcc; }
        QWidget#MainBackground { background-color: #0d0d15; }
        QFrame#CardFrame { background-color: #1a1a2e; border: 1px solid #00ffcc; border-radius: 8px; }
        QLabel { color: #00ffcc; }
        QLabel#AppTitle { color: #ff00ff; font-size: 18pt; font-weight: bold; }
        QLineEdit { background: #0f0f1a; border: 1px solid #ff00ff; border-radius: 4px; color: #00ffcc; padding: 8px; }
        QPushButton#SecondaryButton { background: #0b0b19; border: 1px solid #00ffcc; color: #00ffcc; }
        QPushButton#PrimaryButton { background: #ff0055; border: 1px solid #ff0055; color: #ffffff; font-weight: bold; }
        QTabWidget::pane { border: 1px solid #00ffcc; background: #0d0d15; }
        QTabBar::tab { background: #0d0d15; color: #008888; border: 1px solid #004444; padding: 10px; }
        QTabBar::tab:selected { color: #00ffcc; border: 1px solid #00ffcc; }
        QTextEdit#LogConsole { background-color: #0f0f1f; color: #00ffcc; border: 1px solid #00ffcc; }
        QWidget#LogHeader { background-color: #121225; border-bottom: 1px solid #00ffcc; }
        QComboBox { background: #0d0d15; color: #00ffcc; border: 1px solid #00ffcc; }
        QComboBox QAbstractItemView { background-color: #0d0d15; color: #00ffcc; selection-background-color: #ff0055; }
        QLabel#TipLabel { color: #0088aa; font-size: 9pt; }
        """
        if real_theme == "现代深色":
            self.setStyleSheet(dark_qss)
        elif real_theme == "赛博朋克":
            self.setStyleSheet(cyber_qss)
        else:
            self.setStyleSheet(light_qss)


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv or sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
