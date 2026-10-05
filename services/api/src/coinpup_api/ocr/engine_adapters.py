"""Private raster-to-text adapters; loaded only inside the isolated candidate process."""

import ctypes
import importlib
import math
import os
import re
import sys
import unicodedata
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from .parser_types import ParserLimits, TextPage, Word

_LIMITS = ParserLimits()
_LANGUAGES = ["eng", "chi_sim", "chi_tra"]


class EngineError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("OCR engine operation failed.")


def _require(valid, reason="engine_result_invalid"):
    if not valid:
        raise EngineError(reason) from None


def _cstring(address, limit):
    _require(bool(address))
    data = ctypes.cast(address, ctypes.POINTER(ctypes.c_ubyte))
    result = bytearray()
    for index in range(limit + 1):
        if not data[index]:
            try:
                return result.decode("utf8")
            except UnicodeError:
                raise EngineError("engine_result_invalid") from None
        _require(index < limit, "engine_limit")
        result.append(data[index])


def _transcript(words):
    """Readable source rows; original boxes remain the parser's only geometry."""
    fallback = "\n".join(word.text for word in words)
    rows = []
    for word in words:
        if rows:
            anchor = rows[-1][0].bbox
            overlap = min(anchor[3], word.bbox[3]) - max(anchor[1], word.bbox[1])
            height = min(anchor[3] - anchor[1], word.bbox[3] - word.bbox[1])
            if overlap >= height / 2:
                rows[-1].append(word)
                continue
            if overlap > 0:
                return fallback
        rows.append([word])
    for row in rows:
        row.sort(key=lambda word: (word.bbox[0], word.bbox[2]))
        if any(left.bbox[2] > right.bbox[0] for left, right in zip(row, row[1:], strict=False)):
            return fallback
    return "\n".join(" ".join(word.text for word in row) for row in rows)


def _page(lines, width, height):
    words, size = [], 0
    for index, (text, box) in enumerate(lines):
        _require(index < _LIMITS.words_per_page, "engine_limit")
        _require(type(text) is str)
        _require(len(text) <= _LIMITS.word_bytes, "engine_limit")
        _require(bool(text.strip()) and not any(unicodedata.category(c) == "Cc" for c in text))
        try:
            count = len(text.encode("utf8"))
        except UnicodeError:
            raise EngineError("engine_result_invalid") from None
        _require(count <= _LIMITS.word_bytes, "engine_limit")
        size += count
        # The shared parser budgets both Word text and the complete page transcript.
        _require(2 * size + len(words) <= _LIMITS.text_bytes, "engine_limit")
        _require(len(box) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in box))
        x0, y0, x1, y1 = box
        _require(0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height)
        words.append(Word(text, tuple(box)))
    words.sort(key=lambda word: (word.bbox[1], word.bbox[0], word.bbox[3], word.bbox[2]))
    return TextPage(width, height, tuple(words), _transcript(words))


class Adapter:
    @property
    def metadata(self):
        return deepcopy(self._metadata)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def recognize(self, rgb_bytes, width, height):
        _require(not self._closed, "engine_unavailable")
        _require(type(width) is int and type(height) is int and min(width, height) > 0)
        _require(max(width, height) <= 10000 and width * height <= 20000000, "engine_limit")
        _require(type(rgb_bytes) is bytes and len(rgb_bytes) == width * height * 3)
        try:
            with closing(self._lines(rgb_bytes, width, height)) as lines:
                return _page(lines, width, height)
        except EngineError:
            raise
        except MemoryError:
            raise EngineError("engine_limit") from None
        except Exception:
            raise EngineError("engine_recognition_failed") from None

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._dispose()
            except Exception:
                raise EngineError("engine_recognition_failed") from None


class _Tesseract(Adapter):
    def __init__(self, profile, assets_dir):
        self._closed, self.handle = False, None
        self.library = ctypes.CDLL("/opt/tesseract/lib/libtesseract.so.5.5.3")
        pointer, integer = ctypes.c_void_p, ctypes.c_int
        vector = ctypes.POINTER(pointer)
        definitions = {
            "TessVersion": (pointer, []),
            "TessBaseAPICreate": (pointer, []),
            "TessBaseAPIInit3": (integer, [pointer, ctypes.c_char_p, ctypes.c_char_p]),
            "TessBaseAPIGetLoadedLanguagesAsVector": (vector, [pointer]),
            "TessDeleteTextArray": (None, [vector]),
            "TessBaseAPISetPageSegMode": (None, [pointer, integer]),
            "TessBaseAPISetImage": (None, [pointer, pointer, integer, integer, integer, integer]),
            "TessBaseAPISetSourceResolution": (None, [pointer, integer]),
            "TessBaseAPIRecognize": (integer, [pointer, pointer]),
            "TessBaseAPIGetIterator": (pointer, [pointer]),
            "TessResultIteratorGetPageIterator": (pointer, [pointer]),
            "TessResultIteratorGetUTF8Text": (pointer, [pointer, integer]),
            "TessPageIteratorBoundingBox": (
                integer,
                [pointer, integer, *[ctypes.POINTER(integer)] * 4],
            ),
            "TessResultIteratorNext": (integer, [pointer, integer]),
            **{
                name: (None, [pointer])
                for name in (
                    "TessDeleteText",
                    "TessResultIteratorDelete",
                    "TessBaseAPIClear",
                    "TessBaseAPIEnd",
                    "TessBaseAPIDelete",
                )
            },
        }
        for name, (result, arguments) in definitions.items():
            function = getattr(self.library, name)
            function.restype, function.argtypes = result, arguments
        try:
            version = _cstring(self.library.TessVersion(), 64)
            _require(version == "5.5.3", "engine_initialization_failed")
            self.handle = self.library.TessBaseAPICreate()
            _require(bool(self.handle), "engine_initialization_failed")
            _require(
                self.library.TessBaseAPIInit3(
                    self.handle,
                    os.fsencode(assets_dir / "tesseract" / profile["model_set"]),
                    b"eng+chi_sim+chi_tra",
                )
                == 0,
                "engine_initialization_failed",
            )
            languages = self._languages()
            self.library.TessBaseAPISetPageSegMode(self.handle, profile["psm"])
            self._metadata = {
                "profile": profile,
                "versions": {"tesseract": version},
                "loaded_languages": languages,
                "parameters": {"languages": _LANGUAGES, "dpi": 300, "level": "textline"},
            }
        except BaseException:
            self.close()
            raise

    def _languages(self):
        values = self.library.TessBaseAPIGetLoadedLanguagesAsVector(self.handle)
        _require(bool(values), "engine_initialization_failed")
        try:
            names = []
            for index in range(16):
                if not values[index]:
                    break
                name = _cstring(values[index], 63)
                _require(bool(re.fullmatch(r"[A-Za-z0-9_]+", name)) and name not in names)
                names.append(name)
            else:
                raise EngineError("engine_initialization_failed")
            _require(set(_LANGUAGES) <= set(names), "engine_initialization_failed")
            return names
        finally:
            self.library.TessDeleteTextArray(values)

    def _lines(self, rgb, width, height):
        library, iterator = self.library, None
        try:
            library.TessBaseAPISetImage(
                self.handle, ctypes.c_char_p(rgb), width, height, 3, width * 3
            )
            library.TessBaseAPISetSourceResolution(self.handle, 300)
            _require(
                library.TessBaseAPIRecognize(self.handle, None) == 0, "engine_recognition_failed"
            )
            iterator = library.TessBaseAPIGetIterator(self.handle)
            if not iterator:
                return
            page_iterator = library.TessResultIteratorGetPageIterator(iterator)
            _require(bool(page_iterator))
            for _ in range(_LIMITS.words_per_page + 1):
                text_pointer = library.TessResultIteratorGetUTF8Text(iterator, 2)
                if text_pointer:
                    try:
                        # Remove only API-added line/paragraph terminators.
                        text = _cstring(text_pointer, _LIMITS.word_bytes + 4).rstrip("\r\n")
                    finally:
                        library.TessDeleteText(text_pointer)
                    if text:
                        box = [ctypes.c_int() for _ in range(4)]
                        _require(
                            library.TessPageIteratorBoundingBox(
                                page_iterator, 2, *(ctypes.byref(value) for value in box)
                            )
                        )
                        yield text, tuple(value.value for value in box)
                if not library.TessResultIteratorNext(iterator, 2):
                    return
            raise EngineError("engine_limit")
        finally:
            try:
                if iterator:
                    library.TessResultIteratorDelete(iterator)
            finally:
                library.TessBaseAPIClear(self.handle)

    def _dispose(self):
        if self.handle:
            try:
                self.library.TessBaseAPIEnd(self.handle)
            finally:
                self.library.TessBaseAPIDelete(self.handle)


class _Paddle(Adapter):
    def __init__(self, profile, assets_dir):
        self._closed, self.pipeline = False, None
        modules = {
            name: importlib.import_module(name)
            for name in ("paddle", "paddlex", "paddleocr", "cv2")
        }
        self.numpy = importlib.import_module("numpy")
        versions = {name: module.__version__ for name, module in modules.items()}
        _require(
            versions
            == {"paddle": "3.4.0", "paddlex": "3.7.0", "paddleocr": "3.7.0", "cv2": "4.10.0"},
            "engine_initialization_failed",
        )
        modules["cv2"].setNumThreads(1)
        parameters = {
            "device": "cpu",
            "cpu_threads": 1,
            "enable_hpi": False,
            "enable_mkldnn": False,
            "precision": "fp32",
            "enable_cinn": False,
            "use_tensorrt": False,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "text_recognition_batch_size": 6,
            "text_det_limit_side_len": 64,
            "text_det_limit_type": "min",
            "text_det_thresh": 0.3,
            "text_det_box_thresh": 0.6,
            "text_det_unclip_ratio": 1.5,
            "text_rec_score_thresh": 0.0,
            "return_word_box": False,
        }
        config = {
            "pipeline_name": "OCR",
            "text_type": "general",
            "use_doc_preprocessor": False,
            "use_textline_orientation": False,
            "SubModules": {
                "TextDetection": {"module_name": "text_detection", "max_side_limit": 4000},
                "TextRecognition": {"module_name": "text_recognition"},
            },
        }
        try:
            self.pipeline = modules["paddleocr"].PaddleOCR(
                **parameters,
                paddlex_config=config,
                text_detection_model_name="PP-OCRv6_small_det",
                text_detection_model_dir=str(assets_dir / "models/det"),
                text_recognition_model_name="PP-OCRv6_small_rec",
                text_recognition_model_dir=str(assets_dir / "models/rec"),
            )
            _require(modules["cv2"].getNumThreads() == 1, "engine_initialization_failed")
            self._metadata = {
                "profile": profile,
                "versions": versions,
                "parameters": {
                    **parameters,
                    "text_det_max_side_limit": 4000,
                    "models": ["PP-OCRv6_small_det", "PP-OCRv6_small_rec"],
                },
            }
        except BaseException:
            self.close()
            raise

    def _lines(self, rgb, width, height):
        image = self.numpy.frombuffer(rgb, dtype=self.numpy.uint8).reshape(height, width, 3)
        bgr = self.numpy.ascontiguousarray(image[:, :, ::-1])
        with closing(self.pipeline.predict_iter(bgr)) as results:
            result = next(results)
            _require(next(results, None) is None)
            texts, boxes = result["rec_texts"], result["rec_polys"]
            _require(len(texts) == len(boxes))
            _require(len(texts) <= _LIMITS.words_per_page, "engine_limit")
            for text, polygon in zip(texts, boxes, strict=True):
                points = polygon.tolist() if hasattr(polygon, "tolist") else polygon
                _require(len(points) == 4 and all(len(point) == 2 for point in points))
                _require(
                    all(
                        type(v) in (int, float) and math.isfinite(v)
                        for point in points
                        for v in point
                    )
                )
                _require(all(0 <= x <= width and 0 <= y <= height for x, y in points))
                if text != "":
                    yield (
                        text,
                        (
                            min(p[0] for p in points),
                            min(p[1] for p in points),
                            max(p[0] for p in points),
                            max(p[1] for p in points),
                        ),
                    )

    def _dispose(self):
        if self.pipeline is not None:
            self.pipeline.close()


def create_adapter(profile: dict, assets_dir: Path) -> Adapter:
    _require(type(profile) is dict, "engine_profile_invalid")
    engine = profile.get("engine")
    valid = (profile == {"engine": "paddle"}) or (
        engine == "tesseract"
        and profile.keys() == {"engine", "model_set", "psm"}
        and profile["model_set"] in ("fast", "best")
        and type(profile["psm"]) is int
        and profile["psm"] in (3, 6, 11)
    )
    _require(valid, "engine_profile_invalid")
    _require(sys.platform == "linux", "engine_unavailable")
    try:
        return (_Tesseract if engine == "tesseract" else _Paddle)(
            deepcopy(profile), Path(assets_dir)
        )
    except EngineError:
        raise
    except ImportError:
        raise EngineError("engine_unavailable") from None
    except MemoryError:
        raise EngineError("engine_limit") from None
    except Exception:
        raise EngineError("engine_initialization_failed") from None
