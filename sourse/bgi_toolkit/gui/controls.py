"""Compact step controls with native spin-box keyboard and range behavior."""
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QDoubleSpinBox, QSpinBox, QWidget, QHBoxLayout, QSlider, QLabel


def configure_spinboxes(root):
    for widget in root.findChildren((QSpinBox, QDoubleSpinBox)):
        widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        widget.setFixedHeight(36)
        widget.lineEdit().setStyleSheet('QLineEdit { background:transparent; border:none; border-radius:0; padding:0; min-height:0; }')


def spinbox_stylesheet(dark):
    field, text, muted, border, hover = ('#292e37','#f0f1f4','#adb5c4','#353b46','#353d4b') if dark else ('#f7f8fa','#242934','#707b8e','#dce1e9','#e8ecf3')
    accent = '#8ca7ff' if dark else '#4b67d6'
    icons = Path(__file__).parent / 'icons'
    suffix = 'dark' if dark else 'light'
    plus, minus = ((icons / f'{name}-{suffix}.svg').as_posix() for name in ('plus','minus'))
    return f"""
        QSpinBox, QDoubleSpinBox {{ background:{field}; color:{text}; border:1px solid {border};
            border-radius:7px; padding:6px 0px; min-height:22px; }}
        QSpinBox:hover, QDoubleSpinBox:hover {{ border-color:{muted}; }}
        QSpinBox:focus, QDoubleSpinBox:focus {{ border-color:{accent}; }}
        QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin:border;
            subcontrol-position:center right; width:24px; height:26px; margin-right:4px;
            border:0; border-radius:4px; background:transparent; }}
        QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin:border;
            subcontrol-position:center left; width:24px; height:26px; margin-left:4px;
            border:0; border-radius:4px; background:transparent; }}
        QSpinBox::up-button:hover, QSpinBox::down-button:hover,
        QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{ background:{hover}; }}
        QSpinBox::up-button:pressed, QSpinBox::down-button:pressed,
        QDoubleSpinBox::up-button:pressed, QDoubleSpinBox::down-button:pressed {{ background:{border}; }}
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image:url("{plus}"); width:12px; height:12px; }}
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image:url("{minus}"); width:12px; height:12px; }}
        QSpinBox:disabled, QDoubleSpinBox:disabled {{ color:{muted}; }}
    """


class QualitySlider(QWidget):
    """Quality 1..100, with zero retaining JSON tables (new inputs use 90)."""
    valueChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setMinimumHeight(36)
        self.slider.setMinimumWidth(140)
        self.label = ui_widget(QLabel, tr('自动'))
        self.label.setMinimumWidth(48)
        self.label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.slider, 1)
        row.addWidget(self.label)
        ui_call(self, 'setToolTip', tr('自动：沿用 JSON 量化表，没有 JSON 时使用质量 90。1～100：数值越大画质越高、文件越大。'))
        self.slider.valueChanged.connect(self._changed)

    def _changed(self, value):
        ui_call(self.label, 'setText', str(value) if value else tr('自动'))
        self.valueChanged.emit(value)

    def value(self):
        return self.slider.value()

    def setValue(self, value):
        self.slider.setValue(value)
