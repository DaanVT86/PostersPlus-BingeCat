"""PP-OCRv5 burned-in text detection for posters and backdrop crops."""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import json
import logging
import os
import platform
import re
import sys
import threading
import unicodedata
import urllib.request
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from queue import LifoQueue
from typing import Any, Literal

import numpy as np

logger = logging.getLogger(__name__)

try:
    from rapidocr import RapidOCR
    _HAS_RAPIDOCR = True
    _RAPIDOCR_IMPORT_ERROR = None
except Exception as exc:
    RapidOCR = None
    _HAS_RAPIDOCR = False
    _RAPIDOCR_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

_MODEL_URL = os.environ.get(
    "PPOCR_MODEL_URL",
    "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.8.0/"
    "onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
)
_MODEL_SHA256 = os.environ.get(
    "PPOCR_MODEL_SHA256",
    "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
)
_BAKED_MODEL = "/app/models/ch_PP-OCRv5_det_mobile.onnx"
_MODEL_PATH = os.environ.get("PPOCR_MODEL_PATH") or (
    _BAKED_MODEL if os.path.exists(_BAKED_MODEL)
    else "/app/cache/ch_PP-OCRv5_det_mobile.onnx"
)
_RAPIDOCR_MODELS = importlib.resources.files("rapidocr").joinpath("models") if _HAS_RAPIDOCR else None


def _find_bundled_model(models_path, keyword: str) -> str:
    """Find a bundled rapidocr .onnx model by keyword, tolerating version renames."""
    if models_path is None:
        return ""
    import pathlib
    d = pathlib.Path(str(models_path))
    if not d.exists():
        return ""
    try:
        matches = sorted(
            str(f) for f in d.iterdir()
            if f.suffix == ".onnx" and keyword in f.stem.lower()
        )
        if not matches:
            logger.warning(
                f"No bundled rapidocr model found for keyword '{keyword}' in {d}; "
                f"available: {[f.name for f in d.iterdir() if f.suffix == '.onnx']}"
            )
        return matches[0] if matches else ""
    except Exception as exc:
        logger.warning(f"Could not scan rapidocr models dir {d}: {exc}")
        return ""


_CLS_MODEL_PATH = _find_bundled_model(_RAPIDOCR_MODELS, "cls")
_REC_MODEL_PATH = _find_bundled_model(_RAPIDOCR_MODELS, "rec")

try:
    _BOX_THRESHOLD = float(os.environ.get("PPOCR_BOX_THRESHOLD", "0.70"))
except (TypeError, ValueError):
    _BOX_THRESHOLD = 0.70
_BOX_THRESHOLD = max(0.0, min(1.0, _BOX_THRESHOLD))

try:
    _WIDE_BOX_THRESHOLD = float(
        os.environ.get("PPOCR_WIDE_BOX_THRESHOLD", "0.30")
    )
except (TypeError, ValueError):
    _WIDE_BOX_THRESHOLD = 0.30
_WIDE_BOX_THRESHOLD = max(0.0, min(_BOX_THRESHOLD, _WIDE_BOX_THRESHOLD))

try:
    _WIDE_MIN_ASPECT = float(os.environ.get("PPOCR_WIDE_MIN_ASPECT", "3.0"))
except (TypeError, ValueError):
    _WIDE_MIN_ASPECT = 3.0
_WIDE_MIN_ASPECT = max(1.0, _WIDE_MIN_ASPECT)

try:
    _WIDE_MIN_AREA = float(os.environ.get("PPOCR_WIDE_MIN_AREA", "0.01"))
except (TypeError, ValueError):
    _WIDE_MIN_AREA = 0.01
_WIDE_MIN_AREA = max(0.0, min(1.0, _WIDE_MIN_AREA))

try:
    _WIDE_MIN_Y = float(os.environ.get("PPOCR_WIDE_MIN_Y", "0.55"))
except (TypeError, ValueError):
    _WIDE_MIN_Y = 0.55
_WIDE_MIN_Y = max(0.0, min(1.0, _WIDE_MIN_Y))

try:
    _SCAN_TOP = float(os.environ.get("TEXTLESS_SCAN_TOP", "0.08"))
except (TypeError, ValueError):
    _SCAN_TOP = 0.08
_SCAN_TOP = max(0.0, min(0.9, _SCAN_TOP))

_LIMIT_SIDE_LEN = 512


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Read one bounded integer without making import-time config fatal."""

    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_MODEL_SESSIONS = max(1, min(
    4,
    os.cpu_count() or 1,
    _bounded_env_int(
        "TEXTLESS_DETECTION_CONCURRENCY",
        2,
        minimum=1,
        maximum=4,
    ),
))
_AUTO_ORT_THREADS = max(1, min(4, (os.cpu_count() or 1) // _MODEL_SESSIONS))
# The existing default remains CPU-count based.  A deployment with multiple
# worker processes can explicitly cap each process's ONNX intra-op pool (for
# example TEXTLESS_DETECTION_THREADS=2 on a four-core host) without changing
# the detector's selection rules or renderer revision.
_ORT_THREADS = _bounded_env_int(
    "TEXTLESS_DETECTION_THREADS",
    _AUTO_ORT_THREADS,
    minimum=1,
    maximum=4,
)

OCR_RULES_VERSION = "ppocr.textless.v1"
OCR_VERIFICATION_RECIPE = "ppocr.textless.v1"
DETECT_RES_SIG = (
    f"ppocrv5m-r7-s{_LIMIT_SIDE_LEN}-c{int(round(_BOX_THRESHOLD * 100))}"
    f"-wc{int(round(_WIDE_BOX_THRESHOLD * 100))}"
    f"-wa{int(round(_WIDE_MIN_ASPECT * 10))}"
    f"-wr{int(round(_WIDE_MIN_AREA * 10000))}"
    f"-wy{int(round(_WIDE_MIN_Y * 100))}"
    f"-t{int(round(_SCAN_TOP * 100))}"
    f"-ot{_ORT_THREADS}-{OCR_RULES_VERSION}"
)

_ocr_pool = None
_ocr_sessions = []
_model_lock = threading.Lock()
_load_failed = False
_load_error = None


OCRDecision = Literal["textless", "text", "unknown"]


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "missing"
    except Exception:
        return "unknown"


def _normalise_title_context(
    title: str | list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    values = [title] if isinstance(title, str) else list(title or ())
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        normalised = _normalise_text(value)
        if normalised and normalised not in result:
            result.append(normalised)
    return tuple(result)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def title_context_sha256(title: str | list[str] | tuple[str, ...] | None) -> str:
    """Hash the normalized, de-duplicated, ordered title tuple."""

    return hashlib.sha256(_canonical_json(list(_normalise_title_context(title)))).hexdigest()


def ocr_architecture() -> str:
    """Return a stable CPU architecture label for cross-host memo isolation."""

    return (platform.machine() or "unknown").strip().lower()[:32] or "unknown"


def ocr_runtime_diagnostics() -> str:
    """Return readable, complete runtime details for diagnostics only."""

    return ";".join(
        (
            f"python={sys.version}",
            f"implementation={platform.python_implementation()}",
            f"rapidocr={_package_version('rapidocr')}",
            f"onnxruntime={_package_version('onnxruntime')}",
            f"Pillow={_package_version('Pillow')}",
            f"numpy={_package_version('numpy')}",
            f"model=ppocrv5-mobile",
            f"model_sha256={_MODEL_SHA256}",
            f"model_url={_MODEL_URL}",
            f"detect_res_sig={DETECT_RES_SIG}",
            f"sessions={_MODEL_SESSIONS}",
            f"ort_threads={_ORT_THREADS}",
            "ort_inter_op_threads=1",
        )
    )


def _ocr_runtime_binding(
    *,
    model_hash: str,
    detection_rules: str,
    runtime: str,
    architecture: str,
) -> dict[str, Any]:
    """Build the complete, canonical input set behind a memo token."""

    return {
        "binding_version": 1,
        "runtime": runtime,
        "model": "ppocrv5-mobile",
        "model_sha256": model_hash,
        "detection_rules": detection_rules,
        "architecture": architecture,
        "detect_res_sig": DETECT_RES_SIG,
        "settings": {
            "python": sys.version,
            "python_implementation": platform.python_implementation(),
            "rapidocr": _package_version("rapidocr"),
            "onnxruntime": _package_version("onnxruntime"),
            "Pillow": _package_version("Pillow"),
            "numpy": _package_version("numpy"),
            "model_url": _MODEL_URL,
            "model_sessions": _MODEL_SESSIONS,
            "ort_threads": _ORT_THREADS,
            "ort_inter_op_threads": 1,
        },
    }


def _runtime_binding_token(binding: Mapping[str, Any]) -> str:
    """Return the fixed-width owner token for a complete binding payload."""

    # ``v1-`` plus a full SHA-256 is 67 characters.  Hashing the complete
    # canonical payload avoids the old prefix-truncation collision where a
    # long version/model/rules value could hide the changed suffix.
    return f"v1-{hashlib.sha256(_canonical_json(binding)).hexdigest()}"


def ocr_runtime_version() -> str:
    """Return the bounded digest token used by the source-owner key."""

    return _runtime_binding_token(
        _ocr_runtime_binding(
            model_hash=str(_MODEL_SHA256).lower(),
            detection_rules=OCR_VERIFICATION_RECIPE,
            runtime=ocr_runtime_diagnostics(),
            architecture=ocr_architecture(),
        )
    )


def ocr_runtime_signature() -> str:
    """Compatibility alias for the bounded runtime-version token."""

    return ocr_runtime_version()


@dataclass(frozen=True)
class OCRMemoKey:
    """Exact source-owner identity for one completed text decision."""

    kind: Literal["poster", "backdrop"]
    source_sha256: str
    title_context_sha256: str
    detection_rules: str
    model: str
    runtime: str
    runtime_version: str
    architecture: str

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_sha256": self.source_sha256,
            "title_context_sha256": self.title_context_sha256,
            "detection_rules": self.detection_rules,
            "model": self.model,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "architecture": self.architecture,
        }

    @property
    def signature(self) -> str:
        return hashlib.sha256(_canonical_json(self.payload())).hexdigest()


@dataclass(frozen=True)
class OCRMemoEntry:
    """A source-owner memo hit, including an explicit unknown decision."""

    signature: str
    decision: OCRDecision
    verified_at: datetime | None = None

    @property
    def value(self) -> bool | None:
        return {"textless": False, "text": True, "unknown": None}[self.decision]


def build_ocr_memo_key(
    normalized_image_sha256: str,
    source_kind: Literal["poster", "backdrop"],
    title: str | list[str] | tuple[str, ...] | None,
    *,
    model_hash: str | None = None,
    rules: str | None = None,
    runtime: str | None = None,
    arch: str | None = None,
) -> OCRMemoKey:
    """Build the exact source-owner key for one normalized image scan."""

    if not re.fullmatch(r"[0-9a-f]{64}", str(normalized_image_sha256)):
        raise ValueError("normalized_image_sha256 must be a lowercase SHA-256")
    if source_kind not in {"poster", "backdrop"}:
        raise ValueError("source_kind must be poster or backdrop")
    model = "ppocrv5-mobile"
    # The pinned model name remains a stable wire field.  Its complete digest
    # is included in the runtime binding below so a changed model cannot reuse
    # an old memo while keeping the owner's bounded key shape.
    model_hash = str(_MODEL_SHA256 if model_hash is None else model_hash).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", model_hash):
        raise ValueError("model_hash must be a lowercase SHA-256")
    runtime_value = (
        ocr_runtime_diagnostics() if runtime is None else str(runtime)
    )
    rules_value = OCR_VERIFICATION_RECIPE if rules is None else str(rules)
    detection_rules = re.sub(
        r"[^a-zA-Z0-9._:-]",
        "-",
        rules_value,
    ).lower()[:80]
    architecture_value = ocr_architecture() if arch is None else str(arch)
    architecture = re.sub(
        r"[^a-zA-Z0-9._:-]",
        "-",
        architecture_value,
    ).lower()[:80]
    runtime_version = _runtime_binding_token(
        _ocr_runtime_binding(
            model_hash=model_hash,
            detection_rules=rules_value,
            runtime=runtime_value,
            architecture=architecture_value,
        )
    )
    return OCRMemoKey(
        kind=source_kind,
        source_sha256=normalized_image_sha256,
        title_context_sha256=title_context_sha256(title),
        detection_rules=detection_rules,
        model=model,
        runtime="rapidocr_onnxruntime",
        runtime_version=runtime_version,
        architecture=architecture,
    )


def decision_for_detection(value: bool | None) -> OCRDecision:
    """Encode text, clear, and unavailable/uncertain without collapsing them."""

    if value is False:
        return "textless"
    if value is True:
        return "text"
    return "unknown"


def parse_ocr_memo_entry(
    value: Any,
    *,
    expected_key: OCRMemoKey | str,
) -> OCRMemoEntry | None:
    """Parse a source-owner response; ``None`` means a true cache miss.

    A stored ``unknown`` value is returned as an entry whose ``value`` is
    ``None``.  Callers must not treat that as a miss and rerun OCR.
    """

    expected_signature = (
        expected_key.signature if isinstance(expected_key, OCRMemoKey) else expected_key
    )
    if value is None:
        return None
    expected_payload = expected_key.payload() if isinstance(expected_key, OCRMemoKey) else None

    def validate_key(raw_key: Any) -> None:
        if expected_payload is None:
            return
        if hasattr(raw_key, "to_dict"):
            raw_key = raw_key.to_dict()
        if not isinstance(raw_key, Mapping) or dict(raw_key) != expected_payload:
            raise ValueError("OCR memo key mismatch")

    def parse_verified_at(raw_verified_at: Any) -> datetime | None:
        if raw_verified_at is None:
            return None
        if isinstance(raw_verified_at, datetime):
            verified_at = raw_verified_at
        elif isinstance(raw_verified_at, str):
            try:
                verified_at = datetime.fromisoformat(raw_verified_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("invalid OCR memo verified_at") from exc
        else:
            raise ValueError("invalid OCR memo verified_at")
        if verified_at.tzinfo is None or verified_at.utcoffset() is None:
            raise ValueError("OCR memo verified_at must be timezone-aware")
        return verified_at

    if isinstance(value, OCRMemoEntry):
        if (
            value.signature != expected_signature
            or value.decision not in {"textless", "text", "unknown"}
        ):
            raise ValueError("OCR memo key mismatch")
        return value
    if (
        hasattr(value, "signature")
        and hasattr(value, "result")
    ):
        signature = getattr(value, "signature")
        decision = getattr(value, "result")
        if signature != expected_signature or decision not in {"textless", "text", "unknown"}:
            raise ValueError("invalid OCR memo response")
        if hasattr(value, "key"):
            validate_key(getattr(value, "key"))
        source_sha256 = getattr(value, "source_sha256", None)
        if expected_payload is not None and source_sha256 not in {
            None,
            expected_payload["source_sha256"],
        }:
            raise ValueError("OCR memo source mismatch")
        return OCRMemoEntry(
            signature=expected_signature,
            decision=decision,
            verified_at=parse_verified_at(getattr(value, "verified_at", None)),
        )
    if isinstance(value, Mapping) and value.get("found") is False:
        return None
    if isinstance(value, Mapping) and isinstance(value.get("memo"), Mapping):
        value = value["memo"]
    if not isinstance(value, Mapping):
        raise ValueError("invalid OCR memo response")
    signature = value.get("signature", expected_signature)
    decision = value.get("result", value.get("decision"))
    if signature != expected_signature or decision not in {"textless", "text", "unknown"}:
        raise ValueError("invalid OCR memo response")
    if "key" in value:
        validate_key(value["key"])
    if expected_payload is not None and value.get("source_sha256") not in {
        None,
        expected_payload["source_sha256"],
    }:
        raise ValueError("OCR memo source mismatch")
    raw_verified_at = value.get("verified_at")
    verified_at = parse_verified_at(raw_verified_at)
    return OCRMemoEntry(
        signature=expected_signature,
        decision=decision,
        verified_at=verified_at,
    )


def ocr_memo_payload(key: OCRMemoKey, value: bool | None) -> dict[str, Any]:
    """Return the bounded registration payload for the source owner."""

    return {
        "signature": key.signature,
        "key": key.payload(),
        "result": decision_for_detection(value),
    }


def text_detection_available() -> bool:
    """True when the PP-OCR runtime is importable."""
    return _HAS_RAPIDOCR


def text_detection_status() -> str:
    """Compact runtime status suitable for startup and request logs."""
    if not _HAS_RAPIDOCR:
        return f"RapidOCR import failed ({_RAPIDOCR_IMPORT_ERROR})"
    if _ocr_pool is not None:
        return (
            f"ready ({DETECT_RES_SIG}, model={_MODEL_PATH}, "
            f"sessions={_MODEL_SESSIONS}, ort_threads={_ORT_THREADS})"
        )
    if _load_failed:
        return f"model load failed ({_load_error})"
    return f"not loaded (model={_MODEL_PATH})"


def _valid_model(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) < 1_000_000:
        return False
    if os.environ.get("PPOCR_SKIP_MODEL_HASH", "").lower() in ("1", "true", "yes"):
        return True
    digest = hashlib.sha256()
    with open(path, "rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == _MODEL_SHA256


def _new_ocr_session():
    params = {
        "Global.use_cls": False,
        "Global.use_rec": False,
        "Global.log_level": "error",
        "Det.model_path": _MODEL_PATH,
        "Det.limit_side_len": _LIMIT_SIDE_LEN,
        "Det.limit_type": "max",
        "Det.box_thresh": 0.3,
        "EngineConfig.onnxruntime.intra_op_num_threads": _ORT_THREADS,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        # Disable the ORT CPU memory arena so freed tensor allocations are
        # returned to the OS rather than held at the inference high-water mark
        # indefinitely.  Slightly slower first inference; no steady-state cost.
        "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
    }
    # Point RapidOCR at the bundled read-only models so it doesn't try to write
    # to a writable alias path.  Omit the key entirely when the model wasn't
    # found (e.g. rapidocr renamed it in a minor release) — RapidOCR will use
    # its own default, which is safer than a guaranteed FileNotFoundError.
    if _CLS_MODEL_PATH:
        params["Cls.model_path"] = _CLS_MODEL_PATH
    if _REC_MODEL_PATH:
        params["Rec.model_path"] = _REC_MODEL_PATH
    return RapidOCR(params=params)


def _ensure_model():
    """Download and load the bounded PP-OCR session pool once."""
    global _ocr_pool, _ocr_sessions, _load_failed, _load_error
    if not _HAS_RAPIDOCR:
        if not _load_failed:
            _load_error = _RAPIDOCR_IMPORT_ERROR
            _load_failed = True
            logger.warning(f"PP-OCR runtime unavailable: {_RAPIDOCR_IMPORT_ERROR}")
        return None
    if _ocr_pool is not None or _load_failed:
        return _ocr_pool
    with _model_lock:
        if _ocr_pool is not None or _load_failed:
            return _ocr_pool
        try:
            if not _valid_model(_MODEL_PATH):
                logger.info(
                    "Downloading PP-OCRv5 Mobile model (one-time) "
                    f"to {_MODEL_PATH}"
                )
                os.makedirs(os.path.dirname(_MODEL_PATH) or ".", exist_ok=True)
                tmp = _MODEL_PATH + ".part"
                urllib.request.urlretrieve(_MODEL_URL, tmp)
                if not _valid_model(tmp):
                    raise ValueError("downloaded model failed SHA-256 validation")
                os.replace(tmp, _MODEL_PATH)

            sessions = [_new_ocr_session() for _ in range(_MODEL_SESSIONS)]
            pool = LifoQueue(maxsize=_MODEL_SESSIONS)
            for session in sessions:
                pool.put(session)
            _ocr_sessions = sessions
            _ocr_pool = pool
            logger.info(
                "PP-OCRv5 Mobile text detector ready: "
                f"signature={DETECT_RES_SIG}, model={_MODEL_PATH}, "
                f"rapidocr={importlib.metadata.version('rapidocr')}, "
                f"sessions={_MODEL_SESSIONS}, ort_threads={_ORT_THREADS}, "
                f"threshold={_BOX_THRESHOLD:.2f}, "
                f"wide_threshold={_WIDE_BOX_THRESHOLD:.2f}, "
                f"wide_aspect={_WIDE_MIN_ASPECT:.2f}, "
                f"wide_area={_WIDE_MIN_AREA:.4f}"
            )
        except Exception as exc:
            _load_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "PP-OCR model unavailable; text detection disabled: "
                f"{_load_error}"
            )
            _load_failed = True
    return _ocr_pool



@contextmanager
def _borrow_ocr():
    pool = _ensure_model()
    if pool is None:
        yield None
        return
    session = pool.get()
    try:
        yield session
    finally:
        pool.put(session)


def warm_model() -> bool:
    return _ensure_model() is not None


def _detect(image):
    if _ensure_model() is None:
        return None, None, 0, 0
    pil_image = image.convert("RGB")
    width, height = pil_image.size
    if not height or not width:
        pil_image.close()
        return None, None, width, height
    try:
        with _borrow_ocr() as ocr:
            result = ocr(pil_image, use_det=True, use_cls=False, use_rec=False)
        boxes  = [] if result.boxes is None else list(result.boxes)
        scores = [] if result.scores is None else list(result.scores)
        del result
    finally:
        pil_image.close()
    return boxes, scores, width, height


def _normalise_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _title_terms(value: str) -> list[str]:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return [
        term for term in re.findall(r"[a-z0-9]+", value.lower())
        if len(term) >= 4
    ]


def _text_matches_title(candidate: str, title: str) -> bool:
    candidate = _normalise_text(candidate)
    expected = _normalise_text(title)
    if len(candidate) < 4 or len(expected) < 4:
        return False
    if candidate in expected or expected in candidate:
        return True
    if len(candidate) < 6:
        return False
    if len(expected) >= 6 and SequenceMatcher(None, candidate, expected).ratio() >= 0.82:
        return True
    return any(
        SequenceMatcher(None, candidate, term).ratio() >= 0.82
        for term in _title_terms(title)
        if len(term) >= 6
    )


def _recognised_alpha_lengths(texts: list[str]) -> list[int]:
    return [
        len(re.sub(r"[^a-z]", "", text.lower()))
        for text in texts
    ]


def _recognised_title_match(
    image,
    title: str | list[str] | tuple[str, ...],
    boxes,
    scores,
) -> tuple[bool, list[str], int]:
    titles = [title] if isinstance(title, str) else list(title)
    titles = [value for value in titles if value]
    expected_titles = [_normalise_text(value) for value in titles]
    if not any(len(value) >= 4 for value in expected_titles):
        return False, [], 0

    source = image.convert("RGB")
    try:
        width, height = source.size
        image_area = max(1, width * height)
        texts = []
        centred_lines = 0
        for box, score in zip(boxes, scores):
            box = np.asarray(box, dtype=np.float32)
            box_width = float(box[:, 0].max() - box[:, 0].min())
            box_height = float(box[:, 1].max() - box[:, 1].min())
            aspect = box_width / max(1.0, box_height)
            area_ratio = (box_width * box_height) / image_area
            centre_x = float(box[:, 0].mean()) / max(1, width)
            title_candidate = aspect >= 1.5 and area_ratio >= _WIDE_MIN_AREA
            centred_candidate = (
                aspect >= _WIDE_MIN_ASPECT
                and area_ratio >= 0.0015
                and 0.25 <= centre_x <= 0.75
            )
            if (
                float(score) < _WIDE_BOX_THRESHOLD
                or not (title_candidate or centred_candidate)
            ):
                continue

            pad = max(3, int(box_height * 0.2))
            left   = max(0,     int(box[:, 0].min()) - pad)
            top    = max(0,     int(box[:, 1].min()) - pad)
            right  = min(width, int(box[:, 0].max()) + pad)
            bottom = min(height, int(box[:, 1].max()) + pad)
            pil_crop = source.crop((left, top, right, bottom))
            try:
                crop = np.asarray(pil_crop)
                with _borrow_ocr() as ocr:
                    result = ocr(crop, use_det=False, use_cls=False, use_rec=True)
            finally:
                pil_crop.close()
                del crop
            recognised = [] if result.txts is None else [
                str(text) for text in result.txts
            ]
            del result
            texts.extend(recognised)
            if centred_candidate and any(
                len(re.sub(r"[^a-z]", "", text.lower())) >= 5
                for text in recognised
            ):
                centred_lines += 1
            if title_candidate and any(
                _text_matches_title(text, alias)
                for text in recognised
                for alias in titles
            ):
                return True, texts, centred_lines
            if float(score) >= 0.80 and area_ratio >= 0.10:
                for text in recognised:
                    candidate = _normalise_text(text)
                    if any(
                        len(candidate) >= 6
                        and len(expected) >= 6
                        and SequenceMatcher(None, candidate, expected).ratio() >= 0.70
                        for expected in expected_titles
                    ):
                        return True, texts, centred_lines
        return False, texts, centred_lines
    finally:
        source.close()


def _qualifying_boxes(
    boxes,
    scores,
    width: int,
    height: int,
    conf: float,
    scan_top: float,
):
    cutoff = height * scan_top
    image_area = max(1, width * height)
    hits = []
    for box, score in zip(boxes, scores):
        box = np.asarray(box, dtype=np.float32)
        score = float(score)
        center_y = float(box[:, 1].mean())
        if center_y < cutoff:
            continue

        box_width = float(box[:, 0].max() - box[:, 0].min())
        box_height = float(box[:, 1].max() - box[:, 1].min())
        aspect = box_width / max(1.0, box_height)
        area_ratio = (box_width * box_height) / image_area
        is_wide_title = (
            score >= _WIDE_BOX_THRESHOLD
            and aspect >= _WIDE_MIN_ASPECT
            and area_ratio >= _WIDE_MIN_AREA
            and center_y / max(1, height) >= _WIDE_MIN_Y
        )
        if is_wide_title:
            hits.append((box, score, is_wide_title, aspect, area_ratio))
    return hits


def poster_has_burned_in_text(
    image,
    *,
    conf: float = _BOX_THRESHOLD,
    lower_region: float = _SCAN_TOP,
    title: str | list[str] | tuple[str, ...] | None = None,
    source: str = "poster",
    debug: bool = False,
) -> bool | None:
    """Return True/False for a completed scan, or None when unavailable."""
    try:
        if source not in ("poster", "backdrop"):
            raise ValueError(f"unknown text-detection source: {source}")
        boxes, scores, width, height = _detect(image)
        if boxes is None:
            return None
        hits = _qualifying_boxes(boxes, scores, width, height, conf, lower_region)
        recognised = []
        should_recognise = bool(title) and any(
            float(score) >= _WIDE_BOX_THRESHOLD
            for score in scores
        )
        detected = False
        centred_lines = 0
        if should_recognise:
            detected, recognised, centred_lines = _recognised_title_match(
                image, title, boxes, scores
            )
        alpha_lengths = _recognised_alpha_lengths(recognised)
        # Two centred lines are enough when OCR also sees substantial copy;
        # short two-line logos remain below these character thresholds.
        if (
            not detected
            and source == "poster"
            and centred_lines >= 2
            and sum(alpha_lengths) >= 30
            and max(alpha_lengths, default=0) >= 16
        ):
            detected = True
        # Recognition is primary because PP-OCR can confidently box broad scene
        # textures. Preserve a narrow escape hatch for unreadable poster titles.
        if not detected and source == "poster":
            has_readable_text = max(alpha_lengths, default=0) >= 6
            for box, score, _is_wide, aspect, area_ratio in hits:
                box = np.asarray(box, dtype=np.float32)
                left_margin = float(box[:, 0].min()) / max(1, width)
                right_margin = 1.0 - float(box[:, 0].max()) / max(1, width)
                centre_x = float(box[:, 0].mean()) / max(1, width)
                full_width_title = (
                    has_readable_text
                    and aspect >= 3.0
                    and area_ratio >= 0.10
                    and 0.25 <= centre_x <= 0.75
                )
                if (
                    score >= conf
                    and area_ratio >= 0.03
                    and (
                        (
                            left_margin >= 0.05
                            and right_margin >= 0.05
                        )
                        or full_width_title
                    )
                ):
                    detected = True
                    break
        if debug:
            candidates = []
            image_area = max(1, width * height)
            for box, score in zip(boxes, scores):
                box = np.asarray(box, dtype=np.float32)
                box_width = float(box[:, 0].max() - box[:, 0].min())
                box_height = float(box[:, 1].max() - box[:, 1].min())
                candidates.append(
                    f"{float(score):.3f}/a{box_width / max(1.0, box_height):.2f}"
                    f"/r{(box_width * box_height) / image_area:.4f}"
                    f"/y{float(box[:, 1].mean()) / max(1, height):.2f}"
                )
            best = max((score for _box, score, *_rest in hits), default=0.0)
            logger.info(
                f"text_detect (PP-OCRv5 Mobile): boxes={len(hits)}, "
                f"best={best:.3f}, threshold={conf:.3f}, "
                f"source={source}, centred_lines={centred_lines}, "
                f"candidates=[{', '.join(candidates[:20])}], "
                f"recognised={recognised[:10]} -> {'TEXT' if detected else 'clear'}"
            )
        return detected
    except Exception as exc:
        logger.warning(f"text_detect error; scan unavailable: {exc}")
        return None


def text_column_profile(image, conf: float = _BOX_THRESHOLD):
    """Return a normalised horizontal text-density profile, or None."""
    try:
        boxes, scores, width, height = _detect(image)
        if boxes is None or width <= 0:
            return None
        profile = np.zeros(width, dtype=np.float32)
        hits = _qualifying_boxes(
            boxes, scores, width, height, conf, _SCAN_TOP
        )
        for box, score, _is_wide, _aspect, _area_ratio in hits:
            left = max(0, min(width - 1, int(np.floor(box[:, 0].min()))))
            right = max(left + 1, min(width, int(np.ceil(box[:, 0].max()))))
            profile[left:right] += score
        maximum = float(profile.max())
        if maximum > 0:
            profile /= maximum
        return profile
    except Exception as exc:
        logger.warning(f"text_column_profile error: {exc}")
        return None


if __name__ == "__main__":
    import sys

    from PIL import Image

    logging.basicConfig(level=logging.INFO)
    for path in sys.argv[1:]:
        try:
            result = poster_has_burned_in_text(Image.open(path), debug=True)
            print(f"{path}: {'HAS TEXT' if result else 'clear'}")
        except Exception as exc:
            print(f"{path}: error {exc}")
