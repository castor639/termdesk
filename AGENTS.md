# termdesk, for agents working in this repo

- `termdesk/` is the package. `rfb.py` speaks VNC, `gfx.py` speaks the kitty graphics protocol, `term.py` decodes terminal input, `session.py` runs the loop and the control socket, `cli.py` is the entry point, `mcp.py` is the stdio MCP server, `sandbox.py` wraps docker, `record.py` writes GIF/MP4, `uninstall.py` removes everything termdesk created.
- `termdesk/skill/SKILL.md` is the canonical agent skill; `termdesk guide` prints it for other agents. `skill/SKILL.md` at the top level is a copy for the plugin; keep them identical.
- `desktop/` is copied into the sandbox image: `start.sh` boots Xvnc + XFCE + the AT-SPI bus and stays PID 1, `a11y_dump.py` prints the accessibility tree, `focus_track.py` keeps `/tmp/termdesk-focus.json` current for typing checks. `desktop/system/` holds Firefox and LibreOffice defaults that the Dockerfile copies to system paths, not into the home directory. `.dockerignore` sends only `desktop/` to the build.
- `site/` deploys to Vercel as-is: landing page, `install` script (text/plain), `dl/` wheels. Rebuild the wheel into `site/dl` with `uv build -o site/dl` and delete the `.gitignore` uv drops there.
- `scripts/install` and `site/install` must stay identical.
- Test without a terminal: `python3 -m termdesk <target> --headless --daemon --name t`, then `python3 -m termdesk action ...`. The tiny x11vnc image (`tiny.Dockerfile`) is enough for protocol tests; the full image is needed for `state`.
- Unit tests: `python3 -m unittest discover -s tests`. They need no docker; keep them passing on Python 3.9.
- Run `python3 -m termdesk sandbox down --all` and kill headless sessions when done.
- No em dashes in prose or code comments. Keep comments rare.
