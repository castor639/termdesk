# termdesk, for agents working in this repo

- `termdesk/` is the package. `rfb.py` speaks VNC, `gfx.py` speaks the kitty graphics protocol, `term.py` decodes terminal input, `session.py` runs the loop and the control socket, `cli.py` is the entry point, `sandbox.py` wraps docker, `record.py` writes GIF/MP4.
- `termdesk/skill/SKILL.md` is the canonical agent skill. `skill/SKILL.md` at the top level is a copy for the plugin; keep them identical.
- `desktop/` is copied into the sandbox image: `start.sh` boots Xvnc + XFCE + the AT-SPI bus, `a11y_dump.py` prints the accessibility tree.
- `site/` deploys to Vercel as-is: landing page, `install` script (text/plain), `dl/` wheels. Rebuild the wheel into `site/dl` with `uv build -o site/dl` and delete the `.gitignore` uv drops there.
- `scripts/install` and `site/install` must stay identical.
- Test without a terminal: `python3 -m termdesk <target> --headless --daemon --name t`, then `python3 -m termdesk action ...`. The tiny x11vnc image (`tiny.Dockerfile`) is enough for protocol tests; the full image is needed for `state`.
- Run `python3 -m termdesk sandbox down --all` and kill headless sessions when done.
- No em dashes in prose or code comments. Keep comments rare.
