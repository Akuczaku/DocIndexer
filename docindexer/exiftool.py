# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Metadati dei file con ExifTool (Phil Harvey), modulo aggiuntivo facoltativo in tools\\exiftool.

ExifTool non è incluso: si scarica da Impostazioni > Strumenti esterni (versione Windows ufficiale) e gira
come programma esterno. Per non pagare l'avvio di Perl a ogni file resta aperto in modalità -stay_open e
riceve i file uno alla volta. I metadati diventano una pagina di testo "Metadati" del documento.
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from .config import TOOLS_DIR

EXIFTOOL_DIR = TOOLS_DIR / "exiftool"
EXIFTOOL_EXE = EXIFTOOL_DIR / "exiftool.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# gruppi e tag che non dicono nulla del contenuto (percorso, permessi, versione di ExifTool...)
_SKIP_GROUPS = {"ExifTool", "System"}
_SKIP_TAGS = {"SourceFile", "ThumbnailImage", "PreviewImage", "JpgFromRaw", "OtherImage", "ICC_Profile",
              "DataDump", "XMPToolkit", "ProfileCMMType"}


def available() -> bool:
    return EXIFTOOL_EXE.is_file()


class ExifTool:
    """Processo ExifTool persistente (-stay_open). Thread-safe; si riavvia da solo se si chiude."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def _start(self) -> None:
        self.proc = subprocess.Popen(
            [str(EXIFTOOL_EXE), "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(EXIFTOOL_DIR), creationflags=NO_WINDOW)

    def metadata(self, path: str | Path, timeout_lines: int = 200000) -> dict:
        """Tutti i metadati del file come {"Gruppo:Tag": valore}."""
        args = ["-json", "-G1", "-a", "-charset", "filename=utf8", "-api", "largefilesupport=1",
                "-b0", str(path)]
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            payload = "\n".join(a for a in args if a != "-b0") + "\n-execute\n"
            self.proc.stdin.write(payload.encode("utf-8"))
            self.proc.stdin.flush()
            out = []
            for _ in range(timeout_lines):
                line = self.proc.stdout.readline()
                if not line:
                    self.proc = None          # ExifTool si è chiuso: al prossimo file si riavvia
                    break
                if line.strip() == b"{ready}":
                    break
                out.append(line)
        raw = b"".join(out).decode("utf-8", "replace").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data[0] if data else {}
        except ValueError:
            return {}

    def close(self) -> None:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self.proc.stdin.write(b"-stay_open\nFalse\n")
                    self.proc.stdin.flush()
                    self.proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    self.proc.kill()
            self.proc = None


def to_items(meta: dict) -> list[tuple[str, str]]:
    """Metadati come coppie ("Gruppo · Tag", valore), senza i tag tecnici e i dati binari."""
    out = []
    for line in to_text(meta).splitlines():
        tag, _, value = line.partition(": ")
        if value:
            out.append((tag, value))
    return out


def to_text(meta: dict) -> str:
    """Metadati in righe "Gruppo · Tag: valore", senza i tag tecnici e i dati binari."""
    lines = []
    for key, value in meta.items():
        group, _, tag = key.partition(":")
        if not tag:
            group, tag = "", group
        if group in _SKIP_GROUPS or tag in _SKIP_TAGS:
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        value = str(value).strip()
        if not value or value.startswith("(Binary data") or value.startswith("base64:"):
            continue
        lines.append(f"{group} · {tag}: {value}" if group else f"{tag}: {value}")
    return "\n".join(lines)
