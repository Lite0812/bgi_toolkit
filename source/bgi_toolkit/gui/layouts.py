"""Reflow existing option widgets into a consistent desktop form layout."""
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import LocalizedText, get_language, tr
from PyQt6.QtCore import QEvent, QObject, QTimer, Qt
from PyQt6.QtWidgets import (QAbstractButton, QBoxLayout, QComboBox, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton,
    QSizePolicy, QSpinBox, QSlider, QStackedWidget, QTabWidget, QVBoxLayout, QWidget, QScrollArea, QRadioButton)


def arrange_action_buttons(layout, widgets):
    """Keep utility actions left and group compact task controls on the right."""
    while layout.count():
        layout.takeAt(0)
    primary = [widget for widget in widgets if widget.objectName() == 'PrimaryButton']
    utilities = [widget for widget in widgets if tr('打开') in widget.text()]
    for widget in utilities:
        layout.addWidget(widget)
    layout.addStretch(1)
    for widget in widgets:
        if widget not in primary and widget not in utilities:
            layout.addWidget(widget)
    for widget in primary:
        layout.addWidget(widget)
    layout.setContentsMargins(0, 6, 0, 0)
    layout.setSpacing(8)
    for widget in widgets:
        minimum = 120 if widget in primary else 80
        widget.setFixedWidth(max(minimum, widget.fontMetrics().horizontalAdvance(widget.text()) + 32))
        widget.setFixedHeight(36)
        widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


LABEL_WIDTH = 136


def form_label(label):
    if isinstance(label, str):
        label = ui_widget(QLabel, label)
    label.setObjectName('FormLabel')
    label.setFixedWidth(160 if get_language() == "en" else LABEL_WIDTH)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return label


class FormGrid(QGridLayout):
    def __init__(self):
        super().__init__()
        self.setContentsMargins(0, 0, 0, 0)
        self.setHorizontalSpacing(12)
        self.setVerticalSpacing(12)
        self.setColumnMinimumWidth(0, LABEL_WIDTH)
        self.setColumnStretch(1, 1)
        self._next_row = 0

    def addRow(self, label, field=None):
        row = self._next_row
        self._next_row += 1
        if field is None:
            self.addWidget(label, row, 0, 1, 2)
        else:
            if label:
                self.addWidget(form_label(label), row, 0)
            self.addWidget(field, row, 1)


class FitPageStack(QObject):
    """Let a mode stack use its current page height rather than its largest page."""
    def __init__(self, stack):
        super().__init__(stack)
        self.stack, self.pending = stack, False
        for index in range(stack.count()):
            stack.widget(index).installEventFilter(self)
        stack.currentChanged.connect(self.schedule)
        self.schedule()

    def schedule(self, *_):
        if not self.pending:
            self.pending = True
            QTimer.singleShot(0, self.adjust)

    def adjust(self):
        self.pending = False
        page = self.stack.currentWidget()
        if page is not None:
            page.layout().activate()
            height = max(1, page.sizeHint().height())
            if self.stack.minimumHeight() != height or self.stack.maximumHeight() != height:
                self.stack.setFixedHeight(height)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.StyleChange, QEvent.Type.Resize, QEvent.Type.Show):
            self.schedule()
        return False


def _normalized_tab_title(text):
    return text.replace(tr('提取（Extract）'), tr('提取')).replace(tr('构建（Build）'), tr('构建'))


def scroll_tab_contents(tabs):
    """Scroll each page's contents while keeping the tab bar in place."""
    for index in range(tabs.count()):
        page = tabs.widget(index)
        layout = page.layout()
        if isinstance(page, QScrollArea) or layout is None:
            continue
        if any(isinstance(layout.itemAt(i).widget(), QScrollArea) for i in range(layout.count())):
            continue
        # Keep the original page object: mapping actions and BM controls use its
        # identity to determine the active operation. Only move its layout.
        content = QWidget()
        content.setObjectName('ToolPageSurface')
        content.setLayout(layout)
        scroll = QScrollArea(page)
        scroll.setObjectName('ToolPageScrollArea')
        scroll.viewport().setObjectName('ToolPageViewport')
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(scroll)


def update_localized_geometry(root):
    for label in root.findChildren(QLabel, 'FormLabel'):
        label.setFixedWidth(160 if get_language() == 'en' else LABEL_WIDTH)
    for button in root.findChildren(QPushButton):
        if button.minimumWidth() == button.maximumWidth() and button.maximumWidth() < 400:
            base = getattr(button, '_language_base_width', button.maximumWidth())
            button._language_base_width = base
            button.setFixedWidth(max(base, button.fontMetrics().horizontalAdvance(button.text()) + 32))
    for frame in root.findChildren(QFrame, 'SegmentedControl'):
        buttons = frame.findChildren(QPushButton, 'SegmentItem')
        width = sum(max(112, button.fontMetrics().horizontalAdvance(button.text()) + 24) for button in buttons)
        frame.setFixedWidth(max(128*len(buttons), width + 6 + 2*max(0, len(buttons)-1)))



def normalize_forms(root):
    """Align labels and fields without adding margins to nested control rows."""
    for widget in (root, *root.findChildren(QWidget)):
        if widget.styleSheet() and widget.objectName() != 'LogConsole':
            widget.setStyleSheet('')
        layout = widget.layout()
        if isinstance(layout, QHBoxLayout):
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
        elif isinstance(layout, QVBoxLayout):
            layout.setSpacing(12)
            if widget.objectName() == 'CardFrame':
                layout.setContentsMargins(16, 16, 16, 16)
            elif widget.objectName() in ('ToolPageSurface', 'ToolFormBody', 'ToolFooter'):
                pass
            elif isinstance(widget.parentWidget(), QStackedWidget) and widget.parentWidget().objectName() == 'qt_tabwidget_stackedwidget' and not widget.findChildren(QFrame, 'CardFrame'):
                layout.setContentsMargins(16, 16, 16, 16)
            else:
                layout.setContentsMargins(0, 0, 0, 0)
        if isinstance(widget, QStackedWidget) and widget.objectName() != 'qt_tabwidget_stackedwidget' and not hasattr(widget, '_form_sizer'):
            widget._form_sizer = FitPageStack(widget)
        if isinstance(widget, QRadioButton):
            widget.setObjectName('ModeChip')
            parent = widget.parentWidget()
            if parent is not None:
                for label in parent.findChildren(QLabel, 'TipLabel'):
                    ui_call(widget, 'setToolTip', source_text(label, fallback=label.text()))
                    label.hide()
        if isinstance(widget, QTabWidget):
            for index in range(widget.count()):
                title = source_text(widget, 'setTabText', index, fallback=widget.tabText(index))
                title = title.transformed(_normalized_tab_title) if isinstance(title, LocalizedText) else _normalized_tab_title(title)
                ui_call(widget, 'setTabText', index, title)
        if isinstance(widget, QLabel):
            if widget.text().strip() in (tr('提取 (Extract)'), tr('构建 (Build)'), tr('BP 提取 (Extract)'), tr('BP 构建 (Build)')):
                widget.hide()
            if widget.objectName() == 'TipLabel':
                widget.setWordWrap(True)
        elif isinstance(widget, QPushButton):
            widget.setMinimumHeight(36)
            widget.setMaximumHeight(36)
            widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            # Previously fixed browse buttons could cut off long captions.
            if widget.maximumWidth() < widget.fontMetrics().horizontalAdvance(widget.text()) + 28:
                widget.setFixedWidth(widget.fontMetrics().horizontalAdvance(widget.text()) + 28)
        elif isinstance(widget, (QLineEdit, QComboBox)):
            widget.setMinimumHeight(36)

    seen = set()
    def walk(layout):
        if layout is None or layout in seen:
            return
        seen.add(layout)
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.layout():
                walk(item.layout())
            elif item.widget():
                walk(item.widget().layout())
        if isinstance(layout, QHBoxLayout):
            widgets = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget()]
            nested = [layout.itemAt(i).layout() for i in range(layout.count()) if layout.itemAt(i).layout()]
            if widgets and all(isinstance(widget, QPushButton) for widget in widgets) and (len(widgets) > 1 or widgets[0].objectName() == 'PrimaryButton'):
                arrange_action_buttons(layout, widgets)
            elif (widgets and isinstance(widgets[0], QLabel)
                    and any(isinstance(widget, (QLineEdit, QComboBox)) for widget in widgets[1:])):
                form_label(widgets[0])
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(12)
            # Put the encoding/version parameters on the same label/field grid
            # as the paths instead of distributing them across the whole row.
            if (not widgets and 2 <= len(nested) <= 3
                    and all(isinstance(child, QHBoxLayout) and any(isinstance(child.itemAt(j).widget(), QComboBox) for j in range(child.count())) for child in nested)):
                owner = layout.parent()
                if isinstance(owner, QBoxLayout):
                    position = next(i for i in range(owner.count()) if owner.itemAt(i).layout() is layout)
                    wrapper = QWidget(); wrapper.setObjectName('FormBlock')
                    column = QVBoxLayout(wrapper); column.setContentsMargins(0, 0, 0, 0); column.setSpacing(12)
                    while layout.count():
                        item = layout.takeAt(0)
                        if item.layout():
                            column.addLayout(item.layout())
                    owner.takeAt(position); owner.insertWidget(position, wrapper)
                    root.__dict__.setdefault('_retired_form_rows', []).append(layout)
        if not isinstance(layout, QVBoxLayout):
            return
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if isinstance(widget, QPushButton) and widget.objectName() == 'PrimaryButton':
                layout.takeAt(index)
                actions = QHBoxLayout(); arrange_action_buttons(actions, [widget])
                layout.insertLayout(index, actions)
        index = 0
        while index + 1 < layout.count():
            label = layout.itemAt(index).widget()
            row = layout.itemAt(index + 1).layout()
            if (isinstance(label, QLabel) and not label.wordWrap() and len(label.text()) < 45
                    and isinstance(row, QHBoxLayout)
                    and any(isinstance(row.itemAt(j).widget(), QLineEdit) for j in range(row.count()))):
                layout.takeAt(index); layout.takeAt(index)
                wrapper = QWidget(); wrapper.setObjectName('FormRow')
                grid = QGridLayout(wrapper); grid.setContentsMargins(0, 0, 0, 0)
                grid.setHorizontalSpacing(12); grid.setColumnStretch(1, 1)
                grid.addWidget(form_label(label), 0, 0)
                row.setContentsMargins(0, 0, 0, 0); row.setSpacing(8)
                grid.addLayout(row, 0, 1)
                layout.insertWidget(index, wrapper)
            index += 1
    for widget in (root, *root.findChildren(QWidget)):
        walk(widget.layout())


def rebuild_bm(window):
    """Keep every original BM control on one aligned form per settings page."""
    old = window.takeCentralWidget()
    window._old_ui = old
    main = QWidget(); main.setObjectName('ToolFormBody')
    layout = QVBoxLayout(main); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(12)
    tabs = QTabWidget(); window.task_tabs = tabs; layout.addWidget(tabs, 1)
    def page(title):
        content = QWidget(); content.setObjectName('ToolPageSurface')
        box = QVBoxLayout(content); box.setContentsMargins(16, 16, 16, 16); box.setSpacing(12)
        form = FormGrid(); box.addLayout(form); box.addStretch()
        scroll = QScrollArea(); scroll.setObjectName('ToolPageScrollArea')
        scroll.viewport().setObjectName('ToolPageViewport')
        scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content); ui_call(tabs, 'addTab', scroll, title)
        return form
    def row(*widgets):
        wrapper = QWidget(); wrapper.setObjectName('FormRow')
        box = QHBoxLayout(wrapper); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(8)
        expands = False
        for widget in widgets:
            stretch = isinstance(widget, (QLineEdit, QSlider))
            expands |= stretch
            box.addWidget(widget, 1 if stretch else 0)
        if not expands:
            box.addStretch(1)
        return wrapper
    def browse(text, callback):
        button = ui_widget(QPushButton, text); button.clicked.connect(callback); return button
    def combo(widget):
        widget.setMinimumWidth(220); widget.setMaximumWidth(420)
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        return widget
    for widget in (window.fps_spin, window.crf_spin, window.deblock_strength_spin,
                   window.thread_spin, window.file_parallel_spin):
        widget.setFixedWidth(100)
        widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    form = page(tr('输入与输出'))
    ui_call(form, 'addRow', tr('BM 文件或目录'), row(window.input_edit, browse(tr('选择文件'), window.browse_input_file), browse(tr('选择目录'), window.browse_input_folder)))
    ui_call(form, 'addRow', tr('输出目录'), row(window.output_edit, browse(tr('选择目录'), window.browse_output_folder)))
    ui_call(form, 'addRow', tr('输出方式'), row(window.radio_png, window.radio_video))
    ui_call(window.btn_ffmpeg, 'setText', tr('选择 FFmpeg'))
    ui_call(form, 'addRow', 'FFmpeg', row(window.ffmpeg_edit, window.btn_ffmpeg))
    window.ffmpeg_edit.show(); window.btn_ffmpeg.show()
    frame = window.video_opts_frame
    for label in frame.findChildren(QLabel):
        if label is not window.crf_label:
            label.hide()
    video_layout = frame.layout()
    while video_layout.count():
        item = video_layout.takeAt(0)
        if item.layout():
            while item.layout().count(): item.layout().takeAt(0)
    video_layout.setContentsMargins(0, 0, 0, 0)
    video_form = FormGrid(); video_layout.addLayout(video_form)
    ui_call(video_form, 'addRow', tr('视频格式'), row(combo(window.format_combo)))
    ui_call(video_form, 'addRow', tr('帧率'), row(combo(window.fps_mode), window.fps_spin))
    ui_call(video_form, 'addRow', tr('画质'), row(window.crf_slider, window.crf_spin))
    ui_call(window.crf_label, 'setText', tr('CRF 0–63：数值越小画质越高，建议 15–30。'))
    window.crf_label.setObjectName('TipLabel'); window.crf_label.setWordWrap(True)
    ui_call(video_form, 'addRow', '', window.crf_label)
    ui_call(form, 'addRow', frame)
    form = page(tr('透明与画质'))
    ui_call(window.remove_black_bg_checkbox, 'setText', tr('24 位图片去黑底'))
    ui_call(window.remove_black_bg_32_checkbox, 'setText', tr('32 位全不透明图片去黑底'))
    ui_call(form, 'addRow', tr('透明恢复'), window.remove_black_bg_checkbox)
    ui_call(form, 'addRow', '', window.remove_black_bg_32_checkbox)
    ui_call(form, 'addRow', tr('指定去黑底文件'), row(window.custom_black_bg_edit, window.custom_black_bg_btn))
    ui_call(form, 'addRow', tr('降噪与去块'), window.deblock_checkbox)
    ui_call(form, 'addRow', tr('处理算法'), row(combo(window.deblock_method_combo)))
    ui_call(form, 'addRow', tr('处理强度'), row(window.deblock_strength_spin))
    window.deblock_strength_desc.setWordWrap(True); ui_call(form, 'addRow', '', window.deblock_strength_desc)
    ui_call(form, 'addRow', tr('Ren’Py 输出'), window.renpy_enable_checkbox)
    ui_call(form, 'addRow', tr('遮罩方式'), row(combo(window.renpy_output_combo)))
    ui_call(form, 'addRow', '', window.keep_original_video_checkbox)
    form = page(tr('性能设置'))
    ui_call(form, 'addRow', tr('帧级并行方式'), row(combo(window.parallel_mode_combo)))
    ui_call(form, 'addRow', tr('帧级并行数量'), row(window.thread_spin))
    ui_call(form, 'addRow', tr('文件并行数量'), row(window.file_parallel_spin))
    hint = ui_widget(QLabel, tr('数量为 0 时自动选择；文件并行数量为 1 时关闭文件并行。'))
    hint.setWordWrap(True); hint.setObjectName('TipLabel'); ui_call(form, 'addRow', '', hint)
    ui_call(form, 'addRow', tr('性能测试'), row(window.benchmark_btn))
    footer = QWidget(); footer.setObjectName('ToolFooter')
    footer_layout = QVBoxLayout(footer); footer_layout.setContentsMargins(16, 0, 16, 16); footer_layout.setSpacing(12)
    status = QHBoxLayout(); status.setContentsMargins(0, 0, 0, 0)
    status.addWidget(window.status_label); status.addWidget(window.progress_bar, 1)
    footer_layout.addLayout(status)
    controls = QHBoxLayout(); arrange_action_buttons(controls, [window.stop_btn, window.pause_btn, window.start_btn])
    footer_layout.addLayout(controls); layout.addWidget(footer)
    layout.addWidget(window.clear_btn); layout.addWidget(window.log_view)
    window.setCentralWidget(main); old.hide()
    window.toggle_mode(None)


def install_segmented_modes(root):
    """Use compact linked segments while retaining the original radio callbacks."""
    from PyQt6.QtWidgets import QButtonGroup
    root._retired_mode_rows = []
    for group in list(root.findChildren(QButtonGroup)):
        originals = group.buttons()
        if not 2 <= len(originals) <= 4 or not all(isinstance(button,QRadioButton) for button in originals):
            continue
        ancestor = originals[0].parentWidget()
        while ancestor is not None and not all(ancestor.isAncestorOf(button) for button in originals):
            ancestor = ancestor.parentWidget()
        if ancestor is None: continue
        def covers(layout, button):
            for i in range(layout.count()):
                item=layout.itemAt(i)
                if item.widget() and (item.widget() is button or item.widget().isAncestorOf(button)): return True
                if item.layout() and covers(item.layout(),button): return True
            return False
        candidates=[]
        def find(layout):
            if layout is None:return
            if isinstance(layout,QHBoxLayout) and all(covers(layout,button) for button in originals): candidates.append(layout)
            for i in range(layout.count()):
                item=layout.itemAt(i)
                if item.layout():find(item.layout())
        find(ancestor.layout())
        if not candidates:continue
        old=candidates[-1];owner=old.parent()
        if not isinstance(owner,QBoxLayout):continue
        position=next((i for i in range(owner.count()) if owner.itemAt(i).layout() is old),None)
        if position is None:continue
        frame=QFrame();frame.setObjectName('SegmentedControl');frame.setFixedHeight(40);frame.setFixedWidth(360 if len(originals)==3 else 128*len(originals))
        row=QHBoxLayout(frame);row.setContentsMargins(3,3,3,3);row.setSpacing(2)
        linked=QButtonGroup(frame);linked.setExclusive(True)
        for index, original in enumerate(originals):
            button=ui_widget(QPushButton, source_text(original, fallback=original.text()));button.setObjectName('SegmentItem');button.setCheckable(True)
            button.setChecked(original.isChecked());button.setMinimumWidth(0);button.setMinimumHeight(30)
            button.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
            linked.addButton(button,index);row.addWidget(button,1)
            button.clicked.connect(lambda checked=False,source=original:source.click())
            original.toggled.connect(lambda checked,target=button:target.setChecked(checked))
        for i in range(old.count()):
            item=old.itemAt(i)
            if item.widget():item.widget().hide()
        owner.takeAt(position);owner.insertWidget(position,frame,0,Qt.AlignmentFlag.AlignLeft)
        root._retired_mode_rows.append(old)
