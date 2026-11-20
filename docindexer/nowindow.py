# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Evita che i processi figli (es. avviati da qualche libreria) aprano finestre del terminale.

Nell'exe compilato senza console ogni processo figlio "da console" apre la sua finestra nera.
Non tutte le librerie la nascondono,
quindi si aggiunge CREATE_NO_WINDOW a tutti i subprocess.Popen del programma.
Va importato prima di tutto il resto (main.py).
"""
import subprocess
import sys

if sys.platform == "win32" and not getattr(subprocess.Popen, "_docindexer_no_window", False):
    _Popen = subprocess.Popen

    class _NoWindowPopen(_Popen):
        _docindexer_no_window = True

        def __init__(self, *args, **kwargs):
            flags = kwargs.get("creationflags", 0) or 0
            if not flags & (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE):
                kwargs["creationflags"] = flags | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen
