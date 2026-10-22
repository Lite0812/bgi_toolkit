"""GUI startup language selection and Qt's standard dialog translations."""
from PyQt6.QtCore import QLibraryInfo, QLocale, QTranslator
from PyQt6.QtWidgets import QApplication
from ..i18n import LANGUAGES, resolve_language, set_language


def configure_language(settings):
    preference = settings.value("ui/language", "auto", str)
    if preference not in LANGUAGES:
        preference = "auto"
    resolved = resolve_language(preference, QLocale.system().uiLanguages())
    set_language(resolved)
    application = QApplication.instance()
    old = getattr(application, "_toolkit_qt_translator", None)
    if old is not None:
        application.removeTranslator(old)
        old.deleteLater()
    application._toolkit_qt_translator = None
    if resolved == "zh_CN":
        translator = QTranslator(application)
        if translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            application.installTranslator(translator)
            application._toolkit_qt_translator = translator
    QLocale.setDefault(QLocale("zh_CN" if resolved == "zh_CN" else "en_US"))
    return preference, resolved
