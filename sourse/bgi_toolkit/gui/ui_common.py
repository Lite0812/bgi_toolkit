from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import LocalizedText, tr
import re
from collections.abc import Iterable
from PyQt6.QtWidgets import QWidget, QLabel, QAbstractButton, QGroupBox, QLineEdit, QComboBox, QTabWidget

THEMES = ("跟随系统", "明亮", "深色")
_SYMBOL_PATTERN = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")


def _plain_ui_text(value: str) -> str:
    if isinstance(value, LocalizedText):
        return value.transformed(_plain_ui_text)
    value = value.replace("☆/★", tr("原文/译文"))
    value = value.replace("☆", tr("原文")).replace("★", tr("译文"))
    value = _SYMBOL_PATTERN.sub("", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


def _sanitize_widget_text(root: QWidget) -> None:
    widgets: Iterable[QWidget] = (root, *root.findChildren(QWidget))
    for widget in widgets:
        if isinstance(widget, QLabel):
            ui_call(widget, 'setText', _plain_ui_text(source_text(widget, "setText", fallback=widget.text())))
        elif isinstance(widget, QAbstractButton):
            ui_call(widget, 'setText', _plain_ui_text(source_text(widget, "setText", fallback=widget.text())))
        elif isinstance(widget, QGroupBox):
            ui_call(widget, 'setTitle', _plain_ui_text(source_text(widget, "setTitle", fallback=widget.title())))

        if isinstance(widget, QLineEdit):
            ui_call(widget, 'setPlaceholderText', _plain_ui_text(source_text(widget, "setPlaceholderText", fallback=widget.placeholderText())))
        elif isinstance(widget, QComboBox):
            for index in range(widget.count()):
                ui_call(widget, 'setItemText', index, _plain_ui_text(source_text(widget, "setItemText", index, fallback=widget.itemText(index))))

        ui_call(widget, 'setToolTip', _plain_ui_text(source_text(widget, "setToolTip", fallback=widget.toolTip())))
        ui_call(widget, 'setStatusTip', _plain_ui_text(source_text(widget, "setStatusTip", fallback=widget.statusTip())))
        ui_call(widget, 'setWhatsThis', _plain_ui_text(source_text(widget, "setWhatsThis", fallback=widget.whatsThis())))

        if isinstance(widget, QTabWidget):
            for index in range(widget.count()):
                ui_call(widget, 'setTabText', index, _plain_ui_text(source_text(widget, "setTabText", index, fallback=widget.tabText(index))))
                ui_call(widget, 'setTabToolTip', index, _plain_ui_text(source_text(widget, "setTabToolTip", index, fallback=widget.tabToolTip(index))))


