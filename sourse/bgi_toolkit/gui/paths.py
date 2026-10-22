"""Use one native path spelling for dropped, browsed, and generated GUI paths."""
from .localized_ui import ui_call, ui_widget, ui_slot, source_text
import os

from PyQt6.QtWidgets import QLineEdit


def display_path(value, *, multiple=False):
    value = str(value).strip()
    if not value:
        return ''
    if multiple:
        return ';'.join(display_path(part) for part in value.split(';') if part.strip())
    return os.path.normpath(value.strip('"'))


class PathLineEdit(QLineEdit):
    multiple_paths = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.editingFinished.connect(self._normalize_path)

    def setText(self, text):
        ui_call(super(), 'setText', display_path(text, multiple=self.multiple_paths))

    def _normalize_path(self):
        normalized = display_path(self.text(), multiple=self.multiple_paths)
        if normalized != self.text():
            ui_call(self, 'setText', normalized)
