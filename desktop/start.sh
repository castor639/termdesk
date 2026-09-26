#!/bin/bash
# Boots a shared, password-less XFCE desktop served by TigerVNC on :5900,
# with the AT-SPI accessibility bus up so agents can read the UI tree.
set -e
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
Xvnc :1 -geometry "${GEOMETRY:-1280x800}" -depth 24 -rfbport 5900 \
    -SecurityTypes None -AlwaysShared -AcceptSetDesktopSize=0 \
    -desktop "termdesk" >/tmp/xvnc.log 2>&1 &
for _ in $(seq 50); do [ -e /tmp/.X11-unix/X1 ] && break; sleep 0.1; done
xset s off -dpms 2>/dev/null || true
export GTK_MODULES=gail:atk-bridge GNOME_ACCESSIBILITY=1 QT_ACCESSIBILITY=1 NO_AT_BRIDGE=0
eval "$(dbus-launch --sh-syntax)"
echo "$DBUS_SESSION_BUS_ADDRESS" > /tmp/dbus-address
/usr/libexec/at-spi-bus-launcher --launch-immediately &
# Keeps /tmp/termdesk-focus.json current; restarted if it dies
(while :; do python3 /home/guest/focus_track.py >>/tmp/focus_track.log 2>&1 || true; sleep 1; done) &
# Stay PID 1 so orphaned processes get reaped, and stop when the session ends
startxfce4 &
session=$!
trap 'kill -TERM $session 2>/dev/null' TERM INT HUP
while kill -0 $session 2>/dev/null; do wait $session || true; done
