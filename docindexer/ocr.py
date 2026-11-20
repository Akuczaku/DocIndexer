# DocIndexer — Copyright (C) 2026 Andrea Cumini <andrea@osintinfo.net> — www.osintinfo.net
# Licenza GNU AGPL-3.0 con i termini aggiuntivi del file NOTICE (attribuzione all'autore).
"""OCR con RapidOCR (modelli PaddleOCR in formato ONNX, eseguiti con onnxruntime).

Niente programmi esterni: il motore gira dentro il processo. I modelli (.onnx) stanno in
<cartella programma>\\tools\\rapidocr, così il programma compilato resta portatile e funziona
anche senza internet una volta scaricati (Impostazioni > Strumenti esterni).

Un modello OCR copre un intero alfabeto, non una lingua sola:
  multi     PP-OCRv6: tutte le lingue in alfabeto latino (italiano, inglese, turco, ...) + cinese/giapponese
  cyrillic  PP-OCRv5: russo, bulgaro, serbo cirillico, ...   (e così via per gli altri alfabeti)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .config import RAPIDOCR_DIR
from .i18n import tr


@dataclass(frozen=True)
class OcrModel:
    key: str
    name: str
    version: str     # PP-OCRv6 / PP-OCRv5
    det: str         # chiavi dei modelli in rapidocr/default_models.yaml
    rec: str
    lang: str        # lang_type passato a RapidOCR


_CLS = "ch_ppocr_mobile_v2.0_cls_mobile"
_V5_DET = "ch_PP-OCRv5_det_mobile"

MODELS: dict[str, OcrModel] = {m.key: m for m in (
    OcrModel("multi", "Latino (italiano, inglese, turco, europee...) + cinese/giapponese",
             "PP-OCRv6", "multi_PP-OCRv6_det_small", "multi_PP-OCRv6_rec_small", "multi"),
    OcrModel("cyrillic", "Cirillico (russo, bulgaro, serbo, macedone...)",
             "PP-OCRv5", _V5_DET, "cyrillic_PP-OCRv5_rec_mobile", "cyrillic"),
    OcrModel("eslav", "Slavo orientale (russo, ucraino, bielorusso)",
             "PP-OCRv5", _V5_DET, "eslav_PP-OCRv5_rec_mobile", "eslav"),
    OcrModel("el", "Greco", "PP-OCRv5", _V5_DET, "el_PP-OCRv5_rec_mobile", "el"),
    OcrModel("arabic", "Arabo (arabo, persiano, urdu...)", "PP-OCRv5", _V5_DET, "arabic_PP-OCRv5_rec_mobile",
             "arabic"),
    OcrModel("korean", "Coreano", "PP-OCRv5", _V5_DET, "korean_PP-OCRv5_rec_mobile", "korean"),
    OcrModel("th", "Thailandese", "PP-OCRv5", _V5_DET, "th_PP-OCRv5_rec_mobile", "th"),
    OcrModel("devanagari", "Devanagari (hindi, marathi, nepalese...)", "PP-OCRv5", _V5_DET,
             "devanagari_PP-OCRv5_rec_mobile", "devanagari"),
    OcrModel("ta", "Tamil", "PP-OCRv5", _V5_DET, "ta_PP-OCRv5_rec_mobile", "ta"),
    OcrModel("te", "Telugu", "PP-OCRv5", _V5_DET, "te_PP-OCRv5_rec_mobile", "te"),
)}
DEFAULT_MODEL = "multi"
# "auto": per ogni documento si prova il modello latino e, se il testo riconosciuto è poco sicuro,
# gli altri modelli scaricati; si tiene quello che legge meglio (vedi extractors.Extractor._ocr_model)
AUTO = "auto"
AUTO_NAME = "Automatico: rileva l'alfabeto del documento (usa i modelli scaricati)"


def get_model(key: str) -> OcrModel:
    return MODELS.get(key) or MODELS[DEFAULT_MODEL]


def model_name(key: str) -> str:
    return AUTO_NAME if key == AUTO else get_model(key).name


def installed() -> list[str]:
    """Modelli con tutti i file presenti in tools\rapidocr."""
    out = []
    for k in MODELS:
        try:
            if not missing_files(k):
                out.append(k)
        except Exception:  # noqa: BLE001
            pass
    return out


# --------------------------------------------------------------- file dei modelli
def model_files(key: str) -> list[tuple[str, str, str]]:
    """(nome file, url, sha256) dei modelli necessari per `key`: rilevamento, orientamento, riconoscimento."""
    from omegaconf import OmegaConf
    import rapidocr

    m = get_model(key)
    conf = OmegaConf.load(Path(rapidocr.__file__).parent / "default_models.yaml")
    ort = conf["onnxruntime"]
    wanted = [(m.version, "det", m.det), ("PP-OCRv4", "cls", _CLS), (m.version, "rec", m.rec)]
    out = []
    for version, task, model_key in wanted:
        info = ort[version][task][model_key]
        url = str(info["model_dir"])
        out.append((url.rsplit("/", 1)[-1], url, str(info.get("SHA256") or "")))
    return out


def bundled_dir() -> Path:
    """Modelli forniti con il pacchetto rapidocr (PP-OCRv6 + orientamento): si copiano senza scaricarli."""
    import rapidocr

    return Path(rapidocr.__file__).parent / "models"


def missing_files(key: str) -> list[tuple[str, str, str]]:
    return [f for f in model_files(key) if not (RAPIDOCR_DIR / f[0]).is_file()]


# ----------------------------------------------------------------------- motore
_engines: dict[str, object] = {}
THREADS = 2   # thread di onnxruntime per il riconoscimento (impostato dal demone da config.ocr_threads)
_lock = threading.Lock()


class OcrNotReady(Exception):
    pass


# thread di onnxruntime creati per l'OCR (id di sistema) e funzione che ne regola priorità e risparmio
# energetico (impostata dal demone): così si rallentano solo loro e non la finestra
WORKER_TIDS: set[int] = set()
TUNE = None
_baseline: dict[str, set[int]] = {}


def _thread_ids() -> set[int]:
    try:
        import psutil

        return {t.id for t in psutil.Process().threads()}
    except Exception:  # noqa: BLE001
        return set()


def set_threads(n: int) -> None:
    """Thread OCR cambiati (slider Risorse del Monitor o Impostazioni): i motori si ricreano alla prossima
    pagina con il nuovo numero di thread. Una pagina già in lavorazione finisce con il motore vecchio."""
    global THREADS
    n = max(1, int(n))
    with _lock:
        if n != THREADS:
            THREADS = n
            _engines.clear()
            _baseline.clear()
            WORKER_TIDS.intersection_update(_thread_ids())    # i thread dei motori chiusi spariscono


def _no_spinning() -> None:
    """onnxruntime di norma tiene i suoi thread in attesa attiva (spinning) tra un calcolo e l'altro:
    consumano processore senza fare nulla e scaldano il PC. Si spegne aggiungendo l'opzione alla
    creazione delle sessioni di RapidOCR."""
    try:
        from rapidocr.inference_engine.onnxruntime import main as mod
    except ImportError:
        try:
            from rapidocr.inference_engine import onnxruntime as mod
        except ImportError:
            return
    cls = getattr(mod, "OrtInferSession", None)
    raw = getattr(cls, "__dict__", {}).get("_init_sess_opts")
    if cls is None or raw is None or getattr(cls, "_docindexer_nospin", False):
        return
    is_static = isinstance(raw, staticmethod)
    orig = raw.__func__ if is_static else raw

    def init(*a, **kw):
        opts = orig(*a, **kw)
        for key in ("session.intra_op.allow_spinning", "session.inter_op.allow_spinning"):
            try:
                opts.add_session_config_entry(key, "0")
            except Exception:  # noqa: BLE001
                pass
        return opts

    cls._init_sess_opts = staticmethod(init) if is_static else init
    cls._docindexer_nospin = True


def engine(key: str):
    """Motore RapidOCR per il modello `key` (uno solo, condiviso dai thread: onnxruntime è thread-safe
    e rilascia il GIL, quindi più pagine vengono riconosciute davvero in parallelo)."""
    m = get_model(key)
    with _lock:
        if m.key in _engines:
            return _engines[m.key]
        if missing_files(m.key):
            raise OcrNotReady(tr("Modelli OCR '{name}' non presenti in {dir}. "
                                 "Scaricali da Impostazioni > Strumenti esterni.", name=tr(m.name), dir=RAPIDOCR_DIR))
        from rapidocr import LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        _no_spinning()
        before = _thread_ids()

        v6 = m.version == "PP-OCRv6"
        size = ModelType.SMALL if v6 else ModelType.MOBILE
        det, cls, rec = (str(RAPIDOCR_DIR / f[0]) for f in model_files(m.key))
        eng = RapidOCR(params={
            "Global.log_level": "error",
            # pochi thread: l'OCR gira in background e non deve occupare tutto il processore
            "EngineConfig.onnxruntime.intra_op_num_threads": max(1, int(THREADS)),
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Global.return_word_box": True,
            "Global.model_root_dir": str(RAPIDOCR_DIR),
            "Det.ocr_version": OCRVersion(m.version), "Det.model_type": size, "Det.model_path": det,
            "Det.lang_type": LangDet.CH if v6 else LangDet.MULTI,
            "Cls.model_path": cls,
            "Rec.ocr_version": OCRVersion(m.version), "Rec.model_type": size, "Rec.model_path": rec,
            # PP-OCRv6 è un modello unico multilingue: per RapidOCR la sua lingua è "ch"
            "Rec.lang_type": LangRec.CH if v6 else LangRec(m.lang),
        })
        _engines[m.key] = eng
        _baseline[m.key] = before
        _register_new_threads(m.key)
        return eng


def _register_new_threads(key: str) -> None:
    """Thread nati con il motore `key` (onnxruntime li può creare anche solo al primo riconoscimento):
    sono quelli nuovi rispetto a prima della creazione che non appartengono a Python (finestra, demone,
    altri lavori in background). Si passano a TUNE per abbassarne priorità e consumi."""
    before = _baseline.get(key)
    if before is None:
        return
    py = {t.native_id for t in threading.enumerate()}
    new = _thread_ids() - before - py - WORKER_TIDS
    if not new:
        return
    WORKER_TIDS.update(new)
    if TUNE is not None:
        try:
            TUNE(new)
        except Exception:  # noqa: BLE001
            pass


def recognize(img, key: str) -> list[tuple[str, float, list]]:
    """Parole riconosciute in un'immagine PIL: [(parola, punteggio, [[x,y] x4]), ...] raggruppate per riga,
    in ordine di lettura. Le righe sono separate da un elemento ("\\n", 1.0, [])."""
    import numpy as np

    res = engine(key)(np.asarray(img.convert("RGB")))
    m = get_model(key).key
    if m in _baseline:
        _register_new_threads(m)
        _baseline.pop(m, None)       # dopo il primo riconoscimento i thread ci sono tutti
    words: list[tuple[str, float, list]] = []
    if not res.txts:
        return words
    lines = res.word_results or ()
    for i, line_text in enumerate(res.txts):
        line_words = lines[i] if i < len(lines) else ()
        if line_words:
            for w, score, quad in line_words:
                if str(w).strip():
                    words.append((str(w).strip(), float(score), [list(map(float, p)) for p in quad]))
        else:  # niente parole singole: si usa la riga intera con il suo riquadro
            quad = [list(map(float, p)) for p in res.boxes[i]]
            for w in str(line_text).split():
                words.append((w, float(res.scores[i]), quad))
        words.append(("\n", 1.0, []))
    return words
