"""Unified sidebar, full tool option pages, and one persistent log workspace."""
from __future__ import annotations
from .localized_ui import ui_call, ui_widget, ui_slot, source_text, retranslate_widgets
from ..i18n import tr, resolve_language
from .language import configure_language
import re
import sys
from pathlib import Path
import threading
from PyQt6.QtCore import QLocale, QSettings, QStandardPaths, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontDatabase, QPalette, QColor
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup, QCheckBox,
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QScrollArea, QSplitter, QStackedWidget, QVBoxLayout, QWidget)
from ..legacy_gui import asdis_gui, bsd_gui, bpd_gui
from .gdb import GdbToolWidget
from .database import DatabaseToolWidget
from .audio import AudioToolWidget
from .anmdef import AnmdefToolWidget
from .assets import AssetToolWidget, BmToolWindow
from .layouts import normalize_forms, install_segmented_modes, scroll_tab_contents, update_localized_geometry
from .comboboxes import apply_combo_theme, combo_stylesheet
from .controls import configure_spinboxes, spinbox_stylesheet
from .scripts import ScriptToolWindow
from .bpd import BpdToolWindow
from .ui_common import THEMES, _SYMBOL_PATTERN, _sanitize_widget_text

TOOLS = (
    ('BCS 剧情脚本工具', '脚本与文本', '剧情脚本提取、构建与 JSON / TXT 文本回填。'),
    ('BSD 反编译', '脚本与文本', '剧情脚本反编译、Ren’Py 输出与高级设置。'),
    ('BPD 反编译', '脚本与文本', '系统脚本反编译、IR 输出、函数映射与源码注释。'),
    ('GDB 工具', '数据与定义', '查看、编辑和验证 BURIKO GDB 存档。'),
    ('动画定义', '数据与定义', '在动画定义二进制与可编辑 CSV 之间转换。'),
    ('图片工具', '资源与媒体', '图片转换与 JSON 元信息回写，保留图片基点。'),
    ('封包工具', '资源与媒体', 'ARC 解包、封包与索引查看。'),
    ('BM 工具', '资源与媒体', 'BF_Movie 解码、动画导出与帧序列 / 视频编码。'),
    ('BP 系统脚本工具', '脚本与文本', 'BP 系统脚本提取、构建与 JSON / TXT 文本回填。'),
    ('数据库工具', '数据与定义', 'ScenarioDB / StringsDB 与 JSON 互转及无损验证。'),
    ('音频工具', '资源与媒体', 'BGI BW 与 WAV / FLAC / OGG / MP3 等音频互转。'),
    ('SDC 工具', '资源与媒体', 'SDC 解压与压缩，支持单文件、目录及封装信息回写。'),
    ('DSC 工具', '资源与媒体', 'DSC 解压与压缩，支持单文件、目录及封装信息回写。'))

class ToolNavigation(QWidget):
    currentChanged = pyqtSignal(int)
    def __init__(self, parent=None):
        super().__init__(parent)
        self._current, self._buttons = -1, {}
        self._group = QButtonGroup(self)
        self._group.idClicked.connect(self.setCurrentIndex)
        layout = QVBoxLayout(self); layout.setContentsMargins(12, 0, 12, 0); layout.setSpacing(3)
        for category, indices in ((tr('脚本与文本'), (0,8,1,2)), (tr('资源与媒体'), (5,10,6,11,12,7)), (tr('数据与定义'), (3,9,4))):
            label = ui_widget(QLabel, category); label.setObjectName('NavigationGroup'); layout.addWidget(label)
            for index in indices:
                button = ui_widget(QPushButton, tr(TOOLS[index][0])); button.setObjectName('ToolNavigationButton')
                button.setCheckable(True); button.setMinimumHeight(36)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                self._buttons[index] = button; self._group.addButton(button, index); layout.addWidget(button)
        layout.addStretch()
    def count(self): return len(self._buttons)
    def tabText(self, index): return self._buttons[index].text()
    def currentIndex(self): return self._current
    def usesScrollButtons(self): return False
    def setCurrentIndex(self, index):
        if index in self._buttons and self._current != index:
            self._current = index; self._buttons[index].setChecked(True); self.currentChanged.emit(index)

_fonts_initialized = False


def ensure_fonts():
    global _fonts_initialized
    if not _fonts_initialized:
        if not QFontDatabase.families():
            for name in ('msyh.ttc', 'segoeui.ttf', 'consola.ttf'):
                path = Path('C:/Windows/Fonts') / name
                if path.is_file():
                    QFontDatabase.addApplicationFont(str(path))
        _fonts_initialized = True
    QApplication.instance().setFont(QFont('Microsoft YaHei', 10))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ui_call(self, 'setWindowTitle', 'BGI Toolkit'); self.setObjectName('ToolkitWindow')
        self.resize(1080, 760); self.setMinimumSize(960, 680); self.setAcceptDrops(True)
        self.settings = QSettings(str(self._settings_path()), QSettings.Format.IniFormat)
        self.language_preference, self.ui_language = configure_language(self.settings)
        ensure_fonts()
        self.asdis_window, self.bsd_window, self.bpd_window = ScriptToolWindow('bcs'), bsd_gui.MainWindow(), BpdToolWindow()
        self.gdb_widget, self.anmdef_widget = GdbToolWidget(), AnmdefToolWidget()
        self.image_widget, self.archive_widget, self.bm_window = AssetToolWidget('image'), AssetToolWidget('archive'), BmToolWindow()
        self.bp_window = ScriptToolWindow('bp')
        self.database_widget = DatabaseToolWidget()
        self.audio_widget = AudioToolWidget(ffmpeg_path=self.bm_window.ffmpeg_edit.text())
        self.sdc_widget = AssetToolWidget('sdc')
        self.dsc_widget = AssetToolWidget('dsc')
        self.tool_windows = (self.asdis_window, self.bsd_window, self.bpd_window, self.gdb_widget,
            self.anmdef_widget, self.image_widget, self.archive_widget, self.bm_window, self.bp_window, self.database_widget, self.audio_widget, self.sdc_widget, self.dsc_widget)
        self._build_shell(); self._embed_tools()
        self.tool_tabs.currentChanged.connect(self._select_tool); self.tool_tabs.setCurrentIndex(0)
        theme = self.settings.value('ui/theme', '跟随系统', str)
        theme = {'现代浅色':'明亮','现代深色':'深色','赛博朋克':'深色'}.get(theme,theme)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(theme)))
        self.theme_combo.currentIndexChanged.connect(lambda _: self._apply_theme(self.theme_combo.currentData()))
        self._apply_theme(self.theme_combo.currentData())
        update_localized_geometry(self)
        self._acceleration_warmup_started = False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._acceleration_warmup_started:
            self._acceleration_warmup_started = True
            QTimer.singleShot(0, self._warm_up_acceleration)

    @staticmethod
    def _warm_up_acceleration():
        from ..bm_codec import HAS_OPTIMIZATION, prepare_numba
        if HAS_OPTIMIZATION:
            threading.Thread(target=prepare_numba, name='bgi-numba-warmup', daemon=True).start()

    @staticmethod
    def _settings_path():
        root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)) / 'bgi_toolkit'
        root.mkdir(parents=True, exist_ok=True); return root / 'bgi_toolkit.ini'

    def _build_shell(self):
        shell = QWidget(); shell.setObjectName('ShellRoot'); self.setCentralWidget(shell)
        row = QHBoxLayout(shell); row.setContentsMargins(0,0,0,0); row.setSpacing(0)
        sidebar = QFrame(); sidebar.setObjectName('Sidebar'); sidebar.setFixedWidth(222 if self.ui_language == 'en' else 202)
        self.sidebar = sidebar
        side = QVBoxLayout(sidebar); side.setContentsMargins(0,18,0,14); side.setSpacing(12)
        for text, name in [('BGI Toolkit','Brand'), (tr('脚本 · 资源 · 动画'),'BrandSubtitle')]:
            label = ui_widget(QLabel, text); label.setObjectName(name); side.addWidget(label)
        self.tool_tabs = ToolNavigation()
        navigation_scroll = QScrollArea(); navigation_scroll.setObjectName('NavigationScrollArea')
        navigation_scroll.setFrameShape(QFrame.Shape.NoFrame); navigation_scroll.setWidgetResizable(True)
        navigation_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        navigation_scroll.setWidget(self.tool_tabs); side.addWidget(navigation_scroll, 1)
        language_box = QWidget(); language_layout = QVBoxLayout(language_box)
        language_layout.setContentsMargins(16, 0, 16, 0); language_layout.setSpacing(6)
        language_label = ui_widget(QLabel, tr('界面语言')); language_label.setObjectName('NavigationGroup')
        language_layout.addWidget(language_label)
        self.language_combo = QComboBox(); self.language_combo.setObjectName('LanguageSelector')
        for label, code in ((tr('跟随系统'), 'auto'), ('简体中文', 'zh_CN'), ('English', 'en')):
            ui_call(self.language_combo, 'addItem', label, code)
        ui_call(self.language_combo, 'setAccessibleName', tr('界面语言'))
        ui_call(self.language_combo, 'setToolTip', tr('跟随系统会自动检测系统界面语言；选择后立即生效。'))
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(self.language_preference)))
        language_layout.addWidget(self.language_combo)
        self.language_combo.currentIndexChanged.connect(self._save_language_preference)
        side.addWidget(language_box)
        footer = ui_widget(QLabel, 'BGI / Ethornell'); footer.setObjectName('SidebarFooter'); side.addWidget(footer); row.addWidget(sidebar)
        workspace = QWidget(); workspace.setObjectName('Workspace')
        column = QVBoxLayout(workspace); column.setContentsMargins(24,18,24,16); column.setSpacing(14)
        header = QHBoxLayout(); titles = QVBoxLayout(); titles.setSpacing(4)
        self.category_label, self.page_title, self.page_description = ui_widget(QLabel), ui_widget(QLabel), ui_widget(QLabel)
        for widget, name in [(self.category_label,'PageCategory'), (self.page_title,'PageTitle'), (self.page_description,'PageDescription')]:
            widget.setObjectName(name); widget.setWordWrap(True); titles.addWidget(widget)
        header.addLayout(titles, 1); settings = QVBoxLayout(); settings.setSpacing(8)
        self.theme_combo = QComboBox()
        for name in THEMES: ui_call(self.theme_combo, 'addItem', tr(name), name)
        self.theme_combo.setFixedWidth(122); settings.addWidget(self.theme_combo)
        self.show_logs = ui_widget(QCheckBox, tr('显示运行日志')); self.show_logs.setChecked(True); settings.addWidget(self.show_logs)
        header.addLayout(settings); column.addLayout(header)
        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical); self.workspace_splitter.setHandleWidth(7)
        self.tool_stack = QStackedWidget(); self.tool_stack.setObjectName('ToolStack'); self.workspace_splitter.addWidget(self.tool_stack)
        self.log_panel = QFrame(); self.log_panel.setObjectName('SharedLogPanel')
        logs = QVBoxLayout(self.log_panel); logs.setContentsMargins(14,8,14,10); logs.setSpacing(8)
        toolbar = QHBoxLayout(); title = ui_widget(QLabel, tr('运行日志')); title.setObjectName('LogTitle'); toolbar.addWidget(title)
        self.log_tool_label = ui_widget(QLabel); self.log_tool_label.setObjectName('LogToolLabel'); toolbar.addWidget(self.log_tool_label); toolbar.addStretch()
        self.log_actions = QStackedWidget(); self.log_actions.setFixedWidth(92); self.log_actions.setFixedHeight(36); toolbar.addWidget(self.log_actions)
        self.expand_log = ui_widget(QPushButton, tr('展开日志')); self.expand_log.setCheckable(True)
        self.expand_log.toggled.connect(self._expand_log); toolbar.addWidget(self.expand_log)
        save = ui_widget(QPushButton, tr('导出日志')); save.clicked.connect(self._save_log); toolbar.addWidget(save); logs.addLayout(toolbar)
        self.log_stack = QStackedWidget(); logs.addWidget(self.log_stack,1); self.workspace_splitter.addWidget(self.log_panel)
        self.workspace_splitter.setSizes([340,290]); self.workspace_splitter.setCollapsible(0,False)
        column.addWidget(self.workspace_splitter,1); self.show_logs.toggled.connect(self._set_logs_visible); row.addWidget(workspace,1)

    def _embed_tools(self):
        # Reparent the existing logs and their action buttons. All option widgets
        # remain the original instances, with their existing signal connections.
        self._preserved_controls = []
        for child in self.tool_windows:
            self._preserved_controls.extend(child.findChildren(QAbstractButton))
            child.setWindowFlags(Qt.WindowType.Widget); child.setMinimumSize(0,0); child.setStyleSheet('')
            if hasattr(child,'theme_combo'): child.theme_combo.hide()
            for title in child.findChildren(QLabel,'AppTitle'): title.hide()
            for label in child.findChildren(QLabel):
                if label.text().strip() in (tr('主题:'), tr('主题：')): label.hide()
            _sanitize_widget_text(child)
            normalize_forms(child)
            if isinstance(child, ScriptToolWindow): install_segmented_modes(child)
            log = child.log_view; old_parent = log.parentWidget()
            actions = QWidget(); actions_layout = QHBoxLayout(actions); actions_layout.setContentsMargins(0,0,0,0); actions_layout.setSpacing(0)
            buttons = []
            if child is self.bm_window:
                buttons.append(child.clear_btn)
                for label in child.findChildren(QLabel):
                    if label.text().strip() == tr('日志输出:'): label.hide()
            elif not isinstance(old_parent,QSplitter):
                header = old_parent.findChild(QWidget,'LogHeader')
                if header: buttons.extend(header.findChildren(QPushButton))
            if not buttons:
                clear = ui_widget(QPushButton, tr('清空')); clear.clicked.connect(ui_slot(log, 'clear')); buttons.append(clear)
            for button in buttons:
                button.setStyleSheet(''); ui_call(button, 'setText', tr('清空')); button.setFixedSize(92,36); actions_layout.addWidget(button)
            self.log_actions.addWidget(actions); self.log_stack.addWidget(log)
            if isinstance(old_parent,QFrame) and not isinstance(old_parent,QSplitter) and old_parent is not child: old_parent.hide()
            tabs = getattr(child, 'task_tabs', None) or child.content_tabs
            scroll_tab_contents(tabs)
            self.tool_stack.addWidget(child)

    def _set_logs_visible(self, visible):
        if not visible and self.expand_log.isChecked(): self.expand_log.setChecked(False)
        self.log_panel.setVisible(visible)

    def _expand_log(self, expanded):
        if expanded: self._last_splitter_sizes = self.workspace_splitter.sizes()
        self.tool_stack.setVisible(not expanded)
        ui_call(self.expand_log, 'setText', tr('还原表单') if expanded else tr('展开日志'))
        if not expanded: self.workspace_splitter.setSizes(getattr(self,'_last_splitter_sizes',[340,290]))

    def _select_tool(self,index):
        self.tool_stack.setCurrentIndex(index); self.log_stack.setCurrentIndex(index); self.log_actions.setCurrentIndex(index)
        title, category, description = TOOLS[index]
        ui_call(self.page_title, 'setText', tr(title)); ui_call(self.category_label, 'setText', tr(category)); ui_call(self.page_description, 'setText', tr(description)); ui_call(self.log_tool_label, 'setText', tr(title))
    def _save_language_preference(self, _index):
        preference = self.language_combo.currentData()
        self.settings.setValue('ui/language', preference); self.settings.sync()
        self.setUpdatesEnabled(False)
        try:
            self.language_preference, self.ui_language = configure_language(self.settings)
            retranslate_widgets()
            self.sidebar.setFixedWidth(222 if self.ui_language == 'en' else 202)
            update_localized_geometry(self)
        finally:
            self.setUpdatesEnabled(True)
            self.update()

    def _active_tool(self): return self.tool_windows[self.tool_tabs.currentIndex()]
    def _apply_theme(self,name):
        if name not in THEMES: name = THEMES[0]
        for child in self.tool_windows:
            if hasattr(child,'theme_combo'):
                child.theme_combo.blockSignals(True); child.theme_combo.setCurrentText(name); child.theme_combo.blockSignals(False)
            child.setStyleSheet('')
        resolved = self.asdis_window._resolve_theme_name(name) if name == '跟随系统' else name
        dark = resolved in ('深色','现代深色')
        palette = QPalette()
        roles = {'Window': '#191c22' if dark else '#f6f7f9',
                 'WindowText': '#f0f1f4' if dark else '#242934',
                 'Base': '#242830' if dark else '#ffffff',
                 'AlternateBase': '#2a2f38' if dark else '#f3f5f8',
                 'Text': '#f0f1f4' if dark else '#242934',
                 'Button': '#282d36' if dark else '#ffffff',
                 'ButtonText': '#f0f1f4' if dark else '#242934',
                 'Highlight': '#6988ff', 'HighlightedText': '#ffffff',
                 'PlaceholderText': '#a1a9b8' if dark else '#8791a3',
                 'ToolTipBase': '#303640' if dark else '#ffffff',
                 'ToolTipText': '#f0f1f4' if dark else '#242934'}
        for role,color in roles.items(): palette.setColor(getattr(QPalette.ColorRole,role),QColor(color))
        QApplication.instance().setPalette(palette)
        self.setStyleSheet(self._shell_stylesheet('深色' if dark else '明亮'))
        configure_spinboxes(self)
        for combo in self.findChildren(QComboBox):
            apply_combo_theme(combo, palette, dark)

        self.settings.setValue('ui/theme',name); self.settings.sync()

    @staticmethod
    def _shell_stylesheet(name):
        dark = name == '深色'
        root,panel,field,text,muted,border = ('#191c22','#20242c','#292e37','#f0f1f4','#adb5c4','#353b46') if dark else ('#f6f7f9','#ffffff','#f7f8fa','#242934','#707b8e','#e4e7ed')
        accent = '#8ca7ff' if dark else '#4b67d6'; active = '#303c5c' if dark else '#edf1fd'
        return f'''
        QWidget {{ color:{text}; font-family:"Microsoft YaHei","Segoe UI"; font-size:9.5pt; }}
        QWidget#ShellRoot, QWidget#Workspace, QMainWindow#MainBackground {{ background:{root}; }}
        QFrame#Sidebar {{ background:{panel}; border-right:1px solid {border}; }}
        QScrollArea#NavigationScrollArea, QScrollArea#NavigationScrollArea > QWidget > QWidget {{ background:{panel}; border:none; }}
        QLabel#Brand {{ font-size:17pt; font-weight:600; padding:6px 20px 0; }}
        QLabel#BrandSubtitle {{ color:{muted}; padding:0 20px 8px; font-size:9pt; }}
        QLabel#NavigationGroup {{ color:{muted}; padding:8px 12px 4px; font-size:8.5pt; }}
        QPushButton#ToolNavigationButton {{ text-align:left; padding:0 16px; min-height:32px; background:transparent; border:none; border-radius:8px; font-size:10pt; }}
        QPushButton#ToolNavigationButton:hover {{ background:{field}; }}
        QPushButton#ToolNavigationButton:checked {{ background:{active}; color:{accent}; font-weight:600; }}
        QLabel#SidebarFooter {{ color:{muted}; padding:6px 24px; font-size:8.5pt; }}
        QLabel#PageCategory {{ color:{muted}; font-size:9pt; }}
        QLabel#PageTitle {{ font-size:19pt; font-weight:600; }}
        QLabel#PageDescription, QLabel#TipLabel {{ color:{muted}; }}
        QStackedWidget#ToolStack {{ background:{panel}; border:1px solid {border}; border-radius:10px; }}
        QScrollArea#ToolPageScrollArea {{ background:{panel}; border:none; }}
        QWidget#ToolPageViewport, QWidget#ToolPageSurface {{ background:{panel}; }}
        QFrame#CardFrame {{ background:transparent; border:none; }}
        QGroupBox {{ background:{panel}; border:1px solid {border}; border-radius:8px; }}
        QGroupBox {{ margin-top:12px; padding:12px 8px 8px; }}
        QGroupBox::title {{ subcontrol-origin:margin; left:12px; padding:0 5px; }}
        QLabel#SectionTitle {{ font-size:11pt; font-weight:600; }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background:{field}; border:1px solid {border}; border-radius:6px; min-height:25px; padding:4px 7px; selection-background-color:{accent}; }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color:{accent}; }}
        QPushButton {{ background:{panel}; border:1px solid {border}; border-radius:6px; padding:6px 12px; min-height:22px; }}
        QPushButton:hover {{ background:{field}; border-color:{accent}; }}
        QPushButton#PrimaryButton {{ background:#526ed3; color:white; border-color:#526ed3; font-weight:600; padding:6px 14px; }}
        QPushButton:disabled {{ color:{muted}; background:{field}; border-color:{border}; }}
        QCheckBox, QRadioButton {{ spacing:7px; padding:3px 0; }}
        QCheckBox::indicator, QRadioButton::indicator {{ width:15px; height:15px; }}
        QFrame#SegmentedControl {{ background:{field}; border:1px solid {border}; border-radius:7px; }}
        QPushButton#SegmentItem {{ background:transparent; color:{muted}; border:none; border-radius:4px; padding:2px 10px; min-height:28px; }}
        QPushButton#SegmentItem:checked {{ background:{panel}; color:{text}; font-weight:600; }}
        QPushButton#SegmentItem:hover {{ color:{text}; }}
        QRadioButton#ModeChip {{ padding:6px 14px; background:{field}; border:1px solid {border}; border-radius:6px; spacing:8px; }}
        QRadioButton#ModeChip:checked {{ background:{active}; color:{text}; border-color:{accent}; }}
        QTabWidget::pane {{ border:none; background:{panel}; }}
        QTabBar::tab {{ background:transparent; color:{muted}; padding:10px 14px; border:none; border-bottom:2px solid transparent; }}
        QTabBar::tab:selected {{ color:{accent}; border-bottom-color:{accent}; font-weight:600; }}
        QTabBar::tab:hover {{ background:{field}; }}
        QFrame#SharedLogPanel {{ background:{panel}; border:1px solid {border}; border-radius:10px; }}
        QLabel#LogTitle {{ font-weight:600; }}
        QLabel#LogToolLabel {{ color:{muted}; font-size:8.5pt; padding-left:8px; }}
        QTextEdit, QPlainTextEdit, QTableWidget, QListWidget, QTreeWidget {{ background:{field}; color:{text}; border:1px solid {border}; border-radius:6px; selection-background-color:{accent}; }}
        QTextEdit#LogConsole {{ border:none; background:{panel}; font-family:"Consolas","Microsoft YaHei"; font-size:9pt; }}
        QSplitter::handle {{ background:transparent; }} QSplitter::handle:hover {{ background:{border}; }}
        QSlider::groove:horizontal {{ height:5px; background:{border}; border-radius:2px; }}
        QSlider::sub-page:horizontal {{ background:{accent}; border-radius:2px; }}
        QSlider::handle:horizontal {{ background:{accent}; width:15px; height:15px;
            margin:-5px 0; border:2px solid {panel}; border-radius:8px; }}
        QSlider::handle:horizontal:hover {{ background:{text}; }}
        QProgressBar {{ border:1px solid {border}; border-radius:5px; text-align:center; background:{field}; }}
        QProgressBar::chunk {{ background:{accent}; border-radius:4px; }}
        QScrollBar:vertical {{ background:transparent; width:9px; margin:0; }}
        QScrollBar::handle:vertical {{ background:{border}; border-radius:4px; min-height:24px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        QToolTip {{ background:{panel}; color:{text}; border:1px solid {border}; padding:5px; }}
        ''' + combo_stylesheet(dark) + spinbox_stylesheet(dark)
    def _save_log(self):
        path,_ = QFileDialog.getSaveFileName(self,tr('导出运行日志'),'bgi-toolkit.log',tr('日志 (*.log *.txt)'))
        if path: Path(path).write_text(self._active_tool().log_view.toPlainText(),encoding='utf-8')
    def dragEnterEvent(self,event): self._active_tool().dragEnterEvent(event)
    def dropEvent(self,event): self._active_tool().dropEvent(event)
    def closeEvent(self,event):
        for child in self.tool_windows:
            if not child.close(): event.ignore(); return
        super().closeEvent(event)

def main(argv=None):
    app = QApplication(argv or sys.argv)
    app.setApplicationName('BGI Toolkit'); app.setOrganizationName('BGI Toolkit'); app.setStyle('Fusion')
    window = MainWindow(); window.show(); return app.exec()
