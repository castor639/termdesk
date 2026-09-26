FROM debian:bookworm-slim AS home
COPY desktop/ /home/guest/
RUN rm -rf /home/guest/system && chmod +x /home/guest/start.sh

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
    tigervnc-standalone-server tigervnc-common \
    xfce4 xfce4-terminal xfce4-whiskermenu-plugin thunar mousepad ristretto \
    firefox-esr fonts-dejavu fonts-noto-color-emoji fonts-wqy-microhei \
    libreoffice-calc libreoffice-writer libreoffice-gtk3 python3-uno \
    dbus-x11 at-spi2-core python3-pyatspi python3-gi xclip \
    sudo procps curl ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Quiet first runs. Debian links /usr/lib/firefox-esr/distribution and
# /usr/lib/libreoffice/share/registry to these directories.
COPY desktop/system/policies.json /usr/share/firefox-esr/distribution/policies.json
COPY desktop/system/termdesk.js /etc/firefox-esr/termdesk.js
COPY desktop/system/termdesk.xcd /etc/libreoffice/registry/termdesk.xcd

RUN useradd -m -s /bin/bash -G sudo guest && echo 'guest ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/guest
COPY --from=home --chown=guest:guest /home/guest/ /home/guest/

USER guest
WORKDIR /home/guest
ENV DISPLAY=:1 GEOMETRY=1280x800 MOZ_CRASHREPORTER_DISABLE=1
# bump with IMAGE_VERSION in termdesk/sandbox.py when desktop/ changes what the host code relies on
LABEL termdesk.image="2"
EXPOSE 5900
CMD ["/home/guest/start.sh"]
