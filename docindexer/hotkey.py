# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Scorciatoia da tastiera globale (funziona con qualsiasi programma in primo piano).

Usa RegisterHotKey di Windows in un thread con la sua coda di messaggi: nessun hook della tastiera,
nessun permesso particolare. Se un altro programma usa già la stessa combinazione la registrazione
fallisce e `error` lo dice.

Formato: "ctrl+alt+space", "ctrl+shift+f", "win+alt+d", "ctrl+alt+f9"...
"""
from __future__ import annotations

import sys
import threading

MOD = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
_KEYS = {"space": 0x20, "spazio": 0x20, "enter": 0x0D, "tab": 0x09, "insert": 0x2D, "home": 0x24, "end": 0x23,
         "pageup": 0x21, "pagedown": 0x22, "pause": 0x13, ".": 0xBE, ",": 0xBC, "-": 0xBD}
_KEYS.update({f"f{i}": 0x6F + i for i in range(1, 13)})


def parse(spec: str) -> tuple[int, int]:
    """'ctrl+alt+space' -> (modificatori, tasto virtuale). ValueError se non valida."""
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(spec)
    mods = 0
    for p in parts[:-1]:
        if p not in MOD:
            raise ValueError(spec)
        mods |= MOD[p]
    key = parts[-1]
    if key in _KEYS:
        vk = _KEYS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    else:
        raise ValueError(spec)
    if not mods & (MOD["ctrl"] | MOD["alt"] | MOD["win"]):
        raise ValueError(spec)          # serve almeno Ctrl, Alt o Win: non si ruba un tasto normale
    return mods, vk


def pretty(spec: str) -> str:
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in spec.lower().replace(" ", "").split("+"))


class GlobalHotkey(threading.Thread):
    def __init__(self, spec: str, callback):
        super().__init__(daemon=True, name="hotkey")
        self.spec = spec
        self.callback = callback
        self.error: str | None = None
        self.ready = threading.Event()
        self._tid = 0

    def run(self) -> None:
        if sys.platform != "win32":
            self.error = "Windows only"
            self.ready.set()
            return
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        try:
            mods, vk = parse(self.spec)
        except ValueError:
            self.error = "invalid"
            self.ready.set()
            return
        self._tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
            self.error = "busy"
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self._tid and sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        self._tid = 0


def start(spec: str, callback) -> GlobalHotkey | None:
    """Registra la scorciatoia; ritorna il thread (con .error se non è stato possibile) o None se spec vuota."""
    if not spec.strip():
        return None
    hk = GlobalHotkey(spec, callback)
    hk.start()
    hk.ready.wait(2)
    return hk
