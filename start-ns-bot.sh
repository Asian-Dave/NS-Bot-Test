#!/bin/bash
# Linux launcher. Double-click "NS Bot.desktop" rather than this file - most
# desktops will not run a bare .sh from a double-click, and the ones that offer
# to do it usually run it WITHOUT a terminal, so every message disappears.
#
# THE WINDOW MUST NOT VANISH. Same reasoning as the macOS launcher: a script
# that fails and closes instantly leaves nothing to read.
trap 'echo; echo "──────────────────────────────────────────"; \
      echo "Press Return to close this window."; read -r _' EXIT

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" || {
    echo "Could not find the bot's folder. Move this file back beside engine/."
    exit 1
}

echo "NS Bot"
echo "  folder: $(pwd)"
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is not installed. Install it with your package manager,"
    echo "for example:  sudo apt install python3 python3-venv"
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "First run: creating the virtual environment (a few seconds)..."
    python3 -m venv .venv || {
        echo "Could not create .venv."
        echo "On Debian/Ubuntu the venv module is a separate package:"
        echo "  sudo apt install python3-venv"
        exit 1
    }
fi

if ! .venv/bin/python -c "import cv2" >/dev/null 2>&1; then
    echo "First run: installing the one dependency (opencv)..."
    .venv/bin/pip install --quiet -r requirements.txt || {
        echo "Could not install the dependency. Are you online?"
        exit 1
    }
fi

mkdir -p run

# FIX THE .desktop FILE'S PATH FOR THE OPERATOR, once.
#
# A .desktop file is not run from the folder it lives in, so its Exec line
# cannot be relative - and telling someone who is "not comfortable using a
# script" to hand-edit a config file gives back the problem the launcher exists
# to remove. So do it here, where the correct absolute path is simply known.
if [ -f "NS Bot.desktop" ] && grep -q '__DIR__' "NS Bot.desktop"; then
    if sed -i.bak "s|__DIR__|$(pwd)|g" "NS Bot.desktop" 2>/dev/null; then
        rm -f "NS Bot.desktop.bak"
        chmod +x "NS Bot.desktop" 2>/dev/null
        echo "Set up 'NS Bot.desktop' for this folder - you can double-click"
        echo "that from now on (copy it to your Desktop if you like)."
        echo
    fi
fi

# No --attach: browser.launch(reuse=True) attaches to a browser already serving
# CDP and starts one only when nothing is, so the launcher never has to guess.
echo "Starting. Close this window (or press Quit in the panel) to stop."
echo
# APPEND, never truncate. A second double-click while an instance is already
# running would otherwise wipe that instance's log before app.py even gets as
# far as refusing to start - destroying the only record of the session that is
# actually doing the work. Appending also keeps the history across a restart,
# which is what makes a relog or a crash legible afterwards.
.venv/bin/python engine/app.py 2>&1 | tee -a run/app.log
status=${PIPESTATUS[0]}

echo
if [ "$status" -ne 0 ]; then
    echo "The bot exited with an error (code $status). The lines above say why."
    echo "If it says another window is already running, that instance still has"
    echo "the game - use its panel, or close it first."
fi
