from __future__ import annotations
from ..gui.localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

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
        QTableWidget,
        QTableWidgetItem,
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

from ..bsd2c.definitions import derive_definitions_path, write_project_definitions
from ..bsd2c.ignore_config import (
    DEFAULT_BSC_IGNORED_BUILTINS,
    DEFAULT_BS5_LIKE_IGNORED_BUILTINS,
    KNOWN_IGNORED_BUILTINS,
    IgnoreBuiltinConfig,
    normalize_ignored_builtin_names,
)
from ..bsd2c.pipeline import (
    OutputFormat,
    TextReplacementRule,
    build_project_rpy_context,
    decompile_file,
    decompile_files,
    derive_ir_output_path,
    derive_output_path_for_format,
    scan_definition_bundles,
    write_definition_bundle,
    write_ir_result,
    write_result,
)
from ..bsd2c.asm_parser import parse_assembly_file
from ..bsd2c.rpy_emitter import RpyEmitOptions


IGNORE_FUNCTION_DESCRIPTIONS = {
    "NotifyOfFunctionName": "函数名元数据",
    "NotifyOfLineCount": "源码行数元数据",
    "RegisterLabel": "标签注册元数据",
    "07b": "特殊内建元数据/内联参数调用",
    "07e": "读取并丢弃脚本流参数的元数据指令",
}


def resolve_settings_path() -> Path:
    root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)) / "bgi_toolkit"
    root.mkdir(parents=True, exist_ok=True)
    return root / "bsd2c-gui.ini"


def split_ignore_names_text(text: str) -> list[str]:
    parts = text.replace("\n", ",").replace(";", ",").split(",")
    ordered: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = part.strip()
        if not name or name in seen:
            continue
        ordered.append(name)
        seen.add(name)
    return ordered


def normalize_output_format(value: str | None) -> OutputFormat:
    return "rpy" if value == "rpy" else "c"


class CardFrame(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("CardFrame")
        self.setFrameShape(QFrame.Shape.NoFrame)


class ModernButton(QPushButton):
    def __init__(self, text: str, is_primary: bool = False):
        super().__init__(text)
        ui_call(self, "setText", text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("PrimaryButton" if is_primary else "SecondaryButton")
        self.setMinimumHeight(30)


from ..gui.paths import PathLineEdit


class DragDropLineEdit(PathLineEdit):
    def __init__(self, accept_mode: str = "any", parent: QWidget | None = None):
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
            return path.is_file()
        if self.accept_mode == "dir":
            return path.is_dir()
        return path.exists()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                local_path = url.toLocalFile()
                if local_path and self._matches(Path(local_path)):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            local_path = url.toLocalFile()
            if not local_path:
                continue
            path = Path(local_path)
            if self._matches(path):
                ui_call(self, 'setText', str(path))
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
        definitions_path: str,
        hint: str,
        output_format: str,
        action: str = "decompile",
        ignore_builtin_config: IgnoreBuiltinConfig | None = None,
        rpy_emit_options: RpyEmitOptions | None = None,
        replacement_rules: tuple[TextReplacementRule, ...] = (),
    ):
        super().__init__()
        self.mode = mode
        self.input_path = input_path.strip()
        self.output_path = output_path.strip()
        self.ir_path = ir_path.strip()
        self.definitions_path = definitions_path.strip()
        self.hint = hint.strip() or "auto"
        self.output_format: OutputFormat = normalize_output_format(output_format.strip() or "c")
        self.action = action
        self.ignore_builtin_config = ignore_builtin_config
        self.rpy_emit_options = rpy_emit_options or RpyEmitOptions()
        self.replacement_rules = replacement_rules

    def log(self, message: str) -> None:
        if message:
            self.log_signal.emit(message.rstrip())

    def run(self) -> None:
        try:
            if self.action == "scan":
                summary = self._run_single_scan() if self.mode == "single" else self._run_batch_scan()
            else:
                summary = self._run_single() if self.mode == "single" else self._run_batch()
        except Exception:
            formatted = traceback.format_exc().rstrip()
            self.log(tr('执行失败，详细异常如下：'))
            for line in formatted.splitlines():
                self.log(line)
            last_line = formatted.splitlines()[-1] if formatted else tr('执行失败')
            self.finished_signal.emit(False, last_line)
            return

        self.finished_signal.emit(True, summary)

    def _override(self) -> str | None:
        return None if self.hint == "auto" else self.hint

    def _log_inference_progress(self, pass_index: int, index: int, total: int, path: Path) -> None:
        self.log(tr('推断进度：第 {0} 轮 {1}/{2}，当前 {3}', pass_index, index, total, path.name))

    def _custom_definitions_path(self) -> Path | None:
        return Path(self.definitions_path) if self.definitions_path else None

    def _default_batch_output_dir(self, input_dir: Path) -> Path:
        return Path(self.output_path) if self.output_path else input_dir.parent / f"{input_dir.name}_decompiled"

    def _default_batch_definitions_path(self, input_dir: Path) -> Path:
        return self._default_batch_output_dir(input_dir) / "bsd2c_defs.json"

    def _resolve_single_scan_definitions_target(self, input_path: Path) -> Path:
        return self._custom_definitions_path() or derive_definitions_path(input_path)

    def _scan_and_write_definitions(
        self,
        source_paths: list[Path],
        definitions_override_path: Path | None,
    ) -> list[Path]:
        scan_results = scan_definition_bundles(
            source_paths,
            self._override(),
            ignore_builtin_config=self.ignore_builtin_config,
            inference_progress_callback=self._log_inference_progress,
            definitions_override_path=definitions_override_path,
        )
        written_paths: list[Path] = []
        for scan_result in scan_results:
            scan_result.definitions_path.parent.mkdir(parents=True, exist_ok=True)
            written = write_project_definitions(scan_result.project_definitions, scan_result.definitions_path)
            self.log(
                tr('扫描完成：{0}，外部函数 {1} 个，用户函数 {2} 个', written, len(scan_result.project_definitions.external_signatures), len(scan_result.project_definitions.user_signatures))
            )
            written_paths.append(written)
        return written_paths

    def _prepare_single_definitions_for_decompile(self, input_path: Path) -> Path:
        definitions_path = self._resolve_single_scan_definitions_target(input_path)
        if definitions_path.exists():
            self.log(tr('发现现成签名 JSON，直接复用：{0}', definitions_path))
            return definitions_path

        self.log(tr('未找到签名 JSON，将在本次反编译中完成推断：{0}', definitions_path))
        return definitions_path

    def _prepare_batch_definitions_for_decompile(self, input_dir: Path, inputs: list[Path]) -> Path | None:
        custom_path = self._custom_definitions_path()
        if custom_path is not None:
            if custom_path.exists():
                self.log(tr('发现现成签名 JSON，直接复用：{0}', custom_path))
                return custom_path
            self.log(tr('未找到签名 JSON，将在本次反编译中完成推断：{0}', custom_path))
            return custom_path

        bundled_path = self._default_batch_definitions_path(input_dir)
        if bundled_path.exists():
            self.log(tr('发现现成签名 JSON，直接复用：{0}', bundled_path))
            return bundled_path

        grouped_inputs: dict[Path, list[Path]] = {}
        for path in inputs:
            grouped_inputs.setdefault(derive_definitions_path(path), []).append(path)

        existing_group_paths = [definitions_path for definitions_path in grouped_inputs if definitions_path.exists()]
        missing_inputs = [path for definitions_path, paths in grouped_inputs.items() if not definitions_path.exists() for path in paths]

        if existing_group_paths and not missing_inputs:
            self.log(tr('发现输入目录内现成签名 JSON，直接按脚本目录分组复用'))
            return None

        if existing_group_paths and missing_inputs:
            self.log(tr('部分脚本目录缺少签名 JSON，将在本次反编译中补全'))
            return None

        self.log(tr('未找到签名 JSON，将在本次反编译中完成推断：{0}', bundled_path))
        return bundled_path

    def _resolve_batch_definitions_target(self, input_dir: Path) -> Path:
        return self._custom_definitions_path() or self._default_batch_definitions_path(input_dir)

    def _scan_directory_inputs(self, input_dir: Path) -> list[Path]:
        self.log(tr('开始扫描目录：{0}', input_dir))
        inputs: list[Path] = []
        for candidate in sorted(input_dir.rglob("*")):
            if not candidate.is_file():
                continue
            if candidate.suffix != ".bsd" and not candidate.name.endswith(".bsd.txt"):
                continue
            inputs.append(candidate)
            if len(inputs) <= 5 or len(inputs) % 20 == 0:
                try:
                    relative_name = candidate.relative_to(input_dir)
                except ValueError:
                    relative_name = candidate.name
                self.log(tr('扫描进度：已发现 {0} 个脚本，当前 {1}', len(inputs), relative_name))
        self.log(tr('扫描完成：共发现 {0} 个脚本文件', len(inputs)))
        return inputs

    def _relative_output_path(
        self,
        input_dir: Path,
        source_path: Path,
        detected_type: str,
        output_format: OutputFormat = "c",
    ) -> Path:
        try:
            relative_source = source_path.relative_to(input_dir)
        except ValueError:
            relative_source = Path(source_path.name)
        return derive_output_path_for_format(relative_source, detected_type, output_format, self.rpy_emit_options)

    def _run_single(self) -> str:
        input_path = Path(self.input_path)
        if not input_path.is_file():
            raise FileNotFoundError(tr('输入文件不存在：{0}', input_path))

        self.log(tr('单文件模式：开始处理 {0}', input_path))
        self.log(tr('扫描进度：单文件模式无需批量扫描'))
        self.log(tr('开始准备项目签名...'))
        definitions_override = self._prepare_single_definitions_for_decompile(input_path)
        self.log(tr('使用签名 JSON：{0}', definitions_override))
        result = decompile_file(
            input_path,
            self._override(),
            ignore_builtin_config=self.ignore_builtin_config,
            inference_progress_callback=self._log_inference_progress,
            definitions_override_path=definitions_override,
        )
        self.log(tr('推断完成：识别类型为 {0}', result.detected_type))
        project_context = build_project_rpy_context([result], self.rpy_emit_options) if self.output_format == "rpy" else None

        output_target = Path(self.output_path) if self.output_path else None
        if output_target is not None:
            output_target.parent.mkdir(parents=True, exist_ok=True)
        written = write_result(
            result,
            output_target,
            output_format=self.output_format,
            project_context=project_context,
            rpy_emit_options=self.rpy_emit_options,
            replacement_rules=self.replacement_rules,
        )
        self.log(tr('已写出 {0} 输出：{1}', self.output_format.upper(), written))

        if self.ir_path:
            ir_target = Path(self.ir_path)
            ir_target.parent.mkdir(parents=True, exist_ok=True)
            ir_written = write_ir_result(result, ir_target)
            self.log(tr('已写出 IR：{0}', ir_written))

        definitions_written = write_definition_bundle([result], definitions_override)
        self.log(tr('已写出 definitions：{0}', definitions_written))
        return tr('单文件处理完成：{0}', written.name)

    def _run_single_scan(self) -> str:
        input_path = Path(self.input_path)
        if not input_path.is_file():
            raise FileNotFoundError(tr('输入文件不存在：{0}', input_path))

        definitions_target = self._resolve_single_scan_definitions_target(input_path)
        self.log(tr('单文件签名扫描：开始处理 {0}', input_path))
        self.log(tr('签名 JSON 输出：{0}', definitions_target))
        [written] = self._scan_and_write_definitions([input_path], definitions_target)
        self.log(tr('已写出签名 JSON：{0}', written))
        return tr('单文件扫描完成：{0}', written.name)

    def _run_batch(self) -> str:
        input_dir = Path(self.input_path)
        if not input_dir.is_dir():
            raise NotADirectoryError(tr('输入目录不存在：{0}', input_dir))

        inputs = self._scan_directory_inputs(input_dir)
        if not inputs:
            raise ValueError(tr('目录下没有找到 .bsd 或 .bsd.txt 文件：{0}', input_dir))

        output_dir = self._default_batch_output_dir(input_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        ir_dir: Path | None = None
        if self.ir_path:
            ir_dir = Path(self.ir_path)
            ir_dir.mkdir(parents=True, exist_ok=True)

        self.log(tr('开始准备项目签名...'))
        definitions_override = self._prepare_batch_definitions_for_decompile(input_dir, inputs)
        if definitions_override is not None:
            self.log(tr('使用签名 JSON：{0}', definitions_override))
        results = decompile_files(
            inputs,
            self._override(),
            ignore_builtin_config=self.ignore_builtin_config,
            inference_progress_callback=self._log_inference_progress,
            definitions_override_path=definitions_override,
        )
        self.log(tr('推断完成：共生成 {0} 个反编译结果', len(results)))

        self.log(tr('开始生成反编译结果...'))
        total = len(inputs)
        project_context = None

        if self.output_format == "rpy":
            project_context = build_project_rpy_context(results, self.rpy_emit_options)

        for index, (source_path, result) in enumerate(zip(inputs, results, strict=False), start=1):

            relative_output = self._relative_output_path(input_dir, source_path, result.detected_type, self.output_format)
            destination = output_dir / relative_output
            destination.parent.mkdir(parents=True, exist_ok=True)
            written = write_result(
                result,
                destination,
                output_format=self.output_format,
                project_context=project_context,
                rpy_emit_options=self.rpy_emit_options,
                replacement_rules=self.replacement_rules,
            )

            try:
                relative_source = source_path.relative_to(input_dir)
            except ValueError:
                relative_source = Path(source_path.name)
            try:
                relative_written = written.relative_to(output_dir)
            except ValueError:
                relative_written = written

            self.log(tr('写出进度：{0}/{1}，{2} -> {3}', index, total, relative_source, relative_written))
            if ir_dir is not None:
                ir_relative_output = self._relative_output_path(input_dir, source_path, result.detected_type, "c")
                ir_target = ir_dir / derive_ir_output_path(ir_relative_output)
                ir_target.parent.mkdir(parents=True, exist_ok=True)
                ir_written = write_ir_result(result, ir_target)
                try:
                    relative_ir = ir_written.relative_to(ir_dir)
                except ValueError:
                    relative_ir = ir_written
                self.log(tr('IR 输出：{0}', relative_ir))

        definitions_written = write_definition_bundle(results, self._resolve_batch_definitions_target(input_dir))
        self.log(tr('已写出 definitions：{0}', definitions_written))
        return tr('批量处理完成：共 {0} 个文件', len(results))

    def _run_batch_scan(self) -> str:
        input_dir = Path(self.input_path)
        if not input_dir.is_dir():
            raise NotADirectoryError(tr('输入目录不存在：{0}', input_dir))

        inputs = self._scan_directory_inputs(input_dir)
        if not inputs:
            raise ValueError(tr('目录下没有找到 .bsd 或 .bsd.txt 文件：{0}', input_dir))

        definitions_target = self._resolve_batch_definitions_target(input_dir)
        self.log(tr('批量签名扫描：开始处理 {0}', input_dir))
        self.log(tr('签名 JSON 输出：{0}', definitions_target))
        [written] = self._scan_and_write_definitions(inputs, definitions_target)
        self.log(tr('已写出签名 JSON：{0}', written))
        return tr('批量扫描完成：共 {0} 个文件', len(inputs))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker: DecompileWorker | None = None
        self.settings_path = resolve_settings_path()
        self.settings = QSettings(str(self.settings_path), QSettings.Format.IniFormat)

        self.setObjectName("MainBackground")
        ui_call(self, 'setWindowTitle', 'bsd2c GUI')
        self.resize(800, 760)
        self.setAcceptDrops(True)

        self.single_input_edit: DragDropLineEdit
        self.single_output_edit: DragDropLineEdit
        self.single_ir_edit: DragDropLineEdit
        self.single_defs_edit: DragDropLineEdit
        self.single_hint_combo: QComboBox
        self.batch_input_edit: DragDropLineEdit
        self.batch_output_edit: DragDropLineEdit
        self.batch_ir_edit: DragDropLineEdit
        self.batch_defs_edit: DragDropLineEdit
        self.batch_hint_combo: QComboBox
        self.format_combo: QComboBox
        self.single_format_combo: QComboBox
        self.batch_format_combo: QComboBox
        self.rpy_strip_double_zero_checkbox: QCheckBox
        self.rpy_strip_three_digit_prefix_checkbox: QCheckBox
        self.rpy_suppress_bgi_label_registration_checkbox: QCheckBox
        self.rpy_lowercase_labels_checkbox: QCheckBox
        self.mode_stack: QStackedWidget
        self.content_tabs: QTabWidget
        self.replacement_table: QTableWidget
        self.theme_combo: QComboBox
        self.log_view: QTextEdit
        self.single_scan_button: QPushButton
        self.batch_scan_button: QPushButton
        self.bsc_ignore_checkboxes: dict[str, QCheckBox] = {}
        self.bs5_like_ignore_checkboxes: dict[str, QCheckBox] = {}
        self.bsc_ignore_extra_edit: QLineEdit
        self.bs5_like_ignore_extra_edit: QLineEdit
        self.last_ignore_summary_label: QLabel
        self.input_edit: DragDropLineEdit
        self._auto_output_paths: dict[QLineEdit, str] = {}
        self._manual_output_edits: set[QLineEdit] = set()
        self._updating_auto_paths = False
        self._loading_settings = False

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
        title_label = ui_widget(QLabel, 'bsd2c')
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
        ui_call(self.content_tabs, 'addTab', self._build_text_replacement_page(), tr('文本替换'))
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
        log_header_layout.addWidget(ui_widget(QLabel, tr('📝 运行日志 (Log)')))
        log_header_layout.addStretch()
        clear_button = ui_widget(QPushButton, tr('清除'))
        clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_button.setStyleSheet("border:none; font-weight:bold; color: #888;")
        clear_button.clicked.connect(lambda: ui_call(self.log_view, 'clear'))
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

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addWidget(ui_widget(QLabel, tr('输出格式:')))
        self.format_combo = QComboBox()
        ui_call(self.format_combo, 'addItems', ['c', 'rpy'])
        self.format_combo.setCurrentText("c")
        self.format_combo.setFixedWidth(80)
        self.single_format_combo = self.format_combo
        self.batch_format_combo = self.format_combo
        mode_row.addWidget(self.format_combo)
        mode_row.addWidget(self._tip_label(tr("c 保持现有 C-like 输出；rpy 输出 Ren'Py 风格脚本文本。")), 1)
        mode_row.addStretch()
        task_layout.addLayout(mode_row)

        self.input_edit = self._create_path_selector(
            task_layout,
            tr('输入脚本或目录'),
            accept_mode="any",
            file_mode="open",
            directory_mode=True,
            filter_text="BGI Assembly (*.bsd *.txt);;All Files (*)",
        )
        self.single_input_edit = self.input_edit
        self.batch_input_edit = self.input_edit
        self.input_edit.textChanged.connect(self._sync_input_mode)
        self.format_combo.currentTextChanged.connect(self._sync_output_paths)

        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(self._build_single_page())
        self.mode_stack.addWidget(self._build_batch_page())
        task_layout.addWidget(self.mode_stack)

        layout.addWidget(task_card)
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
        help_layout.addWidget(self._tip_label(tr('1. 输入 .bsd / .bsd.txt 时处理单个文件；输入目录时自动递归扫描。')))
        help_layout.addWidget(self._tip_label(tr('2. 高级设置页包含 RPY 输出名称裁剪选项，以及 BSC / BS5-like 两组忽略函数配置。')))
        help_layout.addWidget(
            self._tip_label(
                tr('3. RPY 名称裁剪、标签转小写和忽略 BGI 标签注册只在输出格式为 rpy 时生效；例如 001_prologue_01 可以裁剪成 01_prologue_01 或 prologue_01。')
            )
        )
        help_layout.addWidget(
            self._tip_label(
                tr('4. 忽略函数会在提升阶段被直接跳过，不写入最终 C 输出；BSC 单独一组，BS5 / BSB / BSS 共用一组。')
            )
        )
        help_layout.addWidget(self._tip_label(tr('5. 可以先点“仅扫描导出签名 JSON”，手工修改参数类型/数量/名字后，再执行正式反编译。')))
        help_layout.addWidget(self._tip_label(tr('6. 文本替换页会按字面文本顺序替换最终输出；例如可把 \\\\n 替换成 \\n。')))
        help_layout.addWidget(self._tip_label(tr('7. 正式反编译时如果发现现成的签名 JSON，会直接复用；只有缺失时才会先扫描再继续。')))
        help_layout.addWidget(self._tip_label(tr('8. 日志会显示扫描进度、签名准备进度和写出进度。')))
        help_layout.addWidget(self._tip_label(tr('9. GUI 文件位于 src/gui.py，可直接执行 python src/gui.py，也可用已安装后的 bsd2c-gui 启动。')))
        help_layout.addWidget(self._tip_label(tr('10. 高级设置、文本替换和上次使用的路径配置保存在 INI：{0}', self.settings_path)))
        layout.addWidget(help_card)
        layout.addStretch()
        return page

    def _build_advanced_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        rpy_row = QHBoxLayout()
        rpy_row.setSpacing(5)

        rpy_card = CardFrame()
        rpy_layout = QVBoxLayout(rpy_card)
        rpy_layout.setContentsMargins(10, 10, 10, 10)
        rpy_layout.setSpacing(5)
        rpy_layout.addWidget(ui_widget(QLabel, tr('RPY 输出名称')))

        self.rpy_strip_double_zero_checkbox = ui_widget(QCheckBox, tr('RPY 名称中遇到 00 前缀时自动删掉一个 0'))
        self.rpy_strip_double_zero_checkbox.setChecked(True)
        rpy_layout.addWidget(self.rpy_strip_double_zero_checkbox)

        self.rpy_strip_three_digit_prefix_checkbox = ui_widget(QCheckBox, tr('RPY 名称中遇到三位数字+下划线前缀时自动去掉'))
        self.rpy_strip_three_digit_prefix_checkbox.setChecked(False)
        rpy_layout.addWidget(self.rpy_strip_three_digit_prefix_checkbox)
        rpy_row.addWidget(rpy_card, 1)

        rpy_options_card = CardFrame()
        rpy_options_layout = QVBoxLayout(rpy_options_card)
        rpy_options_layout.setContentsMargins(10, 10, 10, 10)
        rpy_options_layout.setSpacing(5)
        rpy_options_layout.addWidget(ui_widget(QLabel, tr('RPY 输出选项')))
        self.rpy_suppress_bgi_label_registration_checkbox = ui_widget(QCheckBox, tr('忽略 BGI 标签注册'))
        self.rpy_suppress_bgi_label_registration_checkbox.setChecked(True)
        rpy_options_layout.addWidget(self.rpy_suppress_bgi_label_registration_checkbox)
        self.rpy_lowercase_labels_checkbox = ui_widget(QCheckBox, tr('所有 RPY 标签和跳转目标转为小写'))
        self.rpy_lowercase_labels_checkbox.setChecked(False)
        rpy_options_layout.addWidget(self.rpy_lowercase_labels_checkbox)
        rpy_options_layout.addStretch()
        rpy_row.addWidget(rpy_options_card, 1)
        layout.addLayout(rpy_row)

        summary_card = CardFrame()
        summary_layout = QVBoxLayout(summary_card)
        summary_layout.setContentsMargins(10, 10, 10, 10)
        summary_layout.setSpacing(5)
        summary_layout.addWidget(ui_widget(QLabel, tr('高级设置')))
        summary_layout.addWidget(self._tip_label(tr('INI 路径：{0}', self.settings_path)))
        self.last_ignore_summary_label = self._tip_label("")
        summary_layout.addWidget(self.last_ignore_summary_label)
        layout.addWidget(summary_card)

        group_splitter = QSplitter(Qt.Orientation.Horizontal)
        group_splitter.setObjectName("IgnoreGroupSplitter")
        group_splitter.setChildrenCollapsible(False)

        bsc_card = self._build_ignore_group_card(
            tr('BSC 忽略函数'),
            tr('只作用于 bsc 脚本。'),
            self.bsc_ignore_checkboxes,
            "bsc_ignore_extra_edit",
        )
        bs5_like_card = self._build_ignore_group_card(
            tr('BS5 / BSB / BSS 忽略函数'),
            tr('这三种脚本共用同一组忽略函数。'),
            self.bs5_like_ignore_checkboxes,
            "bs5_like_ignore_extra_edit",
        )

        group_splitter.addWidget(bsc_card)
        group_splitter.addWidget(bs5_like_card)
        group_splitter.setStretchFactor(0, 1)
        group_splitter.setStretchFactor(1, 1)
        group_splitter.setSizes([1, 1])

        layout.addWidget(group_splitter, 1)
        layout.addStretch()
        return page

    def _build_text_replacement_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        card = CardFrame()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 10, 10, 10)
        card_layout.setSpacing(8)
        card_layout.addWidget(ui_widget(QLabel, tr('文本替换')))
        card_layout.addWidget(
            self._tip_label(tr('按字面文本顺序替换最终输出内容，例如把 \\\\n 替换成 \\n。这里的 \\n 会保持为两个字符，不会自动变成实际换行。'))
        )

        self.replacement_table = QTableWidget(0, 3)
        ui_call(self.replacement_table, 'setHorizontalHeaderLabels', [tr('启用'), tr('查找'), tr('替换为')])
        vertical_header = self.replacement_table.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
            vertical_header.setDefaultSectionSize(36)
            vertical_header.setMinimumSectionSize(36)
        self.replacement_table.setAlternatingRowColors(True)
        self.replacement_table.setStyleSheet("QTableWidget QLineEdit { padding: 4px; margin: 0px; min-height: 0px; }")
        self.replacement_table.setColumnWidth(0, 60)
        self.replacement_table.setColumnWidth(1, 220)
        horizontal_header = self.replacement_table.horizontalHeader()
        if horizontal_header is not None:
            horizontal_header.setStretchLastSection(True)
        card_layout.addWidget(self.replacement_table)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        button_row.setContentsMargins(0, 0, 0, 0)

        add_button = ModernButton(tr('新增'))
        add_button.clicked.connect(lambda: self._append_replacement_row())
        button_row.addWidget(add_button)

        delete_button = ModernButton(tr('删除'))
        delete_button.clicked.connect(self._delete_selected_replacement_row)
        button_row.addWidget(delete_button)

        up_button = ModernButton(tr('上移'))
        up_button.clicked.connect(lambda: self._move_selected_replacement_row(-1))
        button_row.addWidget(up_button)

        down_button = ModernButton(tr('下移'))
        down_button.clicked.connect(lambda: self._move_selected_replacement_row(1))
        button_row.addWidget(down_button)

        reset_button = ModernButton(tr('恢复示例'))
        reset_button.clicked.connect(lambda: self._set_replacement_rows(self._default_replacement_rows()))
        button_row.addWidget(reset_button)
        button_row.addStretch()

        card_layout.addLayout(button_row)
        layout.addWidget(card)
        layout.addStretch()
        return page

    def _default_replacement_rows(self) -> list[dict[str, str | bool]]:
        return [{"enabled": True, "find": "\\\\n", "replace": "\\n"}]

    def _create_replacement_enabled_item(self, enabled: bool) -> QTableWidgetItem:
        item = ui_widget(QTableWidgetItem)
        item.setFlags((item.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable)
        item.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        return item

    def _append_replacement_row(self, enabled: bool = True, find: str = "", replace: str = "") -> None:
        row = self.replacement_table.rowCount()
        self.replacement_table.insertRow(row)
        self.replacement_table.setRowHeight(row, 36)
        self.replacement_table.setItem(row, 0, self._create_replacement_enabled_item(enabled))
        self.replacement_table.setItem(row, 1, ui_widget(QTableWidgetItem, find))
        self.replacement_table.setItem(row, 2, ui_widget(QTableWidgetItem, replace))
        self.replacement_table.setCurrentCell(row, 1)

    def _set_replacement_rows(self, rows: list[dict[str, str | bool]]) -> None:
        self.replacement_table.setRowCount(0)
        for row in rows:
            if not isinstance(row, dict):
                continue
            self._append_replacement_row(
                enabled=bool(row.get("enabled", True)),
                find=str(row.get("find", "")),
                replace=str(row.get("replace", "")),
            )

    def _replacement_rows_for_storage(self) -> list[dict[str, str | bool]]:
        rows: list[dict[str, str | bool]] = []
        for row in range(self.replacement_table.rowCount()):
            enabled_item = self.replacement_table.item(row, 0)
            find_item = self.replacement_table.item(row, 1)
            replace_item = self.replacement_table.item(row, 2)
            rows.append(
                {
                    "enabled": enabled_item is not None and enabled_item.checkState() == Qt.CheckState.Checked,
                    "find": find_item.text() if find_item is not None else "",
                    "replace": replace_item.text() if replace_item is not None else "",
                }
            )
        return rows

    def _current_replacement_rules(self) -> tuple[TextReplacementRule, ...]:
        rules: list[TextReplacementRule] = []
        for row in self._replacement_rows_for_storage():
            if not row["enabled"]:
                continue
            find = str(row["find"])
            if not find:
                continue
            rules.append(TextReplacementRule(find=find, replace=str(row["replace"])))
        return tuple(rules)

    def _delete_selected_replacement_row(self) -> None:
        row = self.replacement_table.currentRow()
        if row >= 0:
            self.replacement_table.removeRow(row)

    def _move_selected_replacement_row(self, offset: int) -> None:
        row = self.replacement_table.currentRow()
        target_row = row + offset
        if row < 0 or target_row < 0 or target_row >= self.replacement_table.rowCount():
            return
        rows = self._replacement_rows_for_storage()
        rows[row], rows[target_row] = rows[target_row], rows[row]
        self._set_replacement_rows(rows)
        self.replacement_table.setCurrentCell(target_row, 1)

    def _build_ignore_group_card(
        self,
        title: str,
        _description: str,
        target_checkboxes: dict[str, QCheckBox],
        extra_edit_attr: str,
    ) -> QWidget:
        card = CardFrame()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)
        layout.addWidget(ui_widget(QLabel, title))

        for name in KNOWN_IGNORED_BUILTINS:
            description_text = tr(IGNORE_FUNCTION_DESCRIPTIONS.get(name, ""))
            label_text = name if not description_text else tr('{0}  ({1})', name, description_text)
            checkbox = ui_widget(QCheckBox, label_text)
            target_checkboxes[name] = checkbox
            layout.addWidget(checkbox)

        layout.addWidget(ui_widget(QLabel, tr('额外忽略函数(可选，逗号分隔):')))
        extra_edit = QLineEdit()
        ui_call(extra_edit, 'setPlaceholderText', tr('例如：Foo, Bar'))
        setattr(self, extra_edit_attr, extra_edit)
        layout.addWidget(extra_edit)
        return card

    def _build_single_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        self.single_output_edit = self._create_path_selector(
            layout,
            tr('输出文件'),
            accept_mode="any",
            file_mode="save",
            directory_mode=False,
            filter_text="Output Files (*.c *.rpy);;All Files (*)",
        )
        self.single_ir_edit = self._create_path_selector(
            layout,
            tr('输出 IR 文件（可选）'),
            accept_mode="any",
            file_mode="save",
            directory_mode=False,
            filter_text="IR Text (*.txt);;All Files (*)",
        )
        self.single_defs_edit = self._create_path_selector(
            layout,
            tr('签名 JSON'),
            accept_mode="any",
            file_mode="save",
            directory_mode=False,
            filter_text="JSON Files (*.json);;All Files (*)",
        )
        for edit in (self.single_output_edit, self.single_defs_edit):
            self._register_output_edit(edit)
        self.single_hint_combo = self._create_hint_row(layout)
        self.single_hint_combo.currentTextChanged.connect(self._sync_output_paths)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        button_row.setContentsMargins(0, 0, 0, 0)

        scan_button = ModernButton(tr('仅扫描导出签名 JSON'))
        scan_button.clicked.connect(self.run_single_signature_scan)
        self.single_scan_button = scan_button
        button_row.addWidget(scan_button)

        run_button = ModernButton(tr('执行单文件反编译'), is_primary=True)
        run_button.clicked.connect(self.run_single_decompile)
        self.single_run_button = run_button
        button_row.addWidget(run_button)
        layout.addLayout(button_row)
        return page

    def _build_batch_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        self.batch_output_edit = self._create_path_selector(
            layout,
            tr('输出目录'),
            accept_mode="dir",
            file_mode=None,
            directory_mode=True,
        )
        self.batch_ir_edit = self._create_path_selector(
            layout,
            tr('IR 输出目录（可选）'),
            accept_mode="dir",
            file_mode=None,
            directory_mode=True,
        )
        self.batch_defs_edit = self._create_path_selector(
            layout,
            tr('签名 JSON'),
            accept_mode="any",
            file_mode="save",
            directory_mode=False,
            filter_text="JSON Files (*.json);;All Files (*)",
        )
        for edit in (self.batch_output_edit, self.batch_defs_edit):
            self._register_output_edit(edit)
        self.batch_hint_combo = self._create_hint_row(layout)
        self.batch_hint_combo.currentTextChanged.connect(self._sync_output_paths)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        button_row.setContentsMargins(0, 0, 0, 0)

        scan_button = ModernButton(tr('仅扫描导出签名 JSON'))
        scan_button.clicked.connect(self.run_batch_signature_scan)
        self.batch_scan_button = scan_button
        button_row.addWidget(scan_button)

        run_button = ModernButton(tr('执行批量反编译'), is_primary=True)
        run_button.clicked.connect(self.run_batch_decompile)
        self.batch_run_button = run_button
        button_row.addWidget(run_button)
        layout.addLayout(button_row)
        return page

    def _tip_label(self, text: str) -> QLabel:
        label = ui_widget(QLabel, text)
        label.setWordWrap(True)
        label.setObjectName("TipLabel")
        return label

    def _create_hint_row(self, parent_layout: QVBoxLayout) -> QComboBox:
        parent_layout.addWidget(ui_widget(QLabel, tr('源类型 Hint:')))
        row = QHBoxLayout()
        row.setSpacing(5)
        row.setContentsMargins(0, 0, 0, 0)
        combo = QComboBox()
        ui_call(combo, 'addItems', ['auto', 'bsc', 'bs5', 'bsb', 'bss'])
        combo.setCurrentText("auto")
        row.addWidget(combo)
        row.addWidget(self._tip_label(tr('auto 会读取脚本头自动判别；只有遇到误判时再手动指定。')), 1)
        parent_layout.addLayout(row)
        return combo

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
        row.setContentsMargins(0, 0, 0, 0)
        edit = DragDropLineEdit(accept_mode)
        row.addWidget(edit, 1)

        if file_mode is not None:
            file_button = ModernButton(tr('文件'))
            file_button.setFixedWidth(50)
            file_button.clicked.connect(lambda: self._browse_file(edit, file_mode, filter_text))
            row.addWidget(file_button)

        if directory_mode:
            dir_button = ModernButton(tr('目录'))
            dir_button.setFixedWidth(50)
            dir_button.clicked.connect(lambda: self._browse_directory(edit))
            row.addWidget(dir_button)

        parent_layout.addLayout(row)
        return edit

    def _browse_file(self, target_edit: QLineEdit, mode: str, filter_text: str) -> None:
        current_text = target_edit.text().strip()
        start_dir = str(Path(current_text).parent) if current_text else str(Path.cwd())

        if mode == "open":
            path, _ = QFileDialog.getOpenFileName(self, tr('选择文件'), start_dir, filter_text)
        else:
            path, _ = QFileDialog.getSaveFileName(self, tr('选择输出文件'), current_text or start_dir, filter_text)

        if path:
            ui_call(target_edit, 'setText', str(Path(path)))

    def _browse_directory(self, target_edit: QLineEdit) -> None:
        current_text = target_edit.text().strip()
        start_dir = current_text or str(Path.cwd())
        path = QFileDialog.getExistingDirectory(self, tr('选择文件夹'), start_dir)
        if path:
            ui_call(target_edit, 'setText', str(Path(path)))

    def _register_output_edit(self, edit: QLineEdit) -> None:
        edit.textChanged.connect(lambda _text, target=edit: self._on_output_path_changed(target))

    def _on_output_path_changed(self, edit: QLineEdit) -> None:
        if self._updating_auto_paths or self._loading_settings:
            return
        if edit.text().strip():
            self._manual_output_edits.add(edit)
        else:
            self._manual_output_edits.discard(edit)
        if edit is self.batch_output_edit:
            self._sync_output_paths()

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
        return not (path.name.lower().endswith(".bsd") or path.name.lower().endswith(".bsd.txt"))

    def _sync_input_mode(self) -> None:
        if self._loading_settings:
            return
        self.mode_stack.setCurrentIndex(1 if self._input_is_batch() else 0)
        self._sync_output_paths()

    def _single_detected_type_for_ui(self, source: Path) -> str:
        hint = self.single_hint_combo.currentText()
        if hint != "auto":
            return hint
        try:
            return parse_assembly_file(source).source_type if source.is_file() else "bs5"
        except (OSError, UnicodeError, ValueError):
            return "bs5"

    def _sync_output_paths(self) -> None:
        if self._loading_settings:
            return
        input_text = self.input_edit.text().strip()
        if not input_text:
            return
        source = Path(input_text)
        if self._input_is_batch():
            default_output = source.parent / f"{source.name}_decompiled"
            self._set_auto_output_path(self.batch_output_edit, default_output)
            definitions_root = Path(self.batch_output_edit.text().strip()) if self.batch_output_edit.text().strip() else default_output
            self._set_auto_output_path(self.batch_defs_edit, definitions_root / "bsd2c_defs.json")
            return

        detected_type = self._single_detected_type_for_ui(source)
        output = derive_output_path_for_format(source, detected_type, self.format_combo.currentText())
        self._set_auto_output_path(self.single_output_edit, output)
        self._set_auto_output_path(self.single_defs_edit, derive_definitions_path(source))

    def _default_batch_output_dir_for_ui(self, input_dir: Path) -> Path:
        output_text = self.batch_output_edit.text().strip()
        return Path(output_text) if output_text else input_dir.parent / f"{input_dir.name}_decompiled"

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            local_path = url.toLocalFile()
            if not local_path:
                continue
            path = Path(local_path)
            if path.exists():
                ui_call(self.input_edit, 'setText', str(path))
                event.acceptProposedAction()
                return
        event.ignore()

    def run_single_decompile(self) -> None:
        input_text = self.single_input_edit.text().strip()
        if not input_text:
            QMessageBox.warning(self, tr('缺少输入'), tr('请选择或拖入一个 .bsd / .bsd.txt 文件。'))
            return
        self._start_worker(
            "single",
            input_text,
            self.single_output_edit.text(),
            self.single_ir_edit.text(),
            self.single_defs_edit.text(),
            self.single_hint_combo.currentText(),
            self.format_combo.currentText(),
        )

    def run_single_signature_scan(self) -> None:
        input_text = self.single_input_edit.text().strip()
        if not input_text:
            QMessageBox.warning(self, tr('缺少输入'), tr('请选择或拖入一个 .bsd / .bsd.txt 文件。'))
            return
        self._start_worker(
            "single",
            input_text,
            self.single_output_edit.text(),
            self.single_ir_edit.text(),
            self.single_defs_edit.text(),
            self.single_hint_combo.currentText(),
            self.format_combo.currentText(),
            action="scan",
        )

    def run_batch_decompile(self) -> None:
        input_text = self.batch_input_edit.text().strip()
        if not input_text:
            QMessageBox.warning(self, tr('缺少输入'), tr('请选择或拖入一个目录。'))
            return
        self._start_worker(
            "batch",
            input_text,
            self.batch_output_edit.text(),
            self.batch_ir_edit.text(),
            self.batch_defs_edit.text(),
            self.batch_hint_combo.currentText(),
            self.format_combo.currentText(),
        )

    def run_batch_signature_scan(self) -> None:
        input_text = self.batch_input_edit.text().strip()
        if not input_text:
            QMessageBox.warning(self, tr('缺少输入'), tr('请选择或拖入一个目录。'))
            return
        self._start_worker(
            "batch",
            input_text,
            self.batch_output_edit.text(),
            self.batch_ir_edit.text(),
            self.batch_defs_edit.text(),
            self.batch_hint_combo.currentText(),
            self.format_combo.currentText(),
            action="scan",
        )

    def _start_worker(
        self,
        mode: str,
        input_path: str,
        output_path: str,
        ir_path: str,
        definitions_path: str,
        hint: str,
        output_format: str,
        action: str = "decompile",
    ) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, tr('任务进行中'), tr('请等待当前任务完成。'))
            return

        ignore_builtin_config = self._current_ignore_builtin_config()
        replacement_rules = self._current_replacement_rules()
        self._save_last_run_ignore_config(ignore_builtin_config)
        self._save_settings()

        self.worker = DecompileWorker(
            mode,
            input_path,
            output_path,
            ir_path,
            definitions_path,
            hint,
            output_format,
            action,
            ignore_builtin_config,
            RpyEmitOptions(
                strip_double_zero_prefix=self.rpy_strip_double_zero_checkbox.isChecked(),
                strip_three_digit_prefix=self.rpy_strip_three_digit_prefix_checkbox.isChecked(),
                suppress_bgi_label_registration=self.rpy_suppress_bgi_label_registration_checkbox.isChecked(),
                lowercase_labels=self.rpy_lowercase_labels_checkbox.isChecked(),
            ),
            replacement_rules,
        )
        self.worker.log_signal.connect(self.append_log)
        self.worker.finished_signal.connect(self._on_worker_finished)
        self._set_controls_enabled(False)
        mode_text = tr('单文件') if mode == "single" else tr('批量目录')
        action_text = tr('签名扫描') if action == "scan" else tr('反编译')
        self.append_log(tr('开始执行：{0}{1}，Hint={2}，格式={3}，输入={4}', mode_text, action_text, hint, output_format, input_path))
        if definitions_path.strip():
            self.append_log(tr('签名 JSON：{0}', definitions_path.strip()))
        self.append_log(
            tr('忽略函数：BSC={0}；BS5-like={1}', sorted(ignore_builtin_config.bsc), sorted(ignore_builtin_config.bs5_like))
        )
        if replacement_rules:
            self.append_log(
                tr('文本替换规则：') + "；".join(self._format_replacement_rule_for_log(rule) for rule in replacement_rules)
            )
        else:
            self.append_log(tr('文本替换规则：无'))
        self.worker.start()

    def _on_worker_finished(self, success: bool, summary: str) -> None:
        self._set_controls_enabled(True)
        self.append_log(summary if success else tr('任务失败：{0}', summary))
        if success:
            QMessageBox.information(self, tr('完成'), summary)
        else:
            QMessageBox.critical(self, tr('失败'), summary)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.theme_combo.setEnabled(enabled)
        self.content_tabs.setEnabled(enabled)
        self.mode_stack.setEnabled(enabled)

    def append_log(self, text: str) -> None:
        if not text:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        self.log_view.insertPlainText(f"[{timestamp}] {text}\n")
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _ignore_names_from_widgets(self, checkboxes: dict[str, QCheckBox], extra_edit: QLineEdit) -> frozenset[str]:
        selected = [name for name, checkbox in checkboxes.items() if checkbox.isChecked()]
        selected.extend(split_ignore_names_text(extra_edit.text()))
        return normalize_ignored_builtin_names(selected)

    def _current_ignore_builtin_config(self) -> IgnoreBuiltinConfig:
        return IgnoreBuiltinConfig(
            bsc=self._ignore_names_from_widgets(self.bsc_ignore_checkboxes, self.bsc_ignore_extra_edit),
            bs5_like=self._ignore_names_from_widgets(self.bs5_like_ignore_checkboxes, self.bs5_like_ignore_extra_edit),
        )

    def _format_replacement_text_for_log(self, text: str) -> str:
        return text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")

    def _format_replacement_rule_for_log(self, rule: TextReplacementRule) -> str:
        return (
            f"{self._format_replacement_text_for_log(rule.find)} -> "
            f"{self._format_replacement_text_for_log(rule.replace)}"
        )

    def _load_replacement_settings(self) -> None:
        raw_rules = self.settings.value("replace/rules_json", None)
        if raw_rules is None:
            self._set_replacement_rows(self._default_replacement_rows())
            return
        try:
            rows = json.loads(str(raw_rules))
        except Exception:
            rows = self._default_replacement_rows()
        if not isinstance(rows, list):
            rows = self._default_replacement_rows()
        self._set_replacement_rows(rows)

    def _save_replacement_settings(self) -> None:
        self.settings.setValue(
            "replace/rules_json",
            json.dumps(self._replacement_rows_for_storage(), ensure_ascii=False),
        )

    def _load_ignore_group_settings(
        self,
        group_name: str,
        defaults: frozenset[str],
        checkboxes: dict[str, QCheckBox],
        extra_edit: QLineEdit,
    ) -> None:
        for name, checkbox in checkboxes.items():
            checkbox.setChecked(self.settings.value(f"ignore/{group_name}/{name}", name in defaults, type=bool))
        ui_call(extra_edit, 'setText', self.settings.value(f'ignore/{group_name}/extra', ''))

    def _save_ignore_group_settings(
        self,
        group_name: str,
        checkboxes: dict[str, QCheckBox],
        extra_edit: QLineEdit,
    ) -> None:
        for name, checkbox in checkboxes.items():
            self.settings.setValue(f"ignore/{group_name}/{name}", checkbox.isChecked())
        extra_names = [name for name in split_ignore_names_text(extra_edit.text()) if name not in KNOWN_IGNORED_BUILTINS]
        self.settings.setValue(f"ignore/{group_name}/extra", ", ".join(extra_names))

    def _save_last_run_ignore_config(self, ignore_builtin_config: IgnoreBuiltinConfig) -> None:
        self.settings.setValue("last_run/timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.settings.setValue("last_run/bsc", ", ".join(sorted(ignore_builtin_config.bsc)))
        self.settings.setValue("last_run/bs5_like", ", ".join(sorted(ignore_builtin_config.bs5_like)))
        self.settings.sync()
        self._refresh_ignore_summary()

    def _refresh_ignore_summary(self) -> None:
        timestamp = self.settings.value("last_run/timestamp", tr('未执行'))
        last_bsc = self.settings.value("last_run/bsc", "")
        last_bs5_like = self.settings.value("last_run/bs5_like", "")
        bsc_text = last_bsc or tr('(空)')
        bs5_like_text = last_bs5_like or tr('(空)')
        ui_call(self.last_ignore_summary_label, 'setText', tr('上次执行时间：{0}\n上次执行的 BSC 忽略函数：{1}\n上次执行的 BS5-like 忽略函数：{2}', timestamp, bsc_text, bs5_like_text))

    def _load_settings(self) -> None:
        self._loading_settings = True
        saved_mode = self.settings.value("ui/mode", "single")
        input_value = self.settings.value("input/path", "")
        if not input_value:
            legacy_key = "batch/input" if saved_mode == "batch" else "single/input"
            input_value = self.settings.value(legacy_key, "")
        ui_call(self.input_edit, 'setText', input_value)
        ui_call(self.single_output_edit, 'setText', self.settings.value('single/output', ''))
        single_ir_value = self.settings.value("single/ir", "")
        if self.settings.value("auto_paths/single_ir", False, type=bool):
            single_ir_value = ""
        ui_call(self.single_ir_edit, 'setText', single_ir_value)
        ui_call(self.single_defs_edit, 'setText', self.settings.value('single/defs', ''))
        self.single_hint_combo.setCurrentText(self.settings.value("single/hint", "auto"))
        ui_call(self.batch_output_edit, 'setText', self.settings.value('batch/output', ''))
        batch_ir_value = self.settings.value("batch/ir", "")
        if self.settings.value("auto_paths/batch_ir", False, type=bool):
            batch_ir_value = ""
        ui_call(self.batch_ir_edit, 'setText', batch_ir_value)
        ui_call(self.batch_defs_edit, 'setText', self.settings.value('batch/defs', ''))
        self.batch_hint_combo.setCurrentText(self.settings.value("batch/hint", "auto"))
        self.format_combo.setCurrentText(normalize_output_format(self.settings.value("format", "c")))
        self.rpy_strip_double_zero_checkbox.setChecked(self.settings.value("rpy/strip_double_zero_prefix", True, type=bool))
        self.rpy_strip_three_digit_prefix_checkbox.setChecked(
            self.settings.value("rpy/strip_three_digit_prefix", False, type=bool)
        )
        self.rpy_suppress_bgi_label_registration_checkbox.setChecked(
            self.settings.value("rpy/suppress_bgi_label_registration", True, type=bool)
        )
        self.rpy_lowercase_labels_checkbox.setChecked(
            self.settings.value("rpy/lowercase_labels", False, type=bool)
        )
        self._load_replacement_settings()
        self._load_ignore_group_settings(
            "bsc",
            DEFAULT_BSC_IGNORED_BUILTINS,
            self.bsc_ignore_checkboxes,
            self.bsc_ignore_extra_edit,
        )
        self._load_ignore_group_settings(
            "bs5_like",
            DEFAULT_BS5_LIKE_IGNORED_BUILTINS,
            self.bs5_like_ignore_checkboxes,
            self.bs5_like_ignore_extra_edit,
        )
        self._loading_settings = False
        output_settings = (
            ("single_output", self.single_output_edit),
            ("single_defs", self.single_defs_edit),
            ("batch_output", self.batch_output_edit),
            ("batch_defs", self.batch_defs_edit),
        )
        for key, edit in output_settings:
            if edit.text().strip() and not self.settings.value(f"auto_paths/{key}", False, type=bool):
                self._manual_output_edits.add(edit)
        self._sync_input_mode()
        self._refresh_ignore_summary()

    def _save_settings(self) -> None:
        self.settings.setValue("input/path", self.input_edit.text())
        self.settings.setValue("single/input", self.input_edit.text())
        self.settings.setValue("single/output", self.single_output_edit.text())
        self.settings.setValue("single/ir", self.single_ir_edit.text())
        self.settings.setValue("single/defs", self.single_defs_edit.text())
        self.settings.setValue("single/hint", self.single_hint_combo.currentText())
        self.settings.setValue("batch/input", self.input_edit.text())
        self.settings.setValue("batch/output", self.batch_output_edit.text())
        self.settings.setValue("batch/ir", self.batch_ir_edit.text())
        self.settings.setValue("batch/defs", self.batch_defs_edit.text())
        self.settings.setValue("batch/hint", self.batch_hint_combo.currentText())
        for key, edit in (
            ("single_output", self.single_output_edit),
            ("single_defs", self.single_defs_edit),
            ("batch_output", self.batch_output_edit),
            ("batch_defs", self.batch_defs_edit),
        ):
            self.settings.setValue(f"auto_paths/{key}", edit not in self._manual_output_edits)
        self.settings.setValue("auto_paths/single_ir", False)
        self.settings.setValue("auto_paths/batch_ir", False)
        self.settings.setValue("format", self.format_combo.currentText())
        self.settings.setValue("rpy/strip_double_zero_prefix", self.rpy_strip_double_zero_checkbox.isChecked())
        self.settings.setValue(
            "rpy/strip_three_digit_prefix",
            self.rpy_strip_three_digit_prefix_checkbox.isChecked(),
        )
        self.settings.setValue(
            "rpy/suppress_bgi_label_registration",
            self.rpy_suppress_bgi_label_registration_checkbox.isChecked(),
        )
        self.settings.setValue("rpy/lowercase_labels", self.rpy_lowercase_labels_checkbox.isChecked())
        self._save_replacement_settings()
        self._save_ignore_group_settings("bsc", self.bsc_ignore_checkboxes, self.bsc_ignore_extra_edit)
        self._save_ignore_group_settings("bs5_like", self.bs5_like_ignore_checkboxes, self.bs5_like_ignore_extra_edit)
        self.settings.setValue("ui/theme", self.theme_combo.currentText())
        self.settings.setValue("ui/mode", "batch" if self._input_is_batch() else "single")
        self.settings.sync()

    def closeEvent(self, event) -> None:
        self._save_settings()
        super().closeEvent(event)

    def detect_system_theme(self) -> None:
        theme = self.settings.value("ui/theme", "跟随系统")
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
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                ) as key:
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
        QTabBar::tab { background: #1e1e1e; color: #999; padding: 10px 20px; border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right:2px;}
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
        QLabel#AppTitle { color: #ff00ff; font-size: 18pt; font-weight: bold; text-shadow: 0 0 5px #ff00ff; }
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
