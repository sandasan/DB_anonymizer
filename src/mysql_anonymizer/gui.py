"""Public desktop GUI entry point."""

from .qt_gui import AnonymizerWindow, main

__all__ = ["AnonymizerWindow", "main"]

if __name__ == "__main__":
    main()
