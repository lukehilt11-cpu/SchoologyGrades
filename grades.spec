# -*- mode: python ; coding: utf-8 -*-
# Don't run this directly; use build.bat (Windows) or build.sh (macOS) in this folder.
#   Windows -> 1. dist/SchoologyGrades/   one folder holding SchoologyGrades.exe and
#                 SchoologySetup.exe, sharing one runtime (fast launches; the installer packs this)
#              2. dist/SchoologyGrades-portable.exe and dist/SchoologySetup-portable.exe
#                 single-file programs (build.bat copies them next to "source")
#   macOS   -> SchoologyGrades.app + SchoologySetup.app (build.sh copies them next to
#              "source" and packs them into a .dmg)

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

IS_MAC = sys.platform == "darwin"
ICON_DIR = os.path.join(SPECPATH, "icons")        # made by icons/make_icons.py
VERSION_FILE = os.path.join(SPECPATH, "VERSION")  # one version number for everything
REPO_FILE = os.path.join(SPECPATH, "REPO")        # added by the GitHub build; switches on the update notice

# Selenium ships selenium-manager as a data file; it finds and downloads the
# matching chromedriver at runtime, so it must be bundled.
selenium_datas = collect_data_files("selenium")

# Newer Selenium loads browser classes lazily (selenium.webdriver.chrome.options
# and friends), which PyInstaller can't see on its own. Bundle every submodule.
selenium_modules = collect_submodules("selenium")

# (script, program name, icon name)
APPS = (
    ("grades.pyw", "SchoologyGrades", "grades"),
    ("setup.py", "SchoologySetup", "setup"),
)


def analyse(script, icon_name):
    # The icon files are bundled so the running program can show its icon in its
    # window (see set_window_icon in sgcommon.py); VERSION feeds the window title.
    datas = selenium_datas + [
        (os.path.join(ICON_DIR, icon_name + ".ico"), "icons"),
        (os.path.join(ICON_DIR, icon_name + "_256.png"), "icons"),
        (os.path.join(ICON_DIR, "grades_64.png"), "icons"),  # the report's tab icon
        (VERSION_FILE, "."),
    ] + ([(REPO_FILE, ".")] if os.path.exists(REPO_FILE) else [])
    return Analysis(
        [script],
        pathex=[],
        binaries=[],
        datas=datas,
        hiddenimports=selenium_modules,
        hookspath=[],
        runtime_hooks=[],
        excludes=[],
        noarchive=False,
    )


def make_exe(pyz, a, name, icon_name, onefile=False):
    # UPX stays off: packed programs trigger more antivirus warnings.
    icon = None if IS_MAC else os.path.join(ICON_DIR, icon_name + ".ico")
    if onefile:
        return EXE(
            pyz, a.scripts, a.binaries, a.datas, [],
            name=name, debug=False, strip=False, upx=False, console=False, icon=icon,
        )
    return EXE(
        pyz, a.scripts, [], exclude_binaries=True,
        name=name, debug=False, strip=False, upx=False, console=False, icon=icon,
    )


if IS_MAC:
    # Two double-clickable apps, each folder-style inside.
    for script, name, icon_name in APPS:
        a = analyse(script, icon_name)
        pyz = PYZ(a.pure)
        coll = COLLECT(make_exe(pyz, a, name, icon_name), a.binaries, a.datas,
                       strip=False, upx=False, name=name)
        BUNDLE(coll, name=name + ".app", icon=os.path.join(ICON_DIR, icon_name + ".icns"),
               bundle_identifier="local." + name.lower())
else:
    # Both programs go into ONE folder. Passing both analyses to a single COLLECT
    # merges their files (identical files are kept once), so they share one runtime.
    # Each program is also built as a single file, from the same analysis.
    parts = []
    for script, name, icon_name in APPS:
        a = analyse(script, icon_name)
        pyz = PYZ(a.pure)
        parts += [make_exe(pyz, a, name, icon_name), a.binaries, a.datas]
        make_exe(pyz, a, name + "-portable", icon_name, onefile=True)
    COLLECT(*parts, strip=False, upx=False, name="SchoologyGrades")
