#!/bin/bash
# The agent side of the demo: types each command, runs it for real, and opens termdesk in a split.
TARGET="${TARGET:-localhost:5900}"
URL="${URL:-http://localhost:8765}"
BROWSER="${BROWSER:-chromium}"
C=$'\e[38;5;245m'; A=$'\e[38;5;117m'; R=$'\e[0m'
say() { printf "%s# %s%s\n" "$C" "$1" "$R"; sleep 0.9; }
typeit() { printf "%s\$%s " "$A" "$R"; local s="$1"; for ((i=0;i<${#s};i++)); do printf "%s" "${s:i:1}"; sleep 0.03; done; printf "\n"; sleep 0.3; }
run() { typeit "$*"; eval "$*" >/dev/null; sleep 0.4; }
termdesk() { python3 -m termdesk "$@"; }

sleep 1.5
say "task: check the termdesk site in a real browser."
say "I need a computer. Launching termdesk."
typeit "termdesk $TARGET"
kitty @ launch --location=vsplit --keep-focus --title termdesk python3 -m termdesk "$TARGET" >/dev/null
kitty @ resize-window --match title:agent --axis horizontal --increment -26 >/dev/null 2>&1
sleep 3
say "desktop is up. Opening a terminal."
run "termdesk action click 567 775"
sleep 2
run "termdesk action wait-idle"
run "termdesk action type '$BROWSER $URL'"
run "termdesk action key Return"
sleep 5
run "termdesk action wait-idle --timeout 8000"
say "page loaded. Reading it."
run "termdesk action scroll 640 450 6"
sleep 1.5
run "termdesk action scroll 640 450 6"
sleep 1.5
run "termdesk action screenshot /tmp/lab.png"
say "done: the site renders fine."
run "termdesk action done"
sleep 4
touch "$DONE_FILE"
sleep 600
