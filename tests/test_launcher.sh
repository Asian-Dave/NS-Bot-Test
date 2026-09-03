#!/bin/bash
# Exercise the macOS launcher's BRANCHES, not just its happy path.
#
# WHY A HARNESS AND NOT JUST DOUBLE-CLICKING IT. Most of what a launcher has to
# get right only happens on a machine that is broken in a particular way: no
# Python, a half-made virtual environment, an offline pip, a bot already
# running. None of those can be produced on a working setup, and the first run
# happens exactly once - after which the interesting branch is dead code that
# nobody looks at again until it fails for a stranger.
#
# So the environment is faked instead. A stub `python3` early on PATH records
# what it was asked to do and fabricates a `.venv` whose python is another
# stub, which lets the whole first-run path run with no download, no real
# interpreter and no risk to the working checkout - the sandbox is a temp
# directory containing only the files the launcher touches.
#
# The .bat and the .desktop cannot be tested from here at all. This at least
# pins the logic they were written to mirror.
#
# Usage:  tests/test_launcher.sh
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0
FAIL=0

check() {   # check <description> <condition-as-string>
    if eval "$2"; then
        echo "  PASS  $1"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $1"
        FAIL=$((FAIL + 1))
    fi
}

# ---------------------------------------------------------------- fixtures
mk_sandbox() {
    export SANDBOX="$WORK/sb"
    rm -rf "$SANDBOX"
    mkdir -p "$SANDBOX/engine"
    cp "$REPO/Start NS Bot.command" "$SANDBOX/"
    cp "$REPO/requirements.txt" "$SANDBOX/"
    echo 'print("app")' > "$SANDBOX/engine/app.py"
}

mk_stub_python() {
    local bin="$WORK/bin"
    mkdir -p "$bin"
    cat > "$bin/python3" <<'SHIM'
#!/bin/bash
echo "python3 $*" >> "$SANDBOX/calls.log"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
    [ -f "$SANDBOX/.venv_fails" ] && exit 1
    mkdir -p "$3/bin"
    cp "$STUB_BIN/venvpython" "$3/bin/python"
    cp "$STUB_BIN/venvpip" "$3/bin/pip"
    exit 0
fi
exit 0
SHIM
    cat > "$bin/venvpython" <<'SHIM'
#!/bin/bash
echo "venv/python $*" >> "$SANDBOX/calls.log"
if [ "${1:-}" = "-c" ] && [ "${2:-}" = "import cv2" ]; then
    [ -f "$SANDBOX/.cv2_ok" ] && exit 0 || exit 1
fi
if [ "${1:-}" = "engine/app.py" ]; then
    echo "(stub bot)"
    exit "${FAKE_APP_EXIT:-0}"
fi
exit 0
SHIM
    cat > "$bin/venvpip" <<'SHIM'
#!/bin/bash
echo "venv/pip $*" >> "$SANDBOX/calls.log"
[ -f "$SANDBOX/.pip_fails" ] && exit 1
touch "$SANDBOX/.cv2_ok"
exit 0
SHIM
    chmod +x "$bin"/*
    export STUB_BIN="$bin"
    export PATH="$bin:$PATH"
}

# The call log ACCUMULATES, so it has to be cleared between launches that
# share a sandbox - otherwise "did the second run rebuild the venv?" reads the
# FIRST run's entries and fails against a launcher doing exactly the right
# thing. (It did, first time out.)
reset_calls() { : > "$SANDBOX/calls.log"; }

launch() {  # runs the launcher in the sandbox, output into $OUT
    reset_calls
    OUT="$( cd "$SANDBOX" && ./"Start NS Bot.command" < /dev/null 2>&1 )"
    RC=$?
}

said()   { grep -qi -- "$2" <<<"$1"; }
called() { grep -q -- "$1" "$SANDBOX/calls.log" 2>/dev/null; }

mk_stub_python

# ------------------------------------------------------------------- cases
echo
echo "FIRST RUN - no .venv, cv2 absent"
mk_sandbox; launch
check "creates the virtual environment" 'called "python3 -m venv .venv"'
check "installs the dependency"         'called "venv/pip install"'
check "then starts the bot"             'called "venv/python engine/app.py"'
check "says so, in that order"          'said "$OUT" "First run: creating"'
check "reports no error"                '! said "$OUT" "exited with an error"'
check "creates run/ for the log"        '[ -d "$SANDBOX/run" ]'
check "and writes the log"              '[ -f "$SANDBOX/run/app.log" ]'

echo
echo "SECOND RUN - venv and cv2 already there"
launch
check "does not rebuild the venv"       '! called "python3 -m venv"'
check "does not reinstall"              '! called "venv/pip install"'
check "starts straight away"            'called "venv/python engine/app.py"'
check "and says nothing about a first run" '! said "$OUT" "First run"'

echo
echo "THE BOT EXITS WITH AN ERROR"
mk_sandbox; touch "$SANDBOX/.cv2_ok"
FAKE_APP_EXIT=3 launch
check "the error is reported"           'said "$OUT" "exited with an error"'
check "with the exit code"              'said "$OUT" "3"'
check "and the already-running hint"    'said "$OUT" "already running"'

echo
echo "NO PYTHON AT ALL"
mk_sandbox
reset_calls
OUT="$( cd "$SANDBOX" && PATH=/nonexistent ./"Start NS Bot.command" < /dev/null 2>&1 )"
check "says Python is missing"          'said "$OUT" "Python 3 is not installed"'
check "and where to get it"             'said "$OUT" "python.org"'
check "does not pretend to start"       '! said "$OUT" "Starting."'

echo
echo "THE INSTALL FAILS (offline)"
mk_sandbox; touch "$SANDBOX/.pip_fails"; launch
check "says the install failed"         'said "$OUT" "Could not install"'
check "asks about the network"          'said "$OUT" "online"'
check "does not start the bot anyway"   '! called "venv/python engine/app.py"'

echo
echo "THE VENV CANNOT BE MADE"
mk_sandbox; touch "$SANDBOX/.venv_fails"; launch
check "says it could not create .venv"  'said "$OUT" "Could not create"'
check "does not go on to install"       '! called "venv/pip install"'

echo
echo "THE LOG IS APPENDED, NEVER TRUNCATED"
# A second double-click while an instance is running would otherwise wipe the
# log of the one doing the work, before app.py even refuses to start.
mk_sandbox; touch "$SANDBOX/.cv2_ok"
printf 'line one\nline two\n' > "$SANDBOX/run_seed"
mkdir -p "$SANDBOX/run"; cp "$SANDBOX/run_seed" "$SANDBOX/run/app.log"
launch
check "the earlier log survives"        'grep -q "line one" "$SANDBOX/run/app.log"'
check "and the new run is added to it"  'grep -q "stub bot" "$SANDBOX/run/app.log"'

echo
echo "IT DOES NOT CARE WHERE IT IS STARTED FROM"
# Finder starts scripts from an arbitrary directory.
mk_sandbox; touch "$SANDBOX/.cv2_ok"
reset_calls
OUT="$( cd / && "$SANDBOX/Start NS Bot.command" < /dev/null 2>&1 )"
check "finds its own folder"            'said "$OUT" "folder: $SANDBOX"'
check "and runs from there"             'called "venv/python engine/app.py"'

echo
echo "─────────────────────────────────────────"
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
