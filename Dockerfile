FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
    tigervnc-standalone-server tigervnc-common \
    xfce4 xfce4-terminal xfce4-whiskermenu-plugin thunar mousepad ristretto \
    firefox-esr fonts-dejavu fonts-noto-color-emoji \
    dbus-x11 at-spi2-core python3-pyatspi python3-gi \
    sudo procps curl ca-certificates \
    && apt-get clean

RUN useradd -m -s /bin/bash -G sudo guest && echo 'guest ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/guest
COPY --chown=guest:guest desktop/ /home/guest/
RUN chmod +x /home/guest/start.sh

USER guest
WORKDIR /home/guest
ENV DISPLAY=:1 GEOMETRY=1280x800
EXPOSE 5900
CMD ["/home/guest/start.sh"]
