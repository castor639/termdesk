# termdesk

A computer inside your terminal.

![An agent in a kitty split launches termdesk, opens a terminal on the remote desktop, starts a browser and loads the termdesk site](assets/agent-demo.gif)

The clip runs at 1.75x speed. Watch the [full, unedited run](https://termdesk.warpfield.me/agent-demo.mp4) on the site.

[Website](https://termdesk.warpfield.me) · Part of [Warpfield](https://warpfield.me)

## What it does

termdesk draws a real desktop inside your terminal pane. You use it with your own mouse and keyboard. A coding agent can use it too, from any shell, and you watch every click it makes.

It talks to the desktop over VNC, a standard protocol for viewing and controlling a remote screen. Point it at any VNC server, or start a throwaway Linux desktop in Docker with one command. In that throwaway desktop, your agent reads the screen as a numbered list of buttons and fields and clicks by number.

## Install

```bash
curl -fsSL https://termdesk.warpfield.me/install | bash
```

The script installs [uv](https://docs.astral.sh/uv/) if you do not have it, then installs termdesk from the wheel on the site. If `~/.claude` exists, it also adds the Claude Code skill. Run it again to upgrade.

You need:

- macOS or Linux.
- A terminal that speaks the kitty graphics protocol: kitty, Ghostty or WezTerm.
- Python 3.9 or newer.
- Docker, for sandboxes. Plain `termdesk host:port` works without it.

The install takes about 35 MB, most of it numpy and Pillow.

## Quick start

Start a sandbox and look at it:

```bash
termdesk sandbox up --name work     # XFCE + Firefox desktop in Docker; the first run downloads the image
termdesk work                       # in a kitty, Ghostty or WezTerm pane: the desktop appears
```

Ctrl+Q closes the pane. Every other key and mouse event goes to the desktop.

From a second shell, drive the same screen:

```bash
termdesk action state                              # the screen as numbered elements
# [3] push button "Web Browser" @640,752 48x48
termdesk action click 3                            # Firefox opens
termdesk action wait-idle                          # wait until the screen stops changing
termdesk action state                              # what changed since the last state
# + [23] entry "Search or enter address" @252,78 780x28 [editable]
termdesk action set-value 23 https://example.org   # click element 23, select all, type
termdesk action key Return
termdesk action click 640 400                      # or click by desktop pixel
termdesk action done                               # clear the AGENT ACTING badge
```

Element numbers come from your own `state` output. On a fresh sandbox they match the ones above.

Clean up when you finish:

```bash
termdesk sandbox down work          # or: termdesk sandbox down --all
```

To view a machine you already have, pass its address. The port defaults to 5900:

```bash
termdesk myhost:5901
```

termdesk does not support VNC passwords yet. Run the server without one and reach it through an ssh tunnel:

```bash
ssh -L 5900:localhost:5900 user@box   # in one pane
termdesk localhost                     # in another
```

## For agents

Every session opens a control socket. `termdesk action` sends commands to it, so any agent that can run shell commands can use the desktop. While the agent acts, the pane shows an AGENT ACTING badge, and you can take the mouse back at any time.

```text
state [--full]                 accessibility tree as numbered elements (diff by default)
screenshot [PATH] [--scale S]  save a PNG
click INDEX | click X Y        [--right | --middle] [--double]
set-value INDEX TEXT           click, select all, type
type TEXT | key COMBO          e.g. key ctrl+l, key Return
scroll INDEX DY | scroll X Y DY [DX]
drag X1 Y1 X2 Y2
wait MS | wait-idle [--idle MS] [--timeout MS]
record start [PATH] [--fps N] | record stop
info | done
```

The accessibility tree is the list of on-screen elements that apps publish for screen readers. Each element comes back with an index, role, name, value and pixel box. The tree works inside a termdesk sandbox. On other VNC hosts, use `screenshot` and pixel coordinates.

Add `--name SESSION` when more than one session runs (`termdesk ls` lists them), and `--json` for machine-readable output. For a session with no pane, on a server or in CI, run:

```bash
termdesk work --headless --daemon --name work
```

### Claude Code skill

```bash
termdesk setup
```

This copies the skill to `~/.claude/skills/termdesk/SKILL.md`. The skill gives the agent the full command reference, an observe, act, verify loop, and a confirmation policy for risky steps such as payments or passwords. The source lives in [termdesk/skill/SKILL.md](termdesk/skill/SKILL.md). The repo also ships a Claude Code plugin manifest in [.claude-plugin/plugin.json](.claude-plugin/plugin.json).

### Sandboxes

```bash
termdesk sandbox up --name work --geometry 1280x800 --memory 2g --cpus 2
termdesk sandbox exec work -- firefox https://example.com   # launch an app
termdesk sandbox snapshot work clean                        # save it as an image
termdesk sandbox restore clean --name work2                 # start a copy
termdesk sandbox ls
```

Each sandbox is one container with a memory and CPU cap. Its VNC port listens on 127.0.0.1 alone, so other machines cannot reach it.

## How it works

- **Frames.** termdesk reads the screen over VNC and asks for ZRLE, a compressed encoding. It redraws the rectangles that changed with the kitty graphics protocol. A full 1280x800 frame is about 60 KB, and a blinking cursor is about 80 bytes. On the same machine, pixels pass through shared memory.
- **Input.** Keys, modifiers, key repeat, mouse buttons and the wheel go back to the desktop as VNC events.
- **Semantics.** The sandbox image runs XFCE, Firefox and the AT-SPI accessibility bus. `state` runs a small script inside the container that dumps the tree.
- **Control.** Each session listens on a Unix socket under `~/.local/state/termdesk`. `termdesk action` writes one JSON line and reads one back.

The status line under the desktop shows fps, bytes in and out, and the transport in use.

## Repo layout

- `termdesk/`: the Python package. `rfb.py` speaks VNC, `gfx.py` draws with kitty graphics, `term.py` reads terminal input, `session.py` runs the loop and control socket, `cli.py` is the entry point, `sandbox.py` wraps Docker, `record.py` writes GIF and MP4.
- `termdesk/skill/SKILL.md`: the agent skill. `skill/SKILL.md` is a copy for the plugin.
- `desktop/`: files copied into the sandbox image. `start.sh` boots the desktop, `a11y_dump.py` prints the accessibility tree.
- `Dockerfile`, `docker-compose.yml`: the XFCE + Firefox desktop image. `tiny.Dockerfile` builds a small xterm desktop for protocol tests.
- `scripts/`: the installer and the script that records the demo video.
- `site/`: the website, installer and wheel, served on Vercel.

## Prior art

[desktui](https://github.com/mishushakov/desktui) drew a VNC desktop with kitty graphics first. [sshdesk](https://github.com/rylena/sshdesk) added an agent CLI over ssh. [terminal-browser](https://terminal-browser.com) did the same for a browser and got this project started.

## License

MIT. See [LICENSE](LICENSE).
