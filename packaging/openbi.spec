# PyInstaller spec: builds a single-file `openbi` executable (Windows .exe,
# Linux and macOS binaries). Run from the repository root:
#   pyinstaller packaging/openbi.spec
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

hiddenimports = (
    collect_submodules("openbi")
    + collect_submodules("uvicorn")  # protocol/loop implementations are imported by name
    + collect_submodules("sqlglot.dialects")
    + collect_submodules("apscheduler")
)
datas = (
    collect_data_files("openbi")  # web/static: HTML, JS, CSS, vendored libraries
    + copy_metadata("apscheduler")  # executors/job stores are resolved via entry points
    + copy_metadata("openbi")
)

a = Analysis(
    ["entry.py"],
    pathex=[],
    hiddenimports=hiddenimports,
    datas=datas,
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="openbi",
    console=True,
    upx=False,
    strip=False,
)
