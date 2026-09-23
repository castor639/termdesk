FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc fluxbox xterm x11-apps fonts-dejavu-core && apt-get clean
ENV DISPLAY=:1
CMD Xvfb :1 -screen 0 1024x768x24 & sleep 1; fluxbox & xterm -geometry 90x30+40+40 -fa DejaVuSansMono -fs 12 & xeyes -geometry 200x150+700+500 & exec x11vnc -display :1 -forever -shared -nopw -rfbport 5900
