"""X keysym tables shared by the terminal decoder and the action API."""

XK = {
    "esc": 0xFF1B, "ret": 0xFF0D, "tab": 0xFF09, "bs": 0xFF08, "ins": 0xFF63, "del": 0xFFFF,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "pgup": 0xFF55, "pgdn": 0xFF56, "end": 0xFF57, "home": 0xFF50, "menu": 0xFF67,
    "shift_l": 0xFFE1, "shift_r": 0xFFE2, "ctrl_l": 0xFFE3, "ctrl_r": 0xFFE4,
    "caps": 0xFFE5, "alt_l": 0xFFE9, "alt_r": 0xFFEA, "super_l": 0xFFEB, "super_r": 0xFFEC,
    "num": 0xFF7F, "scroll": 0xFF14, "print": 0xFF61, "pause": 0xFF13, "space": 0x20,
}
MODIFIER_SYMS = {XK[k] for k in ("shift_l", "shift_r", "ctrl_l", "ctrl_r", "alt_l", "alt_r", "super_l", "super_r")}

# names accepted by `termdesk action key`, e.g. ctrl+shift+t, Return, F5
KEY_NAMES = {
    "return": XK["ret"], "enter": XK["ret"], "tab": XK["tab"], "escape": XK["esc"], "esc": XK["esc"],
    "backspace": XK["bs"], "delete": XK["del"], "del": XK["del"], "insert": XK["ins"], "space": 0x20,
    "up": XK["up"], "down": XK["down"], "left": XK["left"], "right": XK["right"],
    "home": XK["home"], "end": XK["end"], "pageup": XK["pgup"], "pagedown": XK["pgdn"],
    "pgup": XK["pgup"], "pgdn": XK["pgdn"], "menu": XK["menu"], "print": XK["print"],
    "capslock": XK["caps"], "numlock": XK["num"],
    "shift": XK["shift_l"], "ctrl": XK["ctrl_l"], "control": XK["ctrl_l"], "alt": XK["alt_l"],
    "option": XK["alt_l"], "super": XK["super_l"], "meta": XK["super_l"], "cmd": XK["super_l"], "win": XK["super_l"],
}
for _i in range(1, 36):
    KEY_NAMES[f"f{_i}"] = 0xFFBE + _i - 1
MOD_NAMES = {"shift", "ctrl", "control", "alt", "option", "super", "meta", "cmd", "win"}

# kitty CSI-u functional key codes
CSIU = {
    27: XK["esc"], 13: XK["ret"], 9: XK["tab"], 127: XK["bs"],
    57358: XK["caps"], 57359: XK["scroll"], 57360: XK["num"], 57361: XK["print"],
    57362: XK["pause"], 57363: XK["menu"],
    57441: XK["shift_l"], 57442: XK["ctrl_l"], 57443: XK["alt_l"], 57444: XK["super_l"],
    57447: XK["shift_r"], 57448: XK["ctrl_r"], 57449: XK["alt_r"], 57450: XK["super_r"],
    57399: 0xFFB0, 57400: 0xFFB1, 57401: 0xFFB2, 57402: 0xFFB3, 57403: 0xFFB4,
    57404: 0xFFB5, 57405: 0xFFB6, 57406: 0xFFB7, 57407: 0xFFB8, 57408: 0xFFB9,
    57409: 0xFFAE, 57410: 0xFFAF, 57411: 0xFFAA, 57412: 0xFFAB, 57413: 0xFFAD, 57414: 0xFF8D,
}
for _i in range(13, 36):
    CSIU[57376 + _i - 13] = 0xFFBE + _i - 1

CSI_FINAL = {"A": XK["up"], "B": XK["down"], "C": XK["right"], "D": XK["left"], "H": XK["home"],
             "F": XK["end"], "P": 0xFFBE, "Q": 0xFFBF, "R": 0xFFC0, "S": 0xFFC1}
CSI_TILDE = {2: XK["ins"], 3: XK["del"], 5: XK["pgup"], 6: XK["pgdn"], 7: XK["home"], 8: XK["end"],
             11: 0xFFBE, 12: 0xFFBF, 13: 0xFFC0, 14: 0xFFC1, 15: 0xFFC2, 17: 0xFFC3, 18: 0xFFC4,
             19: 0xFFC5, 20: 0xFFC6, 21: 0xFFC7, 23: 0xFFC8, 24: 0xFFC9}
MOD_BITS = [(1, XK["shift_l"]), (2, XK["alt_l"]), (4, XK["ctrl_l"]), (8, XK["super_l"])]

CHAR_KEYSYMS = {"\n": XK["ret"], "\r": XK["ret"], "\t": XK["tab"], "\b": XK["bs"], "\x1b": XK["esc"]}


def char_keysym(ch):
    if ch in CHAR_KEYSYMS:
        return CHAR_KEYSYMS[ch]
    cp = ord(ch)
    return cp if cp < 0x100 else 0x01000000 | cp


def parse_combo(combo):
    """'ctrl+shift+t' -> ([ctrl_l, shift_l], keysym_of_t)."""
    parts = [p for p in combo.replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("empty key combo")
    mods = []
    for p in parts[:-1]:
        if p.lower() not in MOD_NAMES:
            raise ValueError(f"unknown modifier {p!r}")
        mods.append(KEY_NAMES[p.lower()])
    last = parts[-1]
    if len(last) == 1:
        sym = char_keysym(last)
    elif last.lower() in KEY_NAMES:
        sym = KEY_NAMES[last.lower()]
    else:
        raise ValueError(f"unknown key {last!r}")
    return mods, sym
