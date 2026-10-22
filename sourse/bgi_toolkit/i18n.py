"""Small, dependency-free message catalogs shared by the GUI and processors.

Chinese source messages are stable message IDs. Catalogs contain display text only;
file names, enum values, script text and JSON field names are never translated.
"""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
import json
import os
from string import Formatter

LANGUAGES = ("auto", "zh_CN", "en")
_language = os.environ.get("BGI_TOOLKIT_LANGUAGE", "zh_CN")


def resolve_language(preference: str, system_languages=()) -> str:
    """Use the primary OS UI language, with English for unsupported languages."""
    if preference in ("zh_CN", "en"):
        return preference
    if isinstance(system_languages, str):
        system_languages = [system_languages]
    primary = next(iter(system_languages), "en")
    return "zh_CN" if str(primary).replace("_", "-").lower().split("-")[0] == "zh" else "en"


def format_fields(value: str) -> list[str]:
    return sorted(field for _, field, _, _ in Formatter().parse(value) if field is not None)


@lru_cache(maxsize=8)
def load_catalog(language: str) -> dict[str, str]:
    if language == "zh_CN":
        return {}
    try:
        document = json.loads(files("bgi_toolkit").joinpath("locales", language + ".json").read_text(encoding="utf-8"))
        messages = document["messages"]
        if not isinstance(messages, dict):
            return {}
        valid = {}
        for source, translated in messages.items():
            if isinstance(source, str) and isinstance(translated, str) and translated:
                try:
                    if format_fields(source) == format_fields(translated):
                        valid[source] = translated
                except ValueError:
                    continue
        return valid
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def set_language(language: str) -> str:
    global _language
    _language = language if language in ("zh_CN", "en") else "zh_CN"
    load_catalog(_language)
    os.environ["BGI_TOOLKIT_LANGUAGE"] = _language
    return _language


def get_language() -> str:
    return _language


def _render_value(value):
    return value.render() if isinstance(value, LocalizedText) else value


class LocalizedText(str):
    """A normal string carrying its source message for live UI retranslation."""
    def __new__(cls, source="", args=(), kwargs=None, *, parts=None, transforms=()):
        kwargs = kwargs or {}
        if parts is None:
            value = load_catalog(_language).get(source, source)
            if args or kwargs:
                value = value.format(*(_render_value(v) for v in args),
                                     **{k: _render_value(v) for k, v in kwargs.items()})
        else:
            value = "".join(str(_render_value(part)) for part in parts)
        for transform in transforms:
            value = transform(value)
        instance = super().__new__(cls, value)
        instance.source, instance.args, instance.kwargs = source, args, kwargs
        instance.parts, instance.transforms = parts, transforms
        return instance

    def render(self):
        return str(type(self)(self.source, self.args, self.kwargs,
                              parts=self.parts, transforms=self.transforms))

    def transformed(self, function):
        return type(self)(self.source, self.args, self.kwargs, parts=self.parts,
                          transforms=(*self.transforms, function))

    def __add__(self, other):
        if isinstance(other, str):
            return type(self)(parts=(self, other))
        return NotImplemented

    def __radd__(self, other):
        if isinstance(other, str):
            return type(self)(parts=(other, self))
        return NotImplemented

    def __reduce__(self):
        # Process results and exported JSON contain ordinary rendered strings.
        return str, (str(self),)


def tr(source: str, *args, **kwargs) -> str:
    """Translate display text and retain its source for hot switching."""
    if isinstance(source, LocalizedText) and not args and not kwargs:
        return LocalizedText(source.source, source.args, source.kwargs,
                             parts=source.parts, transforms=source.transforms)
    return LocalizedText(source, args, kwargs)
