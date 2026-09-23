#!/bin/bash
# Boots a shared, password-less XFCE desktop served by TigerVNC on :5900,
# with the AT-SPI accessibility bus up so agents can read the UI tree.
set -e
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
Xvnc :1 -geometry "${GEOMETRY:-1280x800}" -depth 24 -rfbport 5900 \
    -SecurityTypes None -AlwaysShared -AcceptSetDesktopSize=0 \
    -desktop "termdesk" >/tmp/xvnc.log 2>&1 &
for _ in $(seq 50); do [ -e /tmp/.X11-unix/X1 ] && break; sleep 0.1; done
export GTK_MODULES=gail:atk-bridge GNOME_ACCESSIBILITY=1 QT_ACCESSIBILITY=1 NO_AT_BRIDGE=0
eval "$(dbus-launch --sh-syntax)"
echo "$DBUS_SESSION_BUS_ADDRESS" > /tmp/dbus-address
/usr/libexec/at-spi-bus-launcher --launch-immediately &
exec startxfce4
