#!/bin/bash
# macOS build:   bash build.sh            the apps next to "source" AND a .dmg in ../releases
#                bash build.sh force      rebuild even if nothing has changed
#                bash build.sh dev        the apps only (skips the .dmg)
#                bash build.sh run        when done, open the disk image (or, with dev, the app)
set -u
cd "$(dirname "$0")" || exit 1

FORCE=0
DEV=0
RUN=0
for arg in "$@"; do
    case "$arg" in
        force) FORCE=1 ;;
        dev) DEV=1 ;;
        run) RUN=1 ;;
    esac
done

VERSION="$(tr -d '[:space:]' < VERSION 2>/dev/null)"
VERSION="${VERSION:-1.0}"

if [ "$(uname)" = "Darwin" ]; then
    CACHE="$HOME/Library/Caches/SchoologyGradesBuild"
    TARGET="../releases/SchoologyGrades-$VERSION.dmg"
else
    CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/SchoologyGradesBuild"
    TARGET="../releases/SchoologyGrades-$VERSION-linux.tar.gz"
fi
WORK="$CACHE/work"
LOG="$CACHE/build.log"
mkdir -p "$CACHE" "$WORK"
: > "$LOG"

fail() {
    echo
    echo "Build failed. Last lines of the log:"
    echo "------------------------------------------------------------"
    tail -n 25 "$LOG"
    echo "------------------------------------------------------------"
    echo "Full log: $LOG"
    exit 1
}
# Run a command with all of its output going to the log; show it only on failure.
run() { "$@" >> "$LOG" 2>&1 || fail; }

# ---- nothing changed since the last build?
LOOSE_OK=1
if [ "$(uname)" = "Darwin" ]; then
    [ -d ../SchoologyGrades.app ] && [ -d ../SchoologySetup.app ] || LOOSE_OK=0
fi
if [ $FORCE -eq 0 ] && [ $DEV -eq 0 ] && [ $LOOSE_OK -eq 1 ] && [ -f "$TARGET" ]; then
    CHANGED="$(find . -type f -newer "$TARGET" -not -path '*/__pycache__/*' | head -n 1)"
    if [ -z "$CHANGED" ]; then
        echo "Nothing has changed since the last build:"
        echo "  ${TARGET#../}"
        echo 'Use "bash build.sh force" to rebuild anyway.'
        if [ $RUN -eq 1 ] && [ "$(uname)" = "Darwin" ]; then open "$TARGET"; fi
        exit 0
    fi
fi

echo "Schoology Grade Checker build $VERSION"

# ---- 1/6 build tools (a private environment that is reused between builds)
PY="$CACHE/venv/bin/python"
TOOLS="pyinstaller selenium>=4.6 pillow numpy"
CHECK="import PyInstaller, selenium, PIL, numpy"
if [ "$(uname)" = "Darwin" ]; then
    TOOLS="$TOOLS dmgbuild"
    CHECK="$CHECK, dmgbuild"
fi
if [ ! -x "$PY" ]; then
    echo "[1/6] Setting up build tools (first time only)..."
    run python3 -m venv "$CACHE/venv"
    run "$PY" -m pip install --quiet --disable-pip-version-check $TOOLS
elif ! "$PY" -c "$CHECK" >/dev/null 2>&1; then
    echo "[1/6] Installing build tools..."
    run "$PY" -m pip install --quiet --disable-pip-version-check $TOOLS
else
    echo "[1/6] Build tools ready"
fi

# ---- 2/6 icons (only redrawn when missing or older than make_icons.py)
ICONS_OK=1
for f in grades.icns grades.ico grades_256.png grades_64.png setup.icns setup.ico setup_256.png wizard_large.bmp wizard_small.bmp dmg_background.png; do
    if [ ! -f "icons/$f" ] || [ "make_icons.py" -nt "icons/$f" ]; then
        ICONS_OK=0
    fi
done
[ $FORCE -eq 1 ] && ICONS_OK=0
if [ $ICONS_OK -eq 1 ]; then
    echo "[2/6] Icons up to date"
else
    echo "[2/6] Drawing icons..."
    run "$PY" make_icons.py
fi
for f in grades.icns grades_256.png grades_64.png setup.icns setup_256.png; do
    if [ ! -f "icons/$f" ]; then
        echo "The icon file icons/$f is missing, and drawing the icons didn't create it."
        echo "Is make_icons.py in the same folder as build.sh?"
        fail
    fi
done

# ---- 3/6 the programs
echo "[3/6] Building programs (a minute or two)..."
CLEAN=""
[ $FORCE -eq 1 ] && CLEAN="--clean"
run "$PY" -m PyInstaller --noconfirm $CLEAN --log-level WARN \
    --distpath "$WORK/dist" --workpath "$WORK/build" grades.spec

# ---- 4/6 the apps, next to "source" (a data folder appears beside them when they run)
if [ "$(uname)" = "Darwin" ]; then
    echo "[4/6] Updating SchoologyGrades.app and SchoologySetup.app..."
    rm -rf ../SchoologyGrades.app ../SchoologySetup.app
    run cp -R "$WORK/dist/SchoologyGrades.app" "$WORK/dist/SchoologySetup.app" ..
else
    echo "[4/6] (Linux builds only make a .tar.gz)"
fi

# ---- 5/6 disk image
RESULT=""
if [ $DEV -eq 1 ]; then
    echo "[5/6] Skipping the disk image (dev build)"
elif [ "$(uname)" = "Darwin" ]; then
    echo "[5/6] Creating the disk image..."
    mkdir -p ../releases
    rm -f "$TARGET"
    # A styled window (background and arrow) via dmgbuild; if that fails, a plain disk image.
    if ! "$PY" -m dmgbuild -s dmg_settings.py -D app_dir="$WORK/dist" -D icons_dir="$PWD/icons" \
            "Schoology Grade Checker" "$TARGET" >> "$LOG" 2>&1; then
        echo "      (the styled disk image didn't work, making a plain one; see build.log)"
        STAGE="$WORK/dmg"
        rm -rf "$STAGE" "$TARGET"
        mkdir -p "$STAGE"
        run cp -R "$WORK/dist/SchoologyGrades.app" "$WORK/dist/SchoologySetup.app" "$STAGE/"
        run ln -s /Applications "$STAGE/Applications"
        run hdiutil create -volname "Schoology Grade Checker" -srcfolder "$STAGE" -ov -format UDZO "$TARGET"
    fi
    RESULT="${TARGET#../}"
else
    echo "[5/6] Packing the programs..."
    mkdir -p ../releases
    rm -f "$TARGET"
    run tar -czf "$TARGET" -C "$WORK/dist" SchoologyGrades
    RESULT="${TARGET#../}"
fi

# ---- 6/6 tidy up (the build cache stays so the next build is faster)
echo "[6/6] Cleaning up..."
rm -rf "$WORK/dist" "$WORK/dmg" __pycache__ build dist
echo
echo "Done."
if [ "$(uname)" = "Darwin" ]; then
    echo "  Apps:       $(cd .. && pwd)/SchoologyGrades.app and SchoologySetup.app"
fi
[ -n "$RESULT" ] && echo "  Disk image: $RESULT"

# "run": open what was just built so it can be tried right away
if [ $RUN -eq 1 ] && [ "$(uname)" = "Darwin" ]; then
    if [ $DEV -eq 1 ]; then open ../SchoologyGrades.app; else open "$TARGET"; fi
fi
true
