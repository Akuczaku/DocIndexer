# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""Estrazione del testo (e OCR) in processi separati.

Perché: leggere un file pesante o fare OCR tiene occupato l'interprete Python; nello stesso processo
della finestra, un file enorme (es. un CSV di alcuni GB) la congelava. Qui ogni file viene letto da un
processo "operaio": la finestra resta sempre libera, più operai lavorano insieme su più core (livello di
risorse alto = più operai), un operaio che esagera con la memoria viene chiuso e il file segnato come
errore, senza toccare il programma.

Il ritmo lo decide il processo principale (Governor): misura la CPU usata dagli operai e li sospende /
riprende più volte al secondo perché restino sotto il tetto del livello scelto. Funziona anche a metà di
un file, non solo tra una pagina e l'altra. Se la temperatura del processore è leggibile, vicino alla
soglia di surriscaldamento il tetto scende da solo.

Protocollo (pickle su stdin/stdout): ingresso {"sha","path","doc_type","cfg","lang","threads"} ...
uscita ("ok", sha, risultato pronto da scrivere nel db) | ("err", sha, messaggio, dettagli).

Avvio: python -m docindexer.extractworker   (programma compilato: DocIndexer.exe --extract-worker)
"""
from __future__ import annotations

import os
import pickle
import queue
import subprocess
import sys
import threading
import time

import psutil

ARG = "--extract-worker"


def command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, ARG]
    return [sys.executable, "-m", "docindexer.extractworker"]


# ===================================================================== operaio
def main() -> None:
    """Ciclo dell'operaio: un file per volta, finché il programma principale non chiude il canale."""
    out = sys.stdout.buffer
    inp = sys.stdin.buffer
    sys.stdout = open(os.devnull, "w")          # nessuna libreria deve scrivere nel canale dei risultati
    try:
        from . import nowindow  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    from . import ocr
    from .db import prepare_extraction
    from .extractors import ExtractionError, Extractor
    from .i18n import set_language

    _exit_with_parent(os.getppid())
    # il programma deve conoscere QUESTO processo: da un ambiente virtuale su Windows python.exe è solo un
    # lanciatore che avvia il vero interprete come processo figlio, e misurare o frenare il lanciatore non serve
    pickle.dump(("hello", os.getpid()), out, protocol=pickle.HIGHEST_PROTOCOL)
    out.flush()
    ex = None
    threads = None
    while True:
        try:
            job = pickle.load(inp)
        except (EOFError, OSError, pickle.UnpicklingError, ValueError):
            break
        try:
            set_language(job.get("lang", "it"))
            if threads != job.get("threads"):
                threads = job.get("threads")
                ocr.set_threads(int(threads or 1))
            if ex is None:
                ex = Extractor(job["cfg"])
            else:
                ex.cfg = job["cfg"]
            stream = ex.text_stream(job["path"], job.get("doc_type"))
            if stream is not None:
                msg = ("ok", job["sha"], _stream(ex, job, stream, out))
            else:
                result = ex.extract(job["path"], job.get("doc_type"))
                msg = ("ok", job["sha"], prepare_extraction(result))   # anche i calcoli sul testo si fanno qui
                del result
        except (ExtractionError, OSError) as exc:
            msg = ("err", job["sha"], str(exc), "")
        except MemoryError:
            msg = ("err", job["sha"], "MemoryError", "")
        except Exception as exc:  # noqa: BLE001
            import traceback

            msg = ("err", job["sha"], f"{type(exc).__name__}: {exc}", traceback.format_exc())
        try:
            pickle.dump(msg, out, protocol=pickle.HIGHEST_PROTOCOL)
            out.flush()
        except OSError:
            break
    if ex is not None:
        ex.close()


def _stream(ex, job: dict, pages, out) -> dict:
    """File di testo enorme: le pagine vanno al programma una per volta ("part"), così né qui né là il file
    sta mai tutto in memoria. Ritorna il riepilogo finale (senza pagine: sono già state scritte)."""
    from pathlib import Path

    from . import detect as dt
    from .db import prepare_extraction
    from .extractors import Extraction

    meta, meta_read = ex.metadata(Path(job["path"]))
    pickle.dump(("begin", job["sha"]), out, protocol=pickle.HIGHEST_PROTOCOL)
    out.flush()
    count = chars = 0
    first = None
    for page in pages:
        d = prepare_extraction(Extraction(dt.TEXT, [page], 1))
        if first is None:
            first = d                       # lingua e impronta dei quasi-duplicati dalla prima pagina
        count += 1
        chars += len(page.text)
        pickle.dump(("part", job["sha"], d["pages"]), out, protocol=pickle.HIGHEST_PROTOCOL)
        out.flush()
    final = prepare_extraction(Extraction(dt.TEXT, [], count, meta, meta_read))
    final.update(append=True, chars=chars, lang=first["lang"] if first else None,
                 simhash=first["simhash"] if first else None)
    return final


def _exit_with_parent(pid: int) -> None:
    def watch():
        try:
            while psutil.pid_exists(pid):
                time.sleep(2)
        except Exception:  # noqa: BLE001
            return
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


# ============================================================ lato programma
_job_handle = None


def _kill_with_me(proc: subprocess.Popen) -> None:
    """Windows: gli operai si chiudono insieme al programma, anche se viene terminato di colpo mentre
    sono sospesi (un processo sospeso non può accorgersi da solo che il programma non c'è più)."""
    global _job_handle
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if _job_handle is None:
            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rt", "wt", "ot")]

            class EXT(ctypes.Structure):
                _fields_ = [("Basic", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            h = k32.CreateJobObjectW(None, None)
            if not h:
                return
            info = EXT()
            info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return
            _job_handle = h
        k32.AssignProcessToJobObject(_job_handle, wintypes.HANDLE(int(proc._handle)))
    except Exception:  # noqa: BLE001
        pass


def _tune_process(pid: int, eco: bool) -> None:
    """Priorità dell'operaio: sempre la più bassa di Windows, per processore e disco, così qualunque
    programma dell'utente gli passa davanti. `eco` (livelli bassi, o utente al PC) aggiunge il risparmio
    energetico di Windows (EcoQoS: core efficienti, niente turbo, quindi meno calore)."""
    try:
        p = psutil.Process(pid)
        if sys.platform == "win32":
            p.nice(psutil.IDLE_PRIORITY_CLASS)
            try:
                p.ionice(psutil.IOPRIO_VERYLOW)             # anche il disco: letture in coda a quelle dell'utente
            except (psutil.Error, OSError, AttributeError, ValueError):
                pass
            import ctypes
            from ctypes import wintypes

            class STATE(ctypes.Structure):
                _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                            ("StateMask", wintypes.ULONG)]

            st = STATE(1, 1, 1 if eco else 0)       # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x0200, False, pid)             # PROCESS_SET_INFORMATION
            if h:
                k32.SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st))
                k32.CloseHandle(h)
        else:
            p.nice(19 if eco else 10)
    except Exception:  # noqa: BLE001
        pass


class Worker:
    def __init__(self, results: queue.Queue):
        from .config import BASE_DIR

        self.proc = subprocess.Popen(command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=str(BASE_DIR),
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _kill_with_me(self.proc)
        self.ps = psutil.Process(self.proc.pid)
        self.job: dict | None = None
        self.t0 = 0.0
        self.noted = False
        self.suspended = False
        self.eco: bool | None = None
        self.idle_since = time.monotonic()
        self.killed_for: str | None = None
        self._results = results
        threading.Thread(target=self._read, name="extract-reader", daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                msg = pickle.load(self.proc.stdout)
            except Exception:  # noqa: BLE001  (canale chiuso: operaio terminato)
                self._results.put((self, None))
                return
            if msg and msg[0] == "hello":           # il processo che lavora davvero (vedi main)
                try:
                    if msg[1] != self.proc.pid:
                        self.ps = psutil.Process(msg[1])
                    self.eco = None                 # priorità da (ri)applicare al processo giusto
                except (psutil.Error, OSError):
                    pass
                continue
            self._results.put((self, msg))

    def send(self, job: dict) -> bool:
        self.job, self.t0, self.noted = job, time.monotonic(), False
        try:
            pickle.dump(job, self.proc.stdin, protocol=pickle.HIGHEST_PROTOCOL)
            self.proc.stdin.flush()
            return True
        except OSError:
            return False

    def alive(self) -> bool:
        return self.proc.poll() is None

    def cpu(self) -> float:
        try:
            t = self.ps.cpu_times()
            return t.user + t.system
        except (psutil.Error, OSError):
            return 0.0

    def rss(self) -> int:
        try:
            return self.ps.memory_info().rss
        except (psutil.Error, OSError):
            return 0

    def suspend(self, on: bool) -> None:
        if on == self.suspended:
            return
        try:
            self.ps.suspend() if on else self.ps.resume()
            self.suspended = on
        except (psutil.Error, OSError):
            pass

    def kill(self) -> None:
        for p in (self.ps, self.proc):              # il processo che lavora e, se diverso, il suo lanciatore
            try:
                p.kill()
            except (psutil.Error, OSError):
                pass


def max_workers(level: int) -> int:
    """Operai in parallelo per livello di risorse: 1 ai livelli bassi, poi sempre di più fino a tutti i
    core fisici meno uno (la finestra e il resto del PC restano reattivi). Circa 2 GB di RAM per operaio."""
    cores = psutil.cpu_count(logical=False) or max(1, (psutil.cpu_count() or 2) // 2)
    n = {1: 1, 2: 1, 3: 2, 4: max(2, cores // 2), 5: max(2, cores - 1)}.get(int(level), 2)
    try:
        ram = psutil.virtual_memory().total // (2 * 1024 ** 3)
    except (psutil.Error, OSError):
        ram = 2
    return max(1, min(n, cores, int(ram), 12))


# ================================================================ temperatura
class Thermal:
    """Temperature di processore e scheda video, lette ogni pochi secondi in un thread a parte.

    CPU: contatore "Thermal Zone Information" di Windows (non richiede permessi di amministratore; su alcuni
    PC non esiste). GPU: nvidia-smi (schede NVIDIA). Dove una lettura non è disponibile il valore resta None
    e quella soglia non interviene.

    Freno: quando CPU o GPU raggiungono la soglia scelta nel Monitor (config.temp_cpu_max / temp_gpu_max)
    `factor` scende (meno CPU consentita, meno file letti insieme) e risale appena la temperatura torna sotto:
    vedi update()."""

    MARGIN = 8.0

    def __init__(self, cfg=None, stop: threading.Event | None = None):
        self.cfg = cfg
        self.value: float | None = None         # CPU, °C
        self.gpu: float | None = None           # GPU, °C
        self.factor = 1.0
        self.hot = None                         # (sensore, °C, soglia) quando il freno è attivo
        self._query = None
        self._counter = None
        self._dead = sys.platform != "win32"
        self._gpu_dead = False
        self._smi: str | None = None
        self._stop = stop or threading.Event()
        threading.Thread(target=self._loop, name="thermal", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.update()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(4.0)

    def _read_gpu(self) -> float | None:
        import shutil

        if self._smi is None:
            self._smi = shutil.which("nvidia-smi") or ""
            if not self._smi and sys.platform == "win32":
                cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
                self._smi = cand if os.path.exists(cand) else ""
        if not self._smi:
            raise OSError("no nvidia-smi")
        out = subprocess.run([self._smi, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        temps = [float(x) for x in out.split() if x.strip().replace(".", "", 1).isdigit()]
        return max(temps) if temps else None

    def _read(self) -> float | None:
        import ctypes
        from ctypes import wintypes

        pdh = ctypes.windll.pdh
        if self._query is None:
            q, c = wintypes.HANDLE(), wintypes.HANDLE()
            pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]
            pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_void_p,
                                                  ctypes.POINTER(wintypes.HANDLE)]
            pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
            pdh.PdhGetFormattedCounterArrayW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                         ctypes.POINTER(wintypes.DWORD),
                                                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            if pdh.PdhOpenQueryW(None, None, ctypes.byref(q)) != 0:
                raise OSError("pdh")
            if pdh.PdhAddEnglishCounterW(q, r"\Thermal Zone Information(*)\Temperature", None,
                                         ctypes.byref(c)) != 0:
                raise OSError("counter")
            self._query, self._counter = q, c
        if pdh.PdhCollectQueryData(self._query) != 0:
            raise OSError("collect")
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        pdh.PdhGetFormattedCounterArrayW(self._counter, 0x100, ctypes.byref(size), ctypes.byref(count), None)
        if not size.value:
            raise OSError("empty")
        buf = ctypes.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(self._counter, 0x100, ctypes.byref(size), ctypes.byref(count),
                                            buf) != 0:
            raise OSError("array")

        class VALUE(ctypes.Structure):            # PDH_FMT_COUNTERVALUE (PDH_FMT_LONG = 0x100)
            _fields_ = [("CStatus", wintypes.DWORD), ("value", ctypes.c_longlong)]

        class ITEM(ctypes.Structure):
            _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", VALUE)]

        items = ctypes.cast(buf, ctypes.POINTER(ITEM))
        temps = [ctypes.c_long(items[i].FmtValue.value & 0xFFFFFFFF).value - 273.15 for i in range(count.value)
                 if items[i].FmtValue.CStatus == 0]
        temps = [t for t in temps if 5 < t < 125]
        return max(temps) if temps else None

    def update(self) -> None:
        if not self._dead:
            try:
                self.value = self._read()
            except Exception:  # noqa: BLE001
                self.value = None
                self._dead = True
        if not self._gpu_dead:
            try:
                self.gpu = self._read_gpu()
            except Exception:  # noqa: BLE001
                self.gpu = None
                self._gpu_dead = True
        pairs = [("CPU", self.value, getattr(self.cfg, "temp_cpu_max", 82)),
                 ("GPU", self.gpu, getattr(self.cfg, "temp_gpu_max", 80))]
        pairs = [(n, t, float(lim)) for n, t, lim in pairs if t is not None]
        if not pairs:
            self.factor, self.hot = 1.0, None
            return
        # il sensore più vicino alla sua soglia comanda. Regolazione continua, come un termostato: sopra la
        # soglia si scende (più in fretta quanto più la si supera), sotto si risale (più in fretta quanto più
        # si è lontani). Così il ritmo si assesta sul massimo che tiene la temperatura appena sotto la soglia,
        # invece di restare bloccato in basso dopo averla toccata una volta.
        name, t, lim = max(pairs, key=lambda x: x[1] - x[2])
        over = t - lim
        if over >= 0:
            self.factor = max(0.10, self.factor * (0.85 if over < 3 else 0.7))
        elif over <= -2:
            self.factor = min(1.0, self.factor + min(0.12, 0.02 * -over))
        self.hot = (name, t, lim) if self.factor < 0.98 else None


# ================================================================== governor
class Pool:
    """Gli operai e il loro ritmo."""

    def __init__(self, cfg, stop: threading.Event, limit, log):
        self.cfg = cfg
        self.stop = stop
        self.limit = limit              # funzione: % di CPU consentita adesso (di tutto il processore)
        self.log = log
        self.results: queue.Queue = queue.Queue()
        self.workers: list[Worker] = []
        self.thermal = Thermal(cfg, stop)
        self.ncpu = psutil.cpu_count() or 1
        self.cpu_pct = 0.0              # CPU usata dagli operai (% di tutto il processore), per il Monitor
        self.throttled = False
        self.broken = False             # impossibile avviare operai: l'indicizzatore lavora nel suo processo
        self._lock = threading.Lock()
        self._budget = 0.0
        # quanto il PC è "libero" per noi: 0 = l'utente lo sta usando, 1 = tempo morto (vedi _allowed)
        self.share = 1.0
        self.others = 0.0               # CPU usata dagli altri programmi (% di tutto il processore)
        self.allowed = 0.0              # % di CPU concessa adesso all'indicizzazione
        self.idle_fn = None             # secondi dall'ultimo tasto o movimento del mouse (dato dall'indicizzatore)
        self.battery = lambda: False
        self._sys0 = None
        self._sys_t = 0.0
        # memoria massima di un operaio: oltre, il file viene abbandonato (errore) invece di bloccare il PC
        self.mem_cap = max(2560 * 1024 ** 2, int(psutil.virtual_memory().total * 0.35))
        self._cpu0: dict[int, float] = {}
        self._thread: threading.Thread | None = None

    # ----------------------------------------------------------- operai
    def busy(self) -> list[Worker]:
        return [w for w in self.workers if w.job is not None]

    def target(self) -> int:
        """File letti insieme adesso: quelli del livello, meno quando il processore è caldo."""
        n = max_workers(self.cfg.resource_level) * self.thermal.factor
        return max(1, int(round(n * (1 / 3 + 2 / 3 * self.share))))     # utente al PC: un terzo

    ACTIVE_S = 15.0         # per questi secondi dopo l'ultimo tasto o movimento l'utente "sta usando il PC"

    def _allowed(self, now: float) -> float:
        """Percentuale di CPU concessa adesso. L'idea: sfruttare i tempi morti e farsi da parte quando
        l'utente lavora.
          - utente al PC (input negli ultimi 15 s): al massimo il tetto "mentre usi il PC" e comunque non più
            di metà della CPU lasciata libera dagli altri programmi
          - poi si sale gradualmente fino al tetto "a PC inattivo", raggiunto dopo idle_minutes
          - al primo tasto o movimento si torna subito giù (entro un quinto di secondo)."""
        cfg = self.cfg
        base = self.limit()                 # tiene conto anche della batteria
        if now - self._sys_t >= 1.0:        # carico degli altri programmi, misurato su 1 secondo
            try:
                t = psutil.cpu_times()
                if self._sys0 is not None:
                    busy = lambda x: sum(x) - x.idle - getattr(x, "iowait", 0.0)  # noqa: E731
                    span = sum(t) - sum(self._sys0)
                    total = 100.0 * (busy(t) - busy(self._sys0)) / span if span > 0 else 0.0
                    self.others = max(0.0, total - self.cpu_pct)
                self._sys0, self._sys_t = t, now
            except (psutil.Error, OSError):
                pass
        full_after = max(self.ACTIVE_S + 1, float(cfg.idle_minutes) * 60)
        idle = self.idle_fn() if self.idle_fn else None
        if idle is None or idle >= full_after:
            self.share = 1.0
        elif idle <= self.ACTIVE_S:
            self.share = 0.0
        else:
            self.share = (idle - self.ACTIVE_S) / (full_after - self.ACTIVE_S)
        active = min(float(cfg.cpu_limit), max(3.0, (100.0 - self.others) * 0.5))
        pct = active + (max(active, float(cfg.cpu_limit_idle)) - active) * self.share
        if self.battery():
            pct = min(pct, base)
        return max(2.0, min(100.0, pct))

    def free_slot(self) -> bool:
        return len(self.busy()) < self.target()

    def submit(self, job: dict) -> bool:
        """Affida un file a un operaio libero (avviandolo se serve)."""
        self._ensure_thread()
        with self._lock:
            w = next((w for w in self.workers if w.job is None and w.alive()), None)
            if w is None:
                try:
                    w = Worker(self.results)
                except Exception as exc:  # noqa: BLE001
                    self.broken = True
                    self.log(f"[!] extract worker: {exc}")
                    return False
                self.workers.append(w)
            self._tune(w)
            if not w.send(job):
                w.job = None
                w.kill()
                self.workers.remove(w)
                return False
            return True

    def _tune(self, w: Worker) -> None:
        from .config import ECO_MAX_LEVEL

        eco = int(self.cfg.resource_level) <= ECO_MAX_LEVEL or self.share < 0.5    # utente al PC: niente turbo
        if w.eco != eco:
            w.eco = eco
            _tune_process(w.ps.pid, eco)

    def retune(self) -> None:
        with self._lock:
            for w in self.workers:
                self._tune(w)

    def finished(self, w: Worker) -> None:
        w.job = None
        w.idle_since = time.monotonic()
        if not w.alive():
            with self._lock:
                if w in self.workers:
                    self.workers.remove(w)

    def close(self) -> None:
        with self._lock:
            for w in self.workers:
                w.kill()
            self.workers.clear()

    # ------------------------------------------------------------ ritmo
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._govern, name="extract-governor", daemon=True)
            self._thread.start()

    def _govern(self) -> None:
        last = time.monotonic()
        window: list[tuple[float, float]] = []
        while not self.stop.is_set():
            self.stop.wait(0.2)
            now = time.monotonic()
            dt, last = now - last, now
            with self._lock:
                workers = list(self.workers)
            used = 0.0
            for w in workers:
                c = w.cpu()
                used += max(0.0, c - self._cpu0.get(w.ps.pid, c))
                self._cpu0[w.ps.pid] = c
                if w.eco is None:
                    with self._lock:
                        self._tune(w)
                if w.job is not None and w.rss() > self.mem_cap and w.killed_for is None:
                    w.killed_for = "memory"         # file che riempie la memoria: si ferma lui, non il PC
                    w.kill()
                elif w.job is None and now - w.idle_since > 90 and len(workers) > 0:
                    w.kill()                        # operai fermi da un po': si libera la loro memoria
                    with self._lock:
                        if w in self.workers and w.job is None:
                            self.workers.remove(w)
            window.append((now, used))
            window = [x for x in window if now - x[0] <= 2.0]
            span = max(0.2, now - window[0][0] + 0.2)
            self.cpu_pct = 100.0 * sum(u for _t, u in window) / span / self.ncpu
            paused = bool(self.cfg.paused)
            self.allowed = self._allowed(now) * self.thermal.factor
            rate = max(0.02, self.allowed / 100.0) * self.ncpu                  # s di CPU al secondo
            for w in workers:
                if w.eco is not None and w.eco != (int(self.cfg.resource_level) <= 2 or self.share < 0.5):
                    with self._lock:
                        self._tune(w)       # utente tornato / andato via: risparmio energetico sì / no
            self._budget = max(-rate * 1.0, min(rate * 0.4, self._budget + rate * dt - used))
            hold = paused or self._budget < 0
            self.throttled = hold and not paused
            for w in workers:
                w.suspend(hold and w.job is not None)
        for w in list(self.workers):
            w.suspend(False)


if __name__ == "__main__":
    main()
