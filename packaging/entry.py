"""Entry point for the frozen (PyInstaller) OpenBI executable."""

import multiprocessing

from openbi.cli import app

if __name__ == "__main__":
    multiprocessing.freeze_support()
    app(prog_name="openbi")
