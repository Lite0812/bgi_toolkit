"""Independent story and system script workspaces using the existing processors."""
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr
from copy import deepcopy

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (QVBoxLayout, QTabWidget, QWidget, QPushButton, QDialog,
    QHBoxLayout, QLabel, QMessageBox, QComboBox)
from pathlib import Path
from ..bp_engine import PROFILES, detect_engine

from ..asdis.service import AsdisOptions
from ..detector import non_story_notice
from .dialog_functions import TextFunctionDialog, text_scan_key, text_scan_signature
from .layouts import FormGrid
from .text_scan_cache import load_text_scan_cache, save_text_scan_cache, invalidate_text_scan_cache

from ..legacy_gui import asdis_gui


class ScriptToolWindow(asdis_gui.MainWindow):
    def __init__(self, kind):
        self.script_kind = kind
        self._text_scan_results = {}
        self._invalid_text_scan_keys = set()
        self._text_scan_controls = []
        super().__init__()
        # Keep the original controls available to settings and worker callbacks.
        # Only the selected script family appears in this workspace.
        self._inactive_pages = QWidget(self)
        self._inactive_pages.hide()
        if kind == 'bcs':
            page = self.tabs.widget(2)
            self.tabs.removeTab(2)
            page.setParent(self._inactive_pages)
        else:
            old_tabs = self.tabs
            bp_page = old_tabs.widget(2)
            bp_tabs = bp_page.findChild(QTabWidget)
            # The original tab widget belongs directly to a splitter.
            splitter = old_tabs.parentWidget()
            splitter.replaceWidget(splitter.indexOf(old_tabs), bp_tabs)
            old_tabs.setParent(self._inactive_pages)
            self.tabs = bp_tabs
        self.task_tabs = self.tabs
        if kind == 'bp':
            row = QHBoxLayout()
            row.addWidget(ui_widget(QLabel, tr('BP 引擎版本')))
            self.bp_engine_combo = QComboBox()
            self.bp_engine_combo.setEditable(True)
            ui_call(self.bp_engine_combo, 'addItem', tr('自动识别'), 'auto')
            for name in PROFILES:
                ui_call(self.bp_engine_combo, 'addItem', tr('当前兼容配置（版本未知）') if name == 'current' else name, name)
            row.addWidget(self.bp_engine_combo, 1)
            detect = ui_widget(QPushButton, tr('识别'))
            detect.clicked.connect(self._show_bp_engine)
            row.addWidget(detect)
            settings_page = QWidget()
            settings_page.setObjectName('ToolPageSurface')
            layout = QVBoxLayout(settings_page)
            layout.setContentsMargins(16,16,16,16)
            note = ui_widget(QLabel, tr('自动识别使用当前提取或构建页的输入，优先读取同一游戏的 EXE 版本。通常保持自动即可；结果会写入运行日志。'))
            note.setWordWrap(True); note.setObjectName('TipLabel'); layout.addWidget(note)
            layout.addLayout(row); layout.addStretch()
            self.bp_engine_settings_page = settings_page
            ui_call(self.tabs, 'addTab', settings_page, tr('引擎设置'))
            self._bp_last_mode_tab = 0
            self.tabs.currentChanged.connect(self._remember_bp_mode)
            self.bp_settings = QSettings(str(Path(self._settings_ini_path()).with_name('bp_engine.ini')), QSettings.Format.IniFormat)
            saved = self.bp_settings.value('engine','auto',str)
            self.bp_engine_combo.setCurrentIndex(max(0,self.bp_engine_combo.findData(saved)))
            if self.bp_engine_combo.findData(saved)<0:self.bp_engine_combo.setEditText(saved)
            self.bp_engine_combo.currentTextChanged.connect(lambda _: self.bp_settings.setValue('engine',self._selected_bp_engine()))
        if kind == 'bcs':
            controls = (
                (self.ext_json_userfunc_edit, self.ext_json_input_edit, self.ext_json_enc_combo, self.ext_json_ver_combo),
                (self.ext_txt_userfunc_edit, self.ext_txt_input_edit, self.ext_txt_enc_combo, self.ext_txt_ver_combo),
                (self.asm_json_userfunc_edit, self.asm_json_src_edit, self.asm_json_src_enc_combo, self.asm_json_ver_combo),
                (self.asm_txt_userfunc_edit, self.asm_txt_src_edit, self.asm_txt_src_enc_combo, self.asm_txt_ver_combo),
            )
            for edit, source, encoding, version in controls:
                ui_call(edit, 'setPlaceholderText', tr('如 f_321; _Talk（可选）'))
                ui_call(edit, 'setToolTip', tr('支持系统函数编号及用户函数；规则示例：f_321:1=name 2=message 3=ignore'))
                button = ui_widget(QPushButton, tr('参数角色…'), edit.parentWidget())
                button.setMinimumWidth(100)
                row = self._find_row(edit.parentWidget().layout(), edit)
                if row is not None:
                    row.addWidget(button)
                button.clicked.connect(lambda checked=False, e=edit, s=source, c=encoding, v=version:
                    self.edit_text_functions(e, s, c, v))

            for edit, source, encoding, version, extract_button in (
                    (self.ext_json_userfunc_edit, self.ext_json_input_edit, self.ext_json_enc_combo, self.ext_json_ver_combo, self.btn_json_extract),
                    (self.ext_txt_userfunc_edit, self.ext_txt_input_edit, self.ext_txt_enc_combo, self.ext_txt_ver_combo, self.btn_txt_extract)):
                page_layout = extract_button.parentWidget().layout()
                position = next(index for index in range(page_layout.count())
                    if page_layout.itemAt(index).widget() is extract_button)
                page_layout.takeAt(position)
                row = QHBoxLayout()
                row.setContentsMargins(0, 6, 0, 0)
                row.setSpacing(10)
                scan = ui_widget(QPushButton, tr('扫描文本函数'), extract_button.parentWidget())
                scan.setObjectName('TextFunctionScanButton')
                ui_call(scan, 'setToolTip', tr('扫描当前输入，查看系统函数、用户函数及字符串参数样例'))
                status = ui_widget(QLabel, tr('未扫描'), extract_button.parentWidget())
                status.setObjectName('TipLabel')
                rules_row = self._find_row(edit.parentWidget().layout(), edit)
                rules_row.insertWidget(rules_row.count() - 1, scan)
                row.addWidget(status)
                row.addStretch()
                row.addWidget(extract_button)
                page_layout.insertLayout(position, row)
                scan.clicked.connect(lambda checked=False, e=edit, s=source, c=encoding, v=version:
                    self.edit_text_functions(e, s, c, v, force_scan=True))
                self._text_scan_controls.append((source, encoding, version, scan, status))
                source.textChanged.connect(self._update_text_scan_status)
                encoding.currentTextChanged.connect(self._update_text_scan_status)
                version.currentTextChanged.connect(self._update_text_scan_status)

    @staticmethod
    def _find_row(layout, edit):
        if layout is None:
            return None
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.widget() is edit:
                return layout
            if item.layout():
                found = ScriptToolWindow._find_row(item.layout(), edit)
                if found is not None:
                    return found
        return None

    def _remember_bp_mode(self, index):
        if index in (0,1):
            self._bp_last_mode_tab = index

    def _selected_bp_engine(self):
        text = self.bp_engine_combo.currentText().strip()
        if text == self.bp_engine_combo.itemText(self.bp_engine_combo.currentIndex()):
            return self.bp_engine_combo.currentData() or text or 'auto'
        return text or 'auto'

    def _show_bp_engine(self):
        try:
            text = self._input_edit().text().strip()
            if not text or not Path(text).exists():
                raise ValueError(tr('请先选择存在的 BP / BPD 文件或目录'))
            self.log_view.append(detect_engine(text, self._selected_bp_engine()).describe())
        except (OSError, ValueError) as error:
            self.log_view.append(tr('BP 引擎识别失败：{0}', error))

    def _reject_resource_input(self, text):
        path = Path(text)
        if not path.is_file():
            return False
        notice = non_story_notice(path)
        if notice is None:
            return False
        self.log_view.append(notice)
        QMessageBox.information(self, tr('检测到非剧本文件'), notice)
        return True

    def start_worker(self, *args, **kwargs):
        if self.script_kind == 'bcs' and args and args[0] in ('disassemble', 'json_extract', 'txt_extract'):
            if self._reject_resource_input(args[1]):
                return
        if self.script_kind == 'bp':
            kwargs['bp_engine'] = self._selected_bp_engine()
        return super().start_worker(*args, **kwargs)

    def _text_scan_options(self, encoding, version):
        return AsdisOptions(encoding=encoding.currentText().strip(),
            script_version=self._normalize_version_choice(version.currentText()))

    def _load_text_scan(self, key):
        result = self._text_scan_results.get(key)
        if result is None and key not in self._invalid_text_scan_keys:
            result = load_text_scan_cache(key)
            if result is not None:
                self._text_scan_results[key] = result
        return result

    def _remember_text_scan(self, path, options, result):
        key = text_scan_key(path, options)
        self._invalid_text_scan_keys.discard(key)
        self._text_scan_results[key] = deepcopy(result)
        if len(self._text_scan_results) > 4:
            self._text_scan_results.pop(next(iter(self._text_scan_results)))
        try:
            save_text_scan_cache(key, result)
        except OSError as error:
            self.log_view.append(tr('扫描缓存保存失败：{0}', error))
        self._update_text_scan_status()

    def _cached_text_scan(self, source, options):
        key = text_scan_key(source, options)
        result = self._load_text_scan(key)
        if result is None:
            return None
        try:
            current = text_scan_signature(source, options)
        except OSError:
            current = None
        if result.get('signature') != current:
            self._text_scan_results.pop(key, None)
            self._invalid_text_scan_keys.add(key)
            try:
                invalidate_text_scan_cache(key)
            except OSError:
                pass
            self._update_text_scan_status()
            return None
        return result

    def _update_text_scan_status(self, *_):
        for source, encoding, version, button, label in self._text_scan_controls:
            options = self._text_scan_options(encoding, version)
            result = self._load_text_scan(text_scan_key(source.text().strip(), options))
            if result is None:
                ui_call(label, 'setText', tr('未扫描'))
                ui_call(button, 'setText', tr('扫描文本函数'))
            elif result.get('errors'):
                ui_call(label, 'setText', tr('已扫描 {0} 个脚本 · {1} 个失败', result['scanned'], len(result['errors'])))
                ui_call(button, 'setText', tr('重新扫描'))
            else:
                ui_call(label, 'setText', tr('已扫描 {0} 个脚本', result['scanned']))
                ui_call(button, 'setText', tr('重新扫描'))

    def edit_text_functions(self, edit, source, encoding, version, *, force_scan=False):
        path = source.text().strip()
        if path and self._reject_resource_input(path):
            return False
        if not path:
            self.log_view.append(tr('请先选择输入脚本或目录，再扫描文本函数。'))
            source.setFocus()
            return False
        if self.worker is not None and self.worker.isRunning():
            self.log_view.append(tr('当前任务仍在运行，请结束后再扫描文本函数。'))
            return False
        try:
            options = self._text_scan_options(encoding, version)
            cached = None if force_scan else self._cached_text_scan(path, options)
            if not force_scan and cached is None:
                QMessageBox.information(self, tr('尚无有效扫描结果'), tr('请先点击“扫描文本函数”。已有结果会保存在缓存中，进入参数角色页面不会自动重新扫描。'))
                return False
            dialog = TextFunctionDialog(path, options, edit.text(), self, scan_result=cached)
            dialog.scan_ready.connect(lambda result: self._remember_text_scan(path, options, result))
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._apply_user_function_names(dialog.rule_text(), persist=True)
                result = dialog.scan_result
                if result is not None:
                    self._remember_text_scan(path, options, result)
                self.log_view.append(tr('已更新文本函数与参数角色，JSON / TXT 提取与导回共用。'))
                current = self._cached_text_scan(path, options) if result is not None else None
                return bool(current and current.get('scanned'))
        except Exception as error:
            self.log_view.append(tr('参数角色分析失败：{0}', error))
        return False

    def _prompt_unscanned_text_extract(self, incomplete=False):
        box = QMessageBox(self)
        ui_call(box, 'setWindowTitle', tr('尚未完成文本函数扫描'))
        box.setIcon(QMessageBox.Icon.Warning)
        ui_call(box, 'setText', tr('当前输入的文本函数扫描不完整。') if incomplete else tr('尚未扫描当前输入的文本函数。'))
        box.setInformativeText(tr('建议先扫描并检查姓名、正文和选项参数；直接提取将使用当前规则，可能遗漏尚未选择的函数文本。'))
        scan = box.addButton(tr('先扫描'), QMessageBox.ButtonRole.AcceptRole)
        direct = box.addButton(tr('直接提取'), QMessageBox.ButtonRole.ActionRole)
        box.addButton(tr('取消'), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(scan)
        box.exec()
        if box.clickedButton() is scan:
            return 'scan'
        if box.clickedButton() is direct:
            return 'extract'
        return 'cancel'

    def _run_dialog_extract(self, mode, input_edit, output_edit, enc_combo, ver_combo, ext, user_func_edit=None):
        path = input_edit.text().strip()
        if self.script_kind == 'bcs' and mode in ('json_extract', 'txt_extract') and path:
            if self._reject_resource_input(path):
                return
            options = self._text_scan_options(enc_combo, ver_combo)
            result = self._cached_text_scan(path, options)
            if not result or not result.get('scanned'):
                action = self._prompt_unscanned_text_extract(incomplete=bool(result and result.get('errors')))
                if action == 'scan':
                    if not self.edit_text_functions(user_func_edit, input_edit, enc_combo, ver_combo, force_scan=True):
                        return
                elif action != 'extract':
                    return
            elif result.get('errors'):
                self.log_view.append(tr('使用 {0} 个成功脚本的扫描结果；{1} 个文件分析失败，详情见扫描日志。', result['scanned'], len(result['errors'])))
        super()._run_dialog_extract(mode, input_edit, output_edit, enc_combo, ver_combo, ext, user_func_edit)

    def _input_edit(self):
        if self.script_kind == 'bp':
            index = self.tabs.currentIndex() if self.tabs.currentIndex() in (0,1) else self._bp_last_mode_tab
            if index == 0:
                return self._get_bp_extract_controls(self.get_bp_extract_mode())[0]
            return self._get_bp_build_controls(self.get_bp_build_mode())['input']
        if self.tabs.currentIndex() == 0:
            return self._get_extract_controls(self.get_extract_mode())[0]
        return self._get_build_controls(self.get_build_mode())['input']

    def dragEnterEvent(self, event):
        self._input_edit().dragEnterEvent(event)

    def dropEvent(self, event):
        self._input_edit().dropEvent(event)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            if not self.worker.wait(2000):
                event.ignore()
                return
        super().closeEvent(event)
