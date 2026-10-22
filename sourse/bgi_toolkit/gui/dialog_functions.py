"""Inspectable, editable text-parameter roles for story extraction."""
from __future__ import annotations
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import LocalizedText, tr

from copy import deepcopy
import os
import json
import re
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QPalette, QColor, QBrush, QIcon, QPixmap, QPainter
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QHeaderView, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QToolButton, QMenu, QWidget, QMessageBox)

from ..asdis.common.dialog_analysis import analyze_calls, canonical_function, describe_calls, parse_rules, split_role_spec, TextCall, is_standard, assign_pair_groups
from ..asdis.service import AsdisOptions
from ..asdis.common.text_scan import scan_file, scan_worker_count, parallel_scan, merge_scan_groups
from ..detector import iter_inputs, non_story_notice
from ..models import InputKind
from .comboboxes import apply_combo_theme

ROLE_LABELS = {"name": "姓名", "message": "正文", "select": "选项", "ignore": "忽略"}


def pairing_color(pair_id, dark=False):
    hue = ((220 + (pair_id - 1) * 137.508) % 360) / 360
    return QColor.fromHslF(hue, 0.62, 0.72 if dark else 0.30)


def pairing_icon(color):
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(color)
    painter.setBrush(color)
    painter.drawEllipse(3, 3, 14, 14)
    painter.end()
    return QIcon(pixmap)


class PairGroupButton(QToolButton):
    """A compact color picker beside the role; IDs remain an internal save format."""
    valueChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self.setFixedSize(34, 32)
        self.setIconSize(QSize(20, 20))
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setMenu(ui_widget(QMenu, self))
        ui_call(self, 'setAccessibleName', tr('选择姓名与正文的配对颜色'))

    def value(self):
        return self._value

    def setValue(self, value):
        value = int(value)
        if not 0 <= value <= 2147483647:
            raise ValueError(tr('无效的配对组'))
        if self._value != value:
            self._value = value
            self.valueChanged.emit(value)


def load_function_display_maps():
    resources = Path(__file__).resolve().parents[1] / "asdis" / "resources"
    maps, errors = {}, []
    for version, filename in (("v0", "bss_mapping_v0.json"), ("v1", "bss_mapping.json")):
        maps[version] = {}
        try:
            payload = json.loads((resources / filename).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(tr('需要 JSON 对象'))
            for key, value in payload.items():
                raw = str(key).rsplit("::", 1)[-1]
                match = re.fullmatch(r"(?:f_|@)([0-9a-fA-F]+)", raw)
                if match and isinstance(value, str):
                    maps[version][f"f_{int(match[1], 16):03x}"] = value
        except (OSError, ValueError) as error:
            errors.append(tr('名表显示失败 · {0}: {1}', filename, error))
    return maps, errors


def parameter_display_label(row):
    """Translate cached UI labels without changing cached samples or role IDs."""
    label = row.get('label')
    if label == '选择提示 · 参数 3':
        return tr('选择提示 · 参数 3')
    match = re.fullmatch(r'选项 (\d+) · 缓冲区', label or '')
    if match:
        return tr('选项 {0} · 缓冲区', int(match[1]))
    return label or tr('参数 {0}', row['position'])


def function_display_label(group, maps):
    name = group["function"]
    if not re.fullmatch(r"f_[0-9a-fA-F]{3,}", name):
        return tr('用户函数')
    versions = group.get("versions", ["v1"])
    labels = [(version, maps.get(version, {}).get(name, tr('名表未收录'))) for version in versions]
    if len(labels) == 1:
        return labels[0][1]
    parts = []
    for version, label in labels:
        if parts:
            parts.append(" / ")
        parts.extend((f"{version.upper()}: ", label))
    return LocalizedText(parts=tuple(parts))


def text_scan_key(source, options):
    return (os.path.normcase(os.path.abspath(str(source))),
        options.effective_source_encoding.casefold(),
        options.effective_source_fallback_encoding.casefold(),
        options.script_version, options.f07e_mode)


def text_scan_signature(source, options, files=None):
    if files is None:
        files = iter_inputs(source, {InputKind.BSD, InputKind.STORY_BINARY})
    snapshots = []
    for path in files:
        stat = path.stat()
        snapshots.append((os.path.normcase(str(path.resolve())), stat.st_size, stat.st_mtime_ns))
    return text_scan_key(source, options), tuple(snapshots)


def pairing_warnings(groups):
    warnings = []
    for group in groups:
        if not group.get("enabled", True):
            continue
        function = group["function"]
        rows = group["arguments"]
        names = [row for row in rows if row["role"] == "name"]
        messages = [row for row in rows if row["role"] == "message"]
        # v0 sets its pending speaker in one call and prints text in another.
        if group.get("versions") == ["v0"] and function in {"f_014", "f_010"}:
            continue
        name_groups = {}
        for row in names:
            if row["pair_id"]:
                name_groups.setdefault(row["pair_id"], []).append(row)
        body_groups = {row["pair_id"] for row in messages if row["pair_id"]}
        def add(text, blocking=False):
            warnings.append({"function": function, "text": text, "blocking": blocking})
        for pair_id, linked in sorted(name_groups.items()):
            if len(linked) > 1:
                positions = "、".join(str(row["position"]) for row in linked)
                add(tr('同色配对组对应多个姓名参数（{0}），请为不同姓名选择不同颜色。', positions), True)
            elif pair_id not in body_groups:
                add(tr('姓名（参数 {0}）没有同色正文，将单独导出姓名。', linked[0]['position']))
        for pair_id in sorted(body_groups - name_groups.keys()) if name_groups else ():
            positions = "、".join(str(row["position"]) for row in messages if row["pair_id"] == pair_id)
            add(tr('正文参数 {0} 没有同色姓名，将只导出正文。', positions))
    return warnings


def pairing_warning_text(warnings):
    parts = []
    for item in warnings:
        if parts:
            parts.append("\n")
        parts.extend((item['function'] + '：', item['text']))
    return LocalizedText(parts=tuple(parts))


def confirm_pairing_warnings(parent, warnings):
    box = QMessageBox(parent)
    ui_call(box, 'setWindowTitle', tr('检查姓名与正文配对'))
    box.setIcon(QMessageBox.Icon.Warning)
    ui_call(box, 'setText', pairing_warning_text(warnings[:4]))
    ui_call(box, 'setInformativeText', tr('同一个人说了多段话时，把这些正文都设成与姓名相同的颜色。导出时，每段话都会带上这个姓名。'))
    if len(warnings) > 4:
        ui_call(box, 'setDetailedText', pairing_warning_text(warnings))
    back = box.addButton(tr('返回调整'), QMessageBox.ButtonRole.RejectRole)
    proceed = None
    if not any(item["blocking"] for item in warnings):
        proceed = box.addButton(tr('按当前配置继续'), QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(back)
    box.setEscapeButton(back)
    box.exec()
    return proceed is not None and box.clickedButton() is proceed


class FunctionScanWorker(QThread):
    progress = pyqtSignal(object)
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source, options, rules, parent=None):
        super().__init__(parent)
        self.source, self.options, self.rules = Path(source), options, rules

    def run(self):
        try:
            skipped = []
            def skip_resource(path, kind):
                skipped.append(path)
                self.progress.emit(non_story_notice(path, kind))
            files = iter_inputs(self.source, {InputKind.BSD, InputKind.STORY_BINARY}, on_skip=skip_resource)
            if not files:
                raise ValueError(tr('输入中没有 BCS 剧本或 BSD 文件'))
            signature = text_scan_signature(self.source, self.options, files)
            merged, errors, scanned = {}, [], 0
            workers = scan_worker_count(files)
            if workers > 1:
                self.progress.emit(tr('并行分析 · {0} 个进程 · 共 {1} 个脚本', workers, len(files)))
                records = parallel_scan(files, self.options, self.rules, workers, self.isInterruptionRequested)
            else:
                records = (scan_file(index, path, self.options, self.rules, analyzer=analyze_calls)
                    for index, path in enumerate(files))
            pending, next_index, finished = {}, 0, 0
            try:
                for result in records:
                    if self.isInterruptionRequested():
                        return
                    if "diagnostic" in result:
                        if result["diagnostic"]:
                            self.progress.emit(result["diagnostic"])
                        continue
                    if result.get("retry_serial"):
                        self.progress.emit(tr('并行任务重试 · {0}', Path(result['path']).name))
                        result = scan_file(result["index"], result["path"], self.options, self.rules, analyzer=analyze_calls)
                    finished += 1
                    self.progress.emit(tr('已分析 {0}/{1} · {2}', finished, len(files), Path(result['path']).name))
                    if result["log"]:
                        self.progress.emit(result["log"])
                    pending[result["index"]] = result
                    # Preserve the original file and sample order despite out-of-order completion.
                    while next_index in pending:
                        current = pending.pop(next_index)
                        next_index += 1
                        if current["error"] is not None:
                            errors.append(f"{current['path']}: {current['error']}")
                            self.progress.emit(tr('分析失败 · {0}: {1}', current['path'], current['error']))
                        else:
                            merge_scan_groups(merged, current["groups"], current["path"])
                            scanned += 1
            finally:
                records.close()
            if not self.isInterruptionRequested():
                if text_scan_signature(self.source, self.options) != signature:
                    errors.append(tr('扫描期间输入文件发生变化，请重新扫描'))
                self.completed.emit({"groups": list(merged.values()), "scanned": scanned,
                    "errors": errors, "signature": signature, "all_functions_scanned": True, "selection_sources_scanned": True})
        except Exception as error:
            self.failed.emit(str(error))


class TextFunctionDialog(QDialog):
    scan_ready = pyqtSignal(object)

    def __init__(self, source, options, rules, parent=None, *, scan_result=None):
        super().__init__(parent)
        ui_call(self, 'setWindowTitle', tr('文本函数与参数角色'))
        self.resize(960, 650)
        self.setMinimumSize(760, 500)
        self.original_rules = parse_rules(rules)
        self.rows = []
        self.pair_controls = {}
        self._row_defaults = {}
        self._explicit_pairs = set()
        self._function_profiles = {}
        self._loading_pairs = False
        self.worker = None
        self.scan_result = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        title = ui_widget(QLabel, tr('文本函数与参数角色'))
        title.setObjectName("SectionTitle")
        layout.addWidget(title)
        hint = ui_widget(QLabel, tr('颜色表示正文配哪个姓名。同一函数里，同色的姓名和正文会一起导出。\n一个姓名能配多段正文；“无”表示单独导出。点颜色或“无”可以修改。'))
        hint.setWordWrap(True)
        hint.setObjectName("TipLabel")
        layout.addWidget(hint)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        ui_call(self.search, 'setPlaceholderText', tr('搜索函数编号、名表对应名、用户函数或原文…'))
        search_row.addWidget(self.search, 1)
        self.show_all = ui_widget(QCheckBox, tr('显示全部函数'))
        ui_call(self.show_all, 'setToolTip', tr('也显示没有可提取文字的函数。只切换列表，不会重新扫描。'))
        search_row.addWidget(self.show_all)
        self.enabled_only = ui_widget(QCheckBox, tr('仅显示已启用'))
        search_row.addWidget(self.enabled_only)
        layout.addLayout(search_row)
        self.tree = QTreeWidget()
        ui_call(self.tree, 'setHeaderLabels', [tr('函数 / 字符串参数'), tr('名表对应名'), tr('调用次数'), tr('文本角色与配对'), tr('原文样例')])
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(20)
        self.tree.setUniformRowHeights(False)
        self.tree.setColumnWidth(0, 164)
        self.tree.setColumnWidth(1, 155)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setColumnWidth(3, 198)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tree.setStyleSheet("QTreeWidget::item { min-height:40px; padding:0 4px; } QHeaderView::section { padding:8px; border:0; }")
        layout.addWidget(self.tree, 1)
        self.status = ui_widget(QLabel, tr('正在读取调用及参数…'))
        self.status.setObjectName("TipLabel")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.pairing_notice = ui_widget(QLabel)
        self.pairing_notice.setWordWrap(True)
        self.pairing_notice.setObjectName("PairingNotice")
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        color = "#f1c36d" if dark else "#915d10"
        self.pairing_notice.setStyleSheet(f"QLabel {{ color:{color}; border-left:3px solid {color}; padding-left:8px; }}")
        self.pairing_notice.hide()
        layout.addWidget(self.pairing_notice)
        footer = QHBoxLayout()
        self.reset = ui_widget(QPushButton, tr('恢复自动判断'))
        self.reset.setEnabled(False)
        self.reset.clicked.connect(self.reset_roles)
        footer.addWidget(self.reset)
        self.help_button = ui_widget(QPushButton, tr('使用说明'))
        self.help_button.clicked.connect(self.show_help)
        footer.addWidget(self.help_button)
        footer.addStretch()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        ui_call(self.buttons.button(QDialogButtonBox.StandardButton.Save), 'setText', tr('应用到提取与导回'))
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("PrimaryButton")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
        ui_call(self.buttons.button(QDialogButtonBox.StandardButton.Cancel), 'setText', tr('取消'))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        footer.addWidget(self.buttons)
        layout.addLayout(footer)
        self.search.textChanged.connect(self.filter_rows)
        self.enabled_only.toggled.connect(self.filter_rows)
        self.show_all.toggled.connect(self.filter_rows)
        self.tree.itemChanged.connect(self.filter_rows)
        self.tree.itemChanged.connect(self.update_pairing_notice)
        if scan_result is not None:
            self.populate(scan_result)
        else:
            self.worker = FunctionScanWorker(source, options, rules, self)
            self.worker.progress.connect(self.show_progress)
            self.worker.completed.connect(self.populate)
            self.worker.failed.connect(self.scan_failed)
            self.worker.start()

    def show_progress(self, text):
        ui_call(self.status, 'setText', text.transformed(lambda value: value.splitlines()[-1]) if isinstance(text, LocalizedText) else text.splitlines()[-1])
        parent = self.parent()
        if parent is not None and hasattr(parent, "log_view"):
            parent.log_view.append(text)

    def scan_failed(self, message):
        ui_call(self.status, 'setText', tr('分析失败：{0}', message))
        self.show_progress(tr('分析失败：{0}', message))

    def populate(self, result):
        self.scan_result = deepcopy(result)
        result = deepcopy(result)
        self._loading_pairs = True
        ui_call(self.rows, 'clear')
        ui_call(self.pair_controls, 'clear')
        ui_call(self._row_defaults, 'clear')
        ui_call(self._explicit_pairs, 'clear')
        ui_call(self._function_profiles, 'clear')
        ui_call(self.tree, 'clear')
        # Cached scans retain samples; enable states and roles follow the current settings.
        for group in result["groups"]:
            key = canonical_function(group["function"])
            group["standard"] = group.get("standard", False) or any(is_standard(TextCall(key, 0, [], 0, v0=version == "v0")) for version in group.get("versions", ["v1"]))
            group["enabled"] = group["standard"] or key in self.original_rules
            for row in group["arguments"]:
                row["role"] = self.original_rules.get(key, {}).get(row["position"], "auto")
            if group["arguments"] and all(split_role_spec(row["role"])[0] == "ignore" for row in group["arguments"]):
                group["enabled"] = False
        maps, mapping_errors = load_function_display_maps()
        parent = self.parent()
        if mapping_errors and parent is not None and hasattr(parent, "log_view"):
            parent.log_view.append("\n".join(mapping_errors))
        self.tree.blockSignals(True)
        for group in sorted(result["groups"], key=lambda item: (not item["enabled"], item["function"])):
            self._function_profiles[group["function"]] = group
            top = ui_widget(QTreeWidgetItem, self.tree, [group['function'], function_display_label(group, maps), str(group['calls'])])
            if group["arguments"]:
                top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                top.setCheckState(0, Qt.CheckState.Checked if group["enabled"] else Qt.CheckState.Unchecked)
            else:
                top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                ui_call(top, 'setText', 3, tr('无可提取文本'))
                ui_call(top, 'setToolTip', 3, tr('当前调用没有可静态定位的非 ASCII 字符串；此项用于查看函数编号、名表名和调用次数。'))
            ui_call(top, 'setToolTip', 0, group.get('source', ''))
            ui_call(top, 'setToolTip', 1, source_text(top, 'setText', 1, fallback=top.text(1)))
            for row in sorted(group["arguments"], key=lambda row: row["position"]):
                samples = "  /  ".join(text.replace("\n", " ⏎ ") for text in row["samples"])
                child = ui_widget(QTreeWidgetItem, top, [parameter_display_label(row), '', '', '', samples])
                ui_call(child, 'setToolTip', 4, '\n\n'.join(row['samples']))
                combo = QComboBox(self.tree)
                inferred = tr(ROLE_LABELS.get(row["auto_role"], "正文"))
                ui_call(combo, 'addItem', tr('自动 · {0}', inferred), 'auto')
                for role, label in ROLE_LABELS.items():
                    ui_call(combo, 'addItem', tr(label), role)
                role, pair_id = split_role_spec(row["role"])
                combo.setCurrentIndex(max(0, combo.findData(role)))
                combo.setMinimumHeight(32)
                if row["uncertain"]:
                    ui_call(combo, 'setToolTip', tr('此参数角色尚未确认，请结合原文样例调整。自动模式逐次调用判断。'))
                dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
                apply_combo_theme(combo, self.palette(), dark)
                key = (group["function"], row["position"])
                self._row_defaults[key] = row["auto_role"]
                if pair_id is not None:
                    self._explicit_pairs.add(key)
                number = PairGroupButton(self.tree)
                number.setValue(pair_id or 0)
                number.setEnabled((row["auto_role"] if role == "auto" else role) in {"name", "message"})
                self.pair_controls[key] = number
                cell = QWidget(self.tree)
                cell.setAutoFillBackground(False)
                cell_layout = QHBoxLayout(cell)
                cell_layout.setContentsMargins(4, 4, 4, 4)
                cell_layout.setSpacing(6)
                cell_layout.addWidget(combo, 1)
                cell_layout.addWidget(number)
                self.tree.setItemWidget(child, 3, cell)
                number.menu().aboutToShow.connect(lambda k=key: self.populate_pair_menu(k))
                self.rows.append((group["function"], row["position"], top, child, combo))
                combo.currentIndexChanged.connect(lambda _, k=key: self.role_changed(k))
                number.valueChanged.connect(lambda _, k=key: self.pair_changed(k))
            top.setExpanded(group["enabled"])
        self.tree.blockSignals(False)
        count = sum(bool(group["arguments"]) for group in result["groups"])
        ui_call(self.status, 'setText', tr('已分析 {0} 个脚本 · {1} 个含文本函数 / 共 {2} 个函数 · {3} 个失败。', result['scanned'], count, len(result['groups']), len(result['errors'])))
        if not result.get("all_functions_scanned"):
            ui_call(self.show_all, 'setToolTip', tr('当前为旧版缓存，仅包含含文本函数；手动重新扫描后可查看完整函数列表。'))
        self.reset.setEnabled(bool(self.rows))
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(result["scanned"] > 0)
        ui_call(self.status, 'setToolTip', '' if result.get('selection_sources_scanned') else tr('当前使用旧版扫描缓存；需要补全 Select 选择提示和缓冲区选项样例时，请手动重新扫描。'))
        self._loading_pairs = False
        self.filter_rows()
        self.update_pairing_notice()
        self.scan_ready.emit(self.scan_result)

    def filter_rows(self, *_):
        if self.scan_result is not None and self.show_all.isChecked() and not self.scan_result.get("all_functions_scanned"):
            ui_call(self.status, 'setText', tr('当前缓存仅包含含文本函数；手动点击重新扫描后，可显示完整函数列表。'))
        elif self.scan_result is not None:
            groups = self.scan_result["groups"]
            text_count = sum(bool(group["arguments"]) for group in groups)
            ui_call(self.status, 'setText', tr('已分析 {0} 个脚本 · {1} 个含文本函数 / 共 {2} 个函数 · {3} 个失败。', self.scan_result['scanned'], text_count, len(groups), len(self.scan_result['errors'])))
        needle = self.search.text().strip().casefold()
        for index in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(index)
            matched = (needle in top.text(0).casefold() or needle in top.text(1).casefold()
                or any(needle in top.child(i).text(4).casefold() for i in range(top.childCount())))
            hidden = (not matched or (not self.show_all.isChecked() and top.childCount() == 0)
                or (self.enabled_only.isChecked() and top.checkState(0) != Qt.CheckState.Checked))
            top.setHidden(hidden)
            if needle and not hidden:
                top.setExpanded(True)

    def effective_role(self, function, position, combo):
        role = combo.currentData()
        return self._row_defaults.get((function, position), "message") if role == "auto" else role

    def ensure_pair_groups(self):
        self._loading_pairs = True
        try:
            for function in self._function_profiles:
                rows = [row for row in self.rows if row[0] == function]
                roles = {row[1]: self.effective_role(function, row[1], row[4]) for row in rows}
                existing = {row[1]: self.pair_controls[(function,row[1])].value() for row in rows
                    if (function,row[1]) in self._explicit_pairs or (roles[row[1]] == "name" and self.pair_controls[(function,row[1])].value())}
                groups = assign_pair_groups(roles, existing)
                for row in rows:
                    button = self.pair_controls[(function,row[1])]
                    button.setEnabled(roles[row[1]] in {"name", "message"})
                    button.setValue(groups.get(row[1], 0))
        finally:
            self._loading_pairs = False

    def pair_changed(self, key):
        if self._loading_pairs:
            return
        self._explicit_pairs.add(key)
        self.update_pairing_notice()

    def role_changed(self, key):
        if self._loading_pairs:
            return
        self._loading_pairs = True
        self._explicit_pairs.discard(key)
        self.pair_controls[key].setValue(0)
        self._loading_pairs = False
        self.update_pairing_notice()

    def pairing_warnings(self):
        groups = {}
        for function, position, top, _, combo in self.rows:
            profile = self._function_profiles[function]
            group = groups.setdefault(function, {"function": function,
                "versions": profile.get("versions", []),
                "enabled": top.checkState(0) == Qt.CheckState.Checked, "arguments": []})
            group["arguments"].append({"position": position,
                "role": self.effective_role(function, position, combo),
                "pair_id": self.pair_controls[(function, position)].value()})
        return pairing_warnings(groups.values())

    def choose_pair_group(self, key, value):
        self._loading_pairs = True
        self._explicit_pairs.add(key)
        self.pair_controls[key].setValue(value)
        self._loading_pairs = False
        self.update_pairing_notice()

    def populate_pair_menu(self, key):
        button = self.pair_controls[key]
        menu = button.menu()
        ui_call(menu, 'clear')
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        panel, text, hover = ("#242830", "#f0f1f4", "#353d4b") if dark else ("#ffffff", "#242934", "#edf1fd")
        menu.setStyleSheet(f"QMenu {{ background:{panel}; color:{text}; border:1px solid {hover}; padding:5px; }} QMenu::item {{ padding:8px 24px 8px 10px; border-radius:4px; }} QMenu::item:selected {{ background:{hover}; }}")
        none = ui_call(menu, 'addAction', tr('无'))
        none.setCheckable(True)
        none.setChecked(button.value() == 0)
        ui_call(none, 'setToolTip', tr('单独导出，不配对姓名和正文。'))
        none.triggered.connect(lambda: self.choose_pair_group(key, 0))
        menu.addSeparator()
        function = key[0]
        rows = [row for row in self.rows if row[0] == function]
        groups = sorted({self.pair_controls[(function, row[1])].value() for row in rows} - {0})
        for pair_id in groups:
            names = [row for row in rows if self.pair_controls[(function, row[1])].value() == pair_id
                and self.effective_role(function, row[1], row[4]) == "name"]
            label = tr('与姓名配对 · ') + names[0][3].text(4)[:24] if names else tr('独立正文组 · 无姓名')
            action = ui_call(menu, 'addAction', pairing_icon(pairing_color(pair_id, dark)), label)
            action.setCheckable(True)
            action.setChecked(button.value() == pair_id)
            action.triggered.connect(lambda checked=False, value=pair_id: self.choose_pair_group(key, value))
        candidate = 1
        while candidate in groups:
            candidate += 1
        menu.addSeparator()
        create = ui_call(menu, 'addAction', pairing_icon(pairing_color(candidate, dark)), tr('新建配对组'))
        create.triggered.connect(lambda: self.choose_pair_group(key, candidate))

    def paint_pairing_groups(self):
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        base = QColor("#292e37" if dark else "#f7f8fa")
        was_blocked = self.tree.blockSignals(True)
        try:
            for function, position, _, child, combo in self.rows:
                button = self.pair_controls[(function, position)]
                pair_id = button.value() if self.effective_role(function, position, combo) in {"name", "message"} else 0
                color = pairing_color(pair_id, dark) if pair_id else QColor("#8b94a5")
                tint = QColor(*(round(a * 0.88 + b * 0.12) for a, b in zip(base.getRgb()[:3], color.getRgb()[:3]))) if pair_id else base
                for column in range(self.tree.columnCount()):
                    child.setBackground(column, QBrush(tint) if pair_id else QBrush())
                child.setForeground(0, QBrush(color) if pair_id else QBrush())
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly if pair_id else Qt.ToolButtonStyle.ToolButtonTextOnly)
                ui_call(button, 'setText', '' if pair_id else tr('无'))
                button.setIcon(pairing_icon(color) if pair_id else QIcon())
                button.setStyleSheet(f"QToolButton {{ background:{tint.name()}; border:0; border-radius:6px; padding:0; }} QToolButton:hover {{ border:1px solid {color.name()}; }} QToolButton::menu-indicator {{ image:none; width:0; }}")
                hint = tr('同色的姓名和正文会一起导出。点这里换颜色，也可以选“无”来单独导出。') if pair_id else tr('这项会单独导出。点这里可以选择要配对的姓名或颜色。')
                if not button.isEnabled():
                    hint = tr('选项和忽略参数不参与姓名与正文配对。')
                ui_call(button, 'setToolTip', hint)
        finally:
            self.tree.blockSignals(was_blocked)

    def show_help(self):
        QMessageBox.information(self, tr('怎么选择和配对文字'),
            tr('1. 先看原文，再选类型\n“姓名”是说话的人，“正文”是他说的话，“选项”是菜单文字。“忽略”表示不提取，“自动”让工具判断。\n\n2. 颜色表示“哪段话配哪个姓名”\n同一函数里，蓝色正文配蓝色姓名，红色正文配红色姓名。颜色没有固定的人物含义，只用来分组。有姓名时才自动配色；没有姓名的正文默认选“无”，单独导出。\n\n3. 修改配对，点下拉框旁的颜色圆点或“无”\n给正文选择“与姓名配对”，就能换成对应的人名。选“无”让这项单独导出。不同姓名要用不同颜色，可以点“新建配对组”。选项和忽略的文字不用配姓名。\n\n4. 同一个人说了好几段话\n例如：一个姓名和两段正文都选蓝色，就会导出两条记录，每条都有这个姓名和一段正文。翻译时，同一个姓名要改成一样的译名，导回时才不会冲突。\n\n5. 配不上的地方会提醒\n姓名没有正文时，可以返回修改，也可以只导出姓名。两个姓名不能用同一种颜色，需要先给其中一个换色。\n\n6. 选项和提示文字默认提取\nSelect 等选择函数里的选项会默认提取，菜单上的提示文字（outline）会作为正文单独提取。不需要的文字选“忽略”；整个函数都不需要，就取消左边的勾选。只有半角英文、数字和符号的原文会跳过，译文不受这个限制。\n\n7. 不用每次重新扫描\n扫描结果会保存，重新打开直接使用。要更新时，手动点“重新扫描”。“显示全部函数”会列出没有可提取文字的函数；搜索和“仅显示已启用”可以一起用，都不会重新扫描。'))

    def update_pairing_notice(self, *_):
        if self._loading_pairs:
            return
        self.ensure_pair_groups()
        self.paint_pairing_groups()
        warnings = self.pairing_warnings()
        self.pairing_notice.setVisible(bool(warnings))
        ui_call(self.pairing_notice, 'setText', pairing_warning_text(warnings[:2]))
        ui_call(self.pairing_notice, 'setToolTip', pairing_warning_text(warnings))

    def reset_roles(self):
        self._loading_pairs = True
        ui_call(self._explicit_pairs, 'clear')
        for function, position, _, _, combo in self.rows:
            combo.setCurrentIndex(0)
            spin = self.pair_controls[(function, position)]
            spin.setValue(0)
            spin.setEnabled(self._row_defaults[(function, position)] in {"name", "message"})
        self._loading_pairs = False
        self.update_pairing_notice()

    def accept(self):
        warnings = self.pairing_warnings()
        if warnings and not confirm_pairing_warnings(self, warnings):
            return
        super().accept()

    def rule_text(self):
        result = {name: dict(roles) for name, roles in self.original_rules.items()}
        grouped = {}
        for name, position, top, _, combo in self.rows:
            spec = combo.currentData()
            pair_id = self.pair_controls[(name, position)].value()
            if self.effective_role(name, position, combo) in {"name", "message"} and (spec != "auto" or (name,position) in self._explicit_pairs):
                spec += f"#{pair_id}"
            grouped.setdefault(name, (top.checkState(0) == Qt.CheckState.Checked, {}))[1][position] = spec
        for name, (enabled, roles) in grouped.items():
            key = canonical_function(name)
            standard = self._function_profiles[name].get("standard", False)
            if enabled:
                result[key] = {pos: role for pos, role in roles.items() if role != "auto"}
            elif standard:
                result[key] = {pos: "ignore" for pos in roles}
            else:
                result.pop(key, None)
        return "; ".join(name + (": " + " ".join(f"{pos}={role}" for pos, role in sorted(roles.items())) if roles else "")
            for name, roles in sorted(result.items()))

    def done(self, result):
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()
            ui_call(self.status, 'setText', tr('正在停止分析，请稍候…'))
            self.setEnabled(False)
            self.worker.finished.connect(lambda: super(TextFunctionDialog, self).done(result))
            return
        super().done(result)
