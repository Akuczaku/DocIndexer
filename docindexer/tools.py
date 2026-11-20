# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Strumenti di terze parti di DocIndexer, tutti portatili dentro <cartella programma>\\tools.

- Modelli OCR RapidOCR -> tools\\rapidocr  (file .onnx; il modello "latino + cinese/giapponese"
                          viene copiato da quelli inclusi nel programma, gli altri alfabeti si scaricano)
- Microsoft Word (opzionale, per .doc/.rtf): solo verifica
- Microsoft PowerPoint (opzionale, per .ppt): solo verifica

Le librerie Python non sono gestite qui: nel programma compilato sono incluse nell'eseguibile,
in sviluppo si installano con  pip install -r requirements.txt
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import ocr
from .i18n import tr
from .config import RAPIDOCR_DIR, TOOLS_DIR, AppConfig, portable

Log = Callable[[str], None]
# progress(etichetta, byte_scaricati, byte_totali)   totali = 0 se sconosciuti
Progress = Callable[[str, int, int], None]

DOWNLOADS_DIR = TOOLS_DIR / "_download"


@dataclass
class Status:
    key: str
    name: str
    ok: bool
    detail: str
    optional: bool = False
    installable: bool = True


def _no_progress(label: str, done: int, total: int) -> None:
    pass


# ---------------------------------------------------------------- verifiche
def check_model(cfg: AppConfig, key: str) -> Status:
    """Stato dei file di un modello OCR. Serve quello in uso (e quello predefinito), gli altri sono opzionali."""
    m = ocr.get_model(key)
    optional = key not in (ocr.DEFAULT_MODEL, cfg.ocr_model)
    name = f"OCR {m.version}: {tr(m.name)}"
    missing = ocr.missing_files(key)
    if missing:
        return Status(f"ocr:{key}", name, False, (tr("Manca 1 file in {dir}", dir=portable(RAPIDOCR_DIR)) if len(missing) == 1
                       else tr("Mancano {n} file in {dir}", n=len(missing), dir=portable(RAPIDOCR_DIR))),
                      optional=optional)
    size = sum((RAPIDOCR_DIR / f[0]).stat().st_size for f in ocr.model_files(key)) / 1_048_576
    return Status(f"ocr:{key}", name, True, tr("Presente ({size} MB)", size=f"{size:.0f}"), optional=optional)


EXIFTOOL_VERSION_URL = "https://exiftool.org/ver.txt"
EXIFTOOL_URL = "https://downloads.sourceforge.net/project/exiftool/exiftool-{ver}_64.zip"
EXIFTOOL_FALLBACK = "13.64"


def check_exiftool(cfg: AppConfig) -> Status:
    from . import exiftool

    name = tr("ExifTool (facoltativo, metadati di foto, documenti, audio e video)")
    if not exiftool.available():
        return Status("exiftool", name, False, tr("Non installato"), optional=True)
    ver = ""
    try:
        out = subprocess.run([str(exiftool.EXIFTOOL_EXE), "-ver"], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ver = out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    state = tr("attivo") if cfg.exif_enabled else tr("disattivato nelle impostazioni")
    return Status("exiftool", name, True, f"ExifTool {ver} · {state}", optional=True)


def install_exiftool(cfg: AppConfig, log: Log, progress: Progress = _no_progress) -> bool:
    """Scarica la versione Windows ufficiale di ExifTool e la mette in tools\\exiftool."""
    import zipfile

    from . import exiftool

    try:
        with urllib.request.urlopen(urllib.request.Request(EXIFTOOL_VERSION_URL,
                                                           headers={"User-Agent": "DocIndexer-setup/1.0"}),
                                    timeout=30) as r:
            ver = r.read().decode("ascii", "ignore").strip() or EXIFTOOL_FALLBACK
    except OSError:
        ver = EXIFTOOL_FALLBACK
    zpath = download(EXIFTOOL_URL.format(ver=ver), DOWNLOADS_DIR / f"exiftool-{ver}_64.zip",
                     f"ExifTool {ver}", log, progress)
    target = exiftool.EXIFTOOL_DIR
    shutil.rmtree(target, ignore_errors=True)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            parts = info.filename.split("/", 1)
            if len(parts) < 2 or not parts[1] or info.is_dir():
                continue
            dest = target / parts[1]
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    # "exiftool(-k).exe" aspetta un tasto alla fine: rinominato in exiftool.exe lavora senza fermarsi
    k = next(target.glob("exiftool*.exe"), None)
    if k is not None and k.name != "exiftool.exe":
        k.rename(target / "exiftool.exe")
    status = check_exiftool(cfg)
    log(("OK  " if status.ok else tr("ERRORE") + " ") + f"{status.name}: {status.detail}")
    return status.ok


SEVENZIP_R_URL = "https://www.7-zip.org/a/7zr.exe"
SEVENZIP_PAGE = "https://www.7-zip.org/download.html"
SEVENZIP_FALLBACK = "a/7z2301-x64.exe"


def check_7zip(cfg: AppConfig) -> Status:
    from . import sevenzip

    name = tr("7-Zip (facoltativo, testo dei file dentro zip, rar, 7z, tar, gz)")
    if not sevenzip.available():
        return Status("7zip", name, False, tr("Non installato"), optional=True)
    state = tr("attivo") if cfg.mod_archives else tr("disattivato nelle impostazioni")
    return Status("7zip", name, True, f"7-Zip · {state}", optional=True)


def install_7zip(cfg: AppConfig, log: Log, progress: Progress = _no_progress) -> bool:
    """Scarica l'installer ufficiale di 7-Zip e ne estrae 7z.exe e 7z.dll in tools\\7zip, senza installarlo
    (lo apre 7zr.exe, la versione minima di 7-Zip, scaricata anche lei)."""
    import re as _re

    from . import sevenzip

    try:
        with urllib.request.urlopen(urllib.request.Request(SEVENZIP_PAGE, headers={"User-Agent": "DocIndexer"}),
                                    timeout=30) as r:
            m = _re.search(r"a/7z\d+-x64\.exe", r.read().decode("utf-8", "ignore"))
            rel = m.group(0) if m else SEVENZIP_FALLBACK
    except OSError:
        rel = SEVENZIP_FALLBACK
    sevenr = download(SEVENZIP_R_URL, DOWNLOADS_DIR / "7zr.exe", "7-Zip (7zr)", log, progress)
    setup = download("https://www.7-zip.org/" + rel, DOWNLOADS_DIR / "7zip-setup.exe", "7-Zip", log, progress)
    stage = DOWNLOADS_DIR / "7zip_x"
    shutil.rmtree(stage, ignore_errors=True)
    r = subprocess.run([str(sevenr), "x", "-y", f"-o{stage}", str(setup)], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    exe = next(stage.rglob("7z.exe"), None)
    if r.returncode != 0 or exe is None:
        raise RuntimeError(tr("Estrazione di 7-Zip non riuscita"))
    shutil.rmtree(sevenzip.SEVENZIP_DIR, ignore_errors=True)
    sevenzip.SEVENZIP_DIR.mkdir(parents=True, exist_ok=True)
    for f in ("7z.exe", "7z.dll", "License.txt", "readme.txt"):
        src = exe.parent / f
        if src.is_file():
            shutil.copy2(src, sevenzip.SEVENZIP_DIR / f)
    status = check_7zip(cfg)
    log(("OK  " if status.ok else tr("ERRORE") + " ") + f"{status.name}: {status.detail}")
    return status.ok


def check_access(cfg: AppConfig) -> Status:
    from .extractors import access_driver

    name = tr("Driver Microsoft Access (facoltativo, per .mdb/.accdb)")
    drv = access_driver()
    if drv is None:
        return Status("access", name, False, tr("Non trovato: si installa con Office o con il "
                                                "\"Microsoft Access Database Engine\" gratuito"),
                      optional=True, installable=False)
    state = tr("attivo") if cfg.mod_access else tr("disattivato nelle impostazioni")
    return Status("access", name, True, f"{drv} · {state}", optional=True, installable=False)


def check_word() -> Status:
    name = tr("Microsoft Word (opzionale, per .doc/.rtf)")
    try:
        import winreg

        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"))
        return Status("word", name, True, tr("Disponibile"), optional=True, installable=False)
    except Exception:  # noqa: BLE001
        return Status("word", name, False, tr("Non trovato: per i .doc si usa l'estrazione grezza"),
                      optional=True, installable=False)


def check_powerpoint() -> Status:
    name = tr("Microsoft PowerPoint (opzionale, per .ppt)")
    try:
        import winreg

        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"PowerPoint.Application\CLSID"))
        return Status("powerpoint", name, True, tr("Disponibile"), optional=True, installable=False)
    except Exception:  # noqa: BLE001
        return Status("powerpoint", name, False, tr("Non trovato: per i .ppt si usa la lettura diretta del file"),
                      optional=True, installable=False)


def check_all(cfg: AppConfig) -> list[Status]:
    return [*(check_model(cfg, k) for k in ocr.MODELS), check_exiftool(cfg), check_7zip(cfg), check_access(cfg),
            check_word(), check_powerpoint()]


# ------------------------------------------------------------------ helper
def download(url: str, dest: Path, label: str, log: Log, progress: Progress) -> Path:
    """Scarica url in dest mostrando l'avanzamento. Scrive su .part e rinomina alla fine."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    log(f"Download {label}: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "DocIndexer-setup/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last = 0.0
            progress(label, 0, total)
            while chunk := resp.read(256 * 1024):
                fh.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last > 0.15:
                    progress(label, done, total)
                    last = now
            progress(label, done, total or done)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    log(f"  {label}: {dest.stat().st_size / 1_048_576:.1f} MB")
    return dest


# ------------------------------------------------------------ installazioni
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def install_model(cfg: AppConfig, log: Log, progress: Progress = _no_progress, key: str = ocr.DEFAULT_MODEL) -> bool:
    """Mette in tools\\rapidocr i file del modello: copiati da quelli inclusi nel programma se ci sono,
    altrimenti scaricati (con verifica SHA-256)."""
    RAPIDOCR_DIR.mkdir(parents=True, exist_ok=True)
    todo = ocr.missing_files(key)
    if not todo:
        log(tr("Modello OCR già presente."))
        return True
    bundled = ocr.bundled_dir()
    failed = []
    for i, (name, url, sha) in enumerate(todo, start=1):
        dest = RAPIDOCR_DIR / name
        src = bundled / name
        try:
            if src.is_file():
                shutil.copy2(src, dest)
                log("  " + tr("copiato {name}", name=name))
                continue
            download(url, DOWNLOADS_DIR / name, tr("Modello {i}/{n}: {name}", i=i, n=len(todo), name=name), log, progress)
            if sha and _sha256(DOWNLOADS_DIR / name) != sha.lower():
                raise RuntimeError(tr("SHA-256 non corrispondente, file scartato"))
            shutil.move(str(DOWNLOADS_DIR / name), dest)
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            log("  " + tr("ERRORE {name}: {exc}", name=name, exc=exc))
            (DOWNLOADS_DIR / name).unlink(missing_ok=True)
    status = check_model(cfg, key)
    log(("OK  " if status.ok else tr("ERRORE") + " ") + f"{status.name}: {status.detail}")
    return not failed


def cleanup_downloads(log: Log) -> None:
    if DOWNLOADS_DIR.exists():
        shutil.rmtree(DOWNLOADS_DIR, ignore_errors=True)
        log(tr("File temporanei di download eliminati."))


def install_missing(cfg: AppConfig, log: Log, progress: Progress = _no_progress) -> list[Status]:
    """Installa ciò che manca e non è opzionale (il modello OCR in uso) e ritorna lo stato finale."""
    for st in check_all(cfg):
        if st.ok or st.optional or not st.installable:
            continue
        log(f"== {st.name} ==")
        try:
            if st.key.startswith("ocr:"):
                install_model(cfg, log, progress, st.key.split(":", 1)[1])
        except Exception as exc:  # noqa: BLE001
            log(tr("ERRORE: {exc}", exc=exc))
    cleanup_downloads(log)
    final = check_all(cfg)
    for s in final:
        log(f"{'OK ' if s.ok else ('-- ' if s.optional else 'NO ')} {s.name}: {s.detail}")
    return final


def _install_and_clean(key: str):
    def run(cfg: AppConfig, log: Log, progress: Progress = _no_progress) -> bool:
        try:
            return install_model(cfg, log, progress, key)
        finally:
            cleanup_downloads(log)
    return run


def _install_exiftool_and_clean(cfg: AppConfig, log: Log, progress: Progress = _no_progress) -> bool:
    try:
        return install_exiftool(cfg, log, progress)
    finally:
        cleanup_downloads(log)


INSTALLERS = {f"ocr:{k}": _install_and_clean(k) for k in ocr.MODELS}
INSTALLERS["exiftool"] = _install_exiftool_and_clean


def _install_7zip_and_clean(cfg: AppConfig, log: Log, progress: Progress = _no_progress) -> bool:
    try:
        return install_7zip(cfg, log, progress)
    finally:
        cleanup_downloads(log)


INSTALLERS["7zip"] = _install_7zip_and_clean


def main() -> None:
    r"""Uso da riga di comando (da sorgente):  python -m docindexer.tools"""
    def progress(label, done, total):
        if total:
            print(f"\r  {label}: {done / 1_048_576:.1f}/{total / 1_048_576:.1f} MB   ", end="", flush=True)
            if done >= total:
                print()

    cfg = AppConfig.load()
    final = install_missing(cfg, print, progress)
    sys.exit(0 if all(s.ok or s.optional for s in final) else 1)


if __name__ == "__main__":
    main()
