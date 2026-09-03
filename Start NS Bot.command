#!/bin/bash
# Double-click me in Finder.
#
# macOS runs a .command file in Terminal, which is the whole trick - no build
# step, no packaging, and the script stays readable by whoever has to fix it.
#
# THE WINDOW MUST NOT VANISH. A double-clicked script that fails and closes
# instantly is worse than no launcher at all: there is nothing to read and no
# way to tell a crash from a clean exit. The trap below holds the window open
# on EVERY exit path, including a failure before Python starts.
trap 'echo; echo "──────────────────────────────────────────"; \
      echo "Press Return to close this window."; read -r _' EXIT

# Finder starts scripts from an arbitrary directory, so never assume the cwd.
cd "$(dirname "${BASH_SOURCE[0]}")" || {
    echo "Could not find the bot's folder. Move this file back beside engine/."
    exit 1
}

echo "NS Bot"
echo "  folder: $(pwd)"
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is not installed."
    echo
    echo "Install it from https://www.python.org/downloads/ (or 'brew install"
    echo "python3'), then double-click this file again."
    exit 1
fi

# FIRST RUN ONLY. Both steps are skipped once they have been done, so a normal
# start is not slowed down by checking things that cannot have changed.
if [ ! -x .venv/bin/python ]; then
    echo "First run: creating the virtual environment (a few seconds)..."
    python3 -m venv .venv || { echo "Could not create .venv"; exit 1; }
fi

if ! .venv/bin/python -c "import cv2" >/dev/null 2>&1; then
    echo "First run: installing the one dependency (opencv)..."
    .venv/bin/pip install --quiet -r requirements.txt || {
        echo "Could not install the dependency. Are you online?"
        exit 1
    }
fi

mkdir -p run

# No --attach. `browser.launch(reuse=True)` already attaches to a browser that
# is serving CDP and only starts one when nothing is, so the launcher does not
# have to guess which case it is in - and guessing wrong is how an operator
# ends up staring at "no page target after 40s".
#
# `tee` keeps the log on screen AND in run/app.log. That file matters: it is the
# only record of a session, and reading a stale one has produced a wrong
# diagnosis here before.
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
