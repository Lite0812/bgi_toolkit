"""Desktop entry point for the unified modern interface."""
from .modern import MainWindow, ToolNavigation, main
from .ui_common import THEMES, _SYMBOL_PATTERN, _plain_ui_text, _sanitize_widget_text

if __name__ == "__main__":
    raise SystemExit(main())
