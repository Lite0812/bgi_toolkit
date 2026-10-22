"""Track source messages on real Qt widgets without rebuilding pages.

Only LocalizedText values are bound. User input, paths, script samples, item data,
and existing log contents remain ordinary strings and are never retranslated.
"""
from __future__ import annotations

import weakref
from PyQt6 import sip
from PyQt6.QtCore import QObject, QPersistentModelIndex, QSignalBlocker, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QApplication, QComboBox, QFormLayout, QGroupBox,
    QListWidget, QListWidgetItem, QProgressBar, QTabWidget, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator)
from ..i18n import LocalizedText, tr

_objects = weakref.WeakValueDictionary()


_PROPERTY = "_bgi_localized_messages"
_ROLE = int(Qt.ItemDataRole.UserRole) + 18391


class _ComboRow:
    # Keep the dictionary key stable when Qt moves or invalidates a model index.
    def __init__(self, index):
        self.index = index


class _MessageStore:
    def __init__(self):
        self.messages = {}


def _store(obj, create=False):
    if isinstance(obj, QObject):
        store = obj.property(_PROPERTY)
        if store is None and create:
            store = _MessageStore()
            obj.setProperty(_PROPERTY, store)
    elif isinstance(obj, QTreeWidgetItem):
        store = obj.data(0, _ROLE)
        if store is None and create:
            store = _MessageStore()
            obj.setData(0, _ROLE, store)
    elif isinstance(obj, (QTableWidgetItem, QListWidgetItem)):
        store = obj.data(_ROLE)
        if store is None and create:
            store = _MessageStore()
            obj.setData(_ROLE, store)
    else:
        store = getattr(obj, _PROPERTY, None)
        if store is None and create:
            store = _MessageStore()
            setattr(obj, _PROPERTY, store)
    return store.messages if store is not None else {}


def _remember(obj, key, value):
    bindings = _store(obj, isinstance(value, LocalizedText))
    if isinstance(value, LocalizedText):
        bindings[key] = value
        _objects[id(obj)] = obj
    else:
        bindings.pop(key, None)


def source_text(obj, method="setText", *slots, fallback=""):
    key = (method, *slots)
    if method == "setItemText" and isinstance(obj, QComboBox):
        index = QPersistentModelIndex(obj.model().index(slots[0], obj.modelColumn(), obj.rootModelIndex()))
        key = next((key for key in _store(obj)
                    if key[0] == "combo" and key[1].index == index), None)
    elif method in ("setTabText", "setTabToolTip") and isinstance(obj, QTabWidget):
        key = ("tab" if method == "setTabText" else "tabtip", obj.widget(slots[0]))
    return _store(obj).get(key, fallback)


def _display_value(value):
    if isinstance(value, LocalizedText):
        return value.render()
    if isinstance(value, (list, tuple)):
        return type(value)(_display_value(item) for item in value)
    return value


def ui_widget(cls, *args, **kwargs):
    obj = cls(*(_display_value(arg) for arg in args), **kwargs)
    if isinstance(obj, QTreeWidgetItem):
        values = next((arg for arg in args if isinstance(arg, (list, tuple))), ())
        for column, value in enumerate(values):
            _remember(obj, ("setText", column), value)
    elif args and isinstance(args[0], str):
        method = "setTitle" if isinstance(obj, QGroupBox) or cls.__name__ == "QMenu" else "setText"
        if hasattr(obj, method):
            _remember(obj, (method,), args[0])
    return obj


def _combo_item(combo, index, value):
    persistent = QPersistentModelIndex(combo.model().index(index, combo.modelColumn(), combo.rootModelIndex()))
    key = next((key for key in _store(combo)
                if key[0] == "combo" and key[1].index == persistent), ("combo", _ComboRow(persistent)))
    _remember(combo, key, value)


def ui_call(obj, method, *args, **kwargs):
    """Invoke a Qt method and retain only its translatable display arguments."""
    before = obj.count() if isinstance(obj, (QComboBox, QListWidget)) else 0
    # A queued worker signal may have been created before the language changed.
    # Render on delivery while retaining the original message for later switches.
    display_args = args if method == 'addRow' and not isinstance(obj, QFormLayout) else tuple(_display_value(arg) for arg in args)
    result = getattr(obj, method)(*display_args, **kwargs)
    if method == "clear" and isinstance(obj, (QComboBox, QListWidget, QTreeWidget, QTableWidget)):
        bindings = _store(obj)
        for key in list(bindings):
            if key[0] in ("combo", "tab", "tabtip"):
                bindings.pop(key, None)
    elif method in ("addItem", "insertItem") and isinstance(obj, QComboBox):
        offset = 1 if method == "insertItem" else 0
        text_offset = offset + (1 if len(args) > offset and isinstance(args[offset], QIcon) else 0)
        if len(args) > text_offset:
            _combo_item(obj, args[0] if method == "insertItem" else before, args[text_offset])
    elif method in ("addItems", "insertItems") and isinstance(obj, QComboBox):
        start, values = (args[0], args[1]) if method == "insertItems" else (before, args[0])
        for index, value in enumerate(values, start):
            _combo_item(obj, index, value)
    elif method == "setItemText" and isinstance(obj, QComboBox):
        _combo_item(obj, args[0], args[1])
    elif method in ("addTab", "insertTab", "setTabText") and isinstance(obj, QTabWidget):
        page = obj.widget(args[0]) if method == "setTabText" else args[0] if method == "addTab" else args[1]
        _remember(obj, ("tab", page), args[-1])
    elif method == "setTabToolTip" and isinstance(obj, QTabWidget):
        _remember(obj, ("tabtip", obj.widget(args[0])), args[1])
    elif method == "addRow" and isinstance(obj, QFormLayout) and args and isinstance(args[0], str):
        item = obj.itemAt(obj.rowCount()-1, QFormLayout.ItemRole.LabelRole)
        if item and item.widget():
            _remember(item.widget(), ("setText",), args[0])
    elif method == "setHeaderLabels" and isinstance(obj, QTreeWidget):
        for index, value in enumerate(args[0]):
            _remember(obj.headerItem(), ("setText", index), value)
    elif method in ("setHorizontalHeaderLabels", "setVerticalHeaderLabels") and isinstance(obj, QTableWidget):
        getter = obj.horizontalHeaderItem if method == "setHorizontalHeaderLabels" else obj.verticalHeaderItem
        for index, value in enumerate(args[0]):
            item = getter(index)
            if item is not None:
                _remember(item, ("setText",), value)
    elif method in ("addItem", "addItems") and isinstance(obj, QListWidget):
        values = args[0] if method == "addItems" else [args[0]]
        for index, value in enumerate(values, before):
            if isinstance(value, str):
                _remember(obj.item(index), ("setText",), value)
    elif method == "addAction" and result is not None:
        value = next((arg for arg in args if isinstance(arg, LocalizedText)), None)
        if value is not None:
            _remember(result, ("setText",), value)
    elif method.startswith("set") and args:
        if method in ("setText", "setToolTip", "setStatusTip", "setWhatsThis") and len(args) == 2:
            key = (method, args[0])
        else:
            key = (method,)
        _remember(obj, key, args[-1])
    return result


def ui_slot(obj, method):
    def receive(*values):
        # Qt's clicked(bool) may target a method that takes no arguments.
        ui_call(obj, method, *(() if method == "clear" else values))
    return receive


def retranslate_widgets():
    """Update display properties in place, with all input signals blocked."""
    widgets = QApplication.allWidgets()
    blockers = [QSignalBlocker(widget) for widget in widgets
                if isinstance(widget, (QComboBox, QTreeWidget, QTableWidget))]
    edits = [(widget, widget.currentText(), widget.lineEdit().cursorPosition())
             for widget in widgets if isinstance(widget, QComboBox) and widget.isEditable()
             and widget.currentText() != widget.itemText(widget.currentIndex())]
    try:
        objects = list(_objects.values()) + list(widgets)
        for widget in widgets:
            objects.extend(widget.actions())
            if isinstance(widget, QTreeWidget):
                objects.append(widget.headerItem())
                iterator = QTreeWidgetItemIterator(widget)
                while iterator.value():
                    objects.append(iterator.value())
                    iterator += 1
            elif isinstance(widget, QTableWidget):
                objects.extend(widget.horizontalHeaderItem(i) for i in range(widget.columnCount()))
                objects.extend(widget.verticalHeaderItem(i) for i in range(widget.rowCount()))
                objects.extend(widget.item(row, col) for row in range(widget.rowCount()) for col in range(widget.columnCount()))
            elif isinstance(widget, QListWidget):
                objects.extend(widget.item(i) for i in range(widget.count()))
        seen = set()
        for obj in objects:
            if obj is None or id(obj) in seen:
                continue
            seen.add(id(obj))
            if sip.isdeleted(obj):
                continue
            for key, value in list(_store(obj).items()):
                text = value.render()
                if key[0] == "combo":
                    index = key[1].index
                    if index.isValid():
                        obj.setItemText(index.row(), text)
                elif key[0] in ("tab", "tabtip"):
                    if sip.isdeleted(key[1]):
                        continue
                    index = obj.indexOf(key[1])
                    if index >= 0:
                        (obj.setTabText if key[0] == "tab" else obj.setTabToolTip)(index, text)
                else:
                    getattr(obj, key[0])(*key[1:], text)
        for combo, value, cursor in edits:
            combo.setEditText(value)
            combo.lineEdit().setCursorPosition(cursor)
    finally:
        del blockers
