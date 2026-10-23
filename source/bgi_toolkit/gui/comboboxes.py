"""Shared flat combo boxes and readable, compact popup lists."""
from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen, QPolygonF, QRegion
from PyQt6.QtWidgets import QFrame, QListView, QStyle, QStyledItemDelegate


class PopupSurface(QObject):
    """Use an opaque native surface; clip corners without alpha-window artifacts."""
    def __init__(self, popup):
        super().__init__(popup)
        self.popup = popup
        popup.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Resize):
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.popup.rect()), 8, 8)
            self.popup.setMask(QRegion(path.toFillPolygon().toPolygon()))
        return False


class ComboItemDelegate(QStyledItemDelegate):
    def __init__(self, combo, view):
        super().__init__(view)
        self.combo = combo
        self.dark = False

    def sizeHint(self, option, index):
        return QSize(option.fontMetrics.horizontalAdvance(str(index.data() or '')) + 56,
                     max(36, option.fontMetrics.height() + 16))

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = index.row() == self.combo.currentIndex()
        hovered = bool(option.state & (QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Selected))
        enabled = bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        text = QColor('#f0f1f4' if self.dark else '#242934')
        accent = QColor('#a8bbff' if self.dark else '#4b67d6')
        rect = option.rect.adjusted(1, 1, -1, -1)
        if selected or hovered:
            background = ('#303c5c' if self.dark else '#edf1fd') if selected else ('#2b3039' if self.dark else '#f3f5f8')
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(background))
            painter.drawRoundedRect(QRectF(rect), 5, 5)
        painter.setFont(option.font)
        painter.setPen((accent if selected else text) if enabled else QColor('#8b94a5'))
        label_rect = rect.adjusted(11, 0, -34, 0)
        label = option.fontMetrics.elidedText(str(index.data() or ''), Qt.TextElideMode.ElideRight, label_rect.width())
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        if selected:
            x, y = rect.right() - 18, rect.center().y()
            painter.setPen(QPen(accent, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(QPolygonF([QPointF(x - 4, y), QPointF(x - 1, y + 3), QPointF(x + 5, y - 4)]))
        painter.restore()


def apply_combo_theme(combo, palette, dark):
    """Retain the combo/model/signals and replace only its native menu presentation."""
    if not hasattr(combo, '_modern_delegate'):
        view = QListView(combo)
        view.setObjectName('ComboList')
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setMouseTracking(True)
        view.setUniformItemSizes(True)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        combo.setView(view)
        combo._modern_delegate = ComboItemDelegate(combo, view)
        combo.setItemDelegate(combo._modern_delegate)
        combo.setMaxVisibleItems(8)
        combo.setMaximumWidth(min(combo.maximumWidth(), 420))
    view = combo.view()
    combo._modern_delegate.dark = dark
    view.setPalette(palette)
    row_height = max(36, combo.fontMetrics().height() + 16)
    view.setMinimumHeight(min(combo.count(), combo.maxVisibleItems()) * row_height + 8)
    text_width = max((combo.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
    view.setMinimumWidth(min(480, max(164, text_width + 60)))
    panel, text, border = ('#242830', '#f0f1f4', '#414956') if dark else ('#ffffff', '#242934', '#dce1e9')
    view.setStyleSheet(f'''
        QListView#ComboList {{ background:{panel}; color:{text}; border:0;
            border-radius:6px; padding:4px; outline:0; }}
        QListView#ComboList::item {{ border:0; }}
        QScrollBar:vertical {{ background:transparent; width:8px; margin:3px 0; }}
        QScrollBar::handle:vertical {{ background:{border}; border-radius:4px; min-height:24px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
    ''')
    popup = view.window()
    popup.setObjectName('ComboPopup')
    popup.setFrameShape(QFrame.Shape.NoFrame)
    if not hasattr(popup, '_surface'):
        popup.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        popup.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        popup._surface = PopupSurface(popup)
    popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    popup.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
    popup.setAutoFillBackground(True)
    popup_palette = QPalette(palette)
    popup_palette.setColor(QPalette.ColorRole.Window, QColor(panel))
    popup_palette.setColor(QPalette.ColorRole.Base, QColor(panel))
    popup.setPalette(popup_palette)
    popup.setStyleSheet(f'QFrame#ComboPopup {{ background:{panel}; border:1px solid {border}; border-radius:8px; }}')
    if popup.layout():
        popup.layout().setContentsMargins(3, 3, 3, 3)
    if combo.isEditable():
        combo.lineEdit().setStyleSheet('QLineEdit { background:transparent; border:none; border-radius:0; padding:0; min-height:0; }')
    view.viewport().update()


def combo_stylesheet(dark):
    field, text, muted, border = ('#292e37', '#f0f1f4', '#adb5c4', '#353b46') if dark else ('#f7f8fa', '#242934', '#707b8e', '#dce1e9')
    accent = '#8ca7ff' if dark else '#4b67d6'
    arrow = (Path(__file__).parent / 'icons' / f'chevron-{"dark" if dark else "light"}.svg').as_posix()
    return f'''
        QComboBox {{ background:{field}; color:{text}; border:1px solid {border};
            border-radius:7px; min-height:22px; padding:6px 32px 6px 10px; combobox-popup:0; }}
        QComboBox:hover {{ border-color:{muted}; }}
        QComboBox:focus, QComboBox:on {{ border-color:{accent}; }}
        QComboBox:disabled {{ color:{muted}; }}
        QComboBox::drop-down {{ subcontrol-origin:padding; subcontrol-position:top right;
            width:28px; border:0; background:transparent; }}
        QComboBox::down-arrow {{ image:url("{arrow}"); width:12px; height:12px; }}
    '''
