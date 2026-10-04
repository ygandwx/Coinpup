"""Native binding/ownership contracts without importing optional OCR dependencies."""

import ctypes
import json
from types import SimpleNamespace

import pytest
from coinpup_api.ocr import engine_adapters as engines


def rejected(reason):
    return pytest.raises(
        engines.EngineError,
        match="OCR engine operation failed.",
        check=lambda e: e.reason == reason,
    )


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


@pytest.fixture
def tess(monkeypatch):
    calls, buffers = [], []
    state = SimpleNamespace(
        lines=[(b"Total: +001,234.50\n\n", (0, 0, 8, 2))],
        index=0,
        languages=[b"eng", b"chi_sim", b"chi_tra"],
        status=0,
        init=0,
    )

    def pointer(value):
        buffer = ctypes.create_string_buffer(value)
        buffers.append(buffer)
        return ctypes.addressof(buffer)

    def invoke(name, *args):
        calls.append((name, args))
        if name == "TessVersion":
            return pointer(b"5.5.3")
        if name == "TessBaseAPICreate":
            return 123
        if name == "TessBaseAPIInit3":
            return state.init
        if name == "TessBaseAPIGetLoadedLanguagesAsVector":
            if state.languages is None:
                return None
            array = (ctypes.c_void_p * (len(state.languages) + 1))(
                *[pointer(text) for text in state.languages], None
            )
            buffers.append(array)
            return array
        if name == "TessBaseAPIRecognize":
            state.index = 0
            return state.status
        if name in ("TessBaseAPIGetIterator", "TessResultIteratorGetPageIterator"):
            return 234 if state.lines else None
        if name == "TessResultIteratorGetUTF8Text":
            return pointer(state.lines[state.index][0])
        if name == "TessPageIteratorBoundingBox":
            for target, value in zip(args[2:], state.lines[state.index][1], strict=True):
                ctypes.cast(target, ctypes.POINTER(ctypes.c_int))[0] = value
            return 1
        if name == "TessResultIteratorNext":
            state.index += 1
            return state.index < len(state.lines)

    class Library:
        def __getattr__(self, name):
            function = Function(lambda *args: invoke(name, *args))
            setattr(self, name, function)
            return function

    library = Library()
    monkeypatch.setattr(engines.sys, "platform", "linux")
    monkeypatch.setattr(engines.ctypes, "CDLL", lambda path: library)
    return state, calls, library


@pytest.mark.parametrize("model_set", ["fast", "best"])
@pytest.mark.parametrize("psm", [3, 6, 11])
def test_real_capi_contract_keeps_model_loaded_and_releases_every_page(
    tess, tmp_path, model_set, psm
):
    state, calls, library = tess
    state.lines += [("日期: 2037-01-02\n".encode(), (0, 3, 8, 5))]
    profile = {"engine": "tesseract", "model_set": model_set, "psm": psm}
    with engines.create_adapter(profile, tmp_path) as adapter:
        first = adapter.recognize(bytes(8 * 5 * 3), 8, 5)
        assert adapter.recognize(bytes(8 * 5 * 3), 8, 5) == first
        assert first.text == "Total: +001,234.50\n日期: 2037-01-02"
        assert first.words[0].bbox == (0, 0, 8, 2)
        assert adapter.metadata["loaded_languages"] == ["eng", "chi_sim", "chi_tra"]
        assert str(tmp_path) not in json.dumps(adapter.metadata)
        adapter.metadata["profile"]["psm"] = 99
        assert adapter.metadata["profile"] == profile
    adapter.close()
    names = [name for name, _ in calls]
    assert names.count("TessBaseAPIInit3") == names.count("TessBaseAPIDelete") == 1
    assert names.count("TessDeleteText") == 4
    assert names.count("TessResultIteratorDelete") == names.count("TessBaseAPIClear") == 2
    assert names[-2:] == ["TessBaseAPIEnd", "TessBaseAPIDelete"]
    assert library.TessResultIteratorGetUTF8Text.restype is ctypes.c_void_p
    assert (
        next(args for name, args in calls if name == "TessBaseAPIInit3")[2]
        == b"eng+chi_sim+chi_tra"
    )
    assert next(args for name, args in calls if name == "TessBaseAPISetPageSegMode") == (123, psm)
    assert all(args[1] == 2 for name, args in calls if name == "TessResultIteratorGetUTF8Text")
    with rejected("engine_unavailable"):
        adapter.recognize(bytes(120), 8, 5)


@pytest.mark.parametrize("language_names", [None, [b"eng"], [b"eng", b"chi_sim"]])
def test_partial_successful_initialization_is_rejected_and_disposed(tess, tmp_path, language_names):
    state, calls, _ = tess
    state.languages = language_names
    with rejected("engine_initialization_failed"):
        engines.create_adapter({"engine": "tesseract", "model_set": "fast", "psm": 3}, tmp_path)
    names = [name for name, _ in calls]
    assert names[-2:] == ["TessBaseAPIEnd", "TessBaseAPIDelete"]
    assert names.count("TessDeleteTextArray") == int(language_names is not None)


@pytest.mark.parametrize(
    "text,box,reason",
    [
        (b"bad\xff", (0, 0, 8, 5), "engine_result_invalid"),
        (b"a" * 2053, (0, 0, 8, 5), "engine_limit"),
        (b"bad\x01text", (0, 0, 8, 5), "engine_result_invalid"),
        (b"text", (-1, 0, 8, 5), "engine_result_invalid"),
    ],
)
def test_failed_line_always_frees_text_iterator_and_page(tess, tmp_path, text, box, reason):
    state, calls, _ = tess
    state.lines = [(text, box)]
    with engines.create_adapter(
        {"engine": "tesseract", "model_set": "best", "psm": 6}, tmp_path
    ) as adapter:
        with rejected(reason):
            adapter.recognize(bytes(120), 8, 5)
        names = [name for name, _ in calls]
        assert names[-2:] == ["TessResultIteratorDelete", "TessBaseAPIClear"]
        assert names.count("TessDeleteText") == 1


@pytest.mark.parametrize("failure", [False, True])
def test_empty_page_and_failed_recognition_both_clear_native_page(tess, tmp_path, failure):
    state, calls, _ = tess
    state.lines, state.status = [], int(failure)
    with engines.create_adapter(
        {"engine": "tesseract", "model_set": "fast", "psm": 3}, tmp_path
    ) as adapter:
        if failure:
            with rejected("engine_recognition_failed"):
                adapter.recognize(bytes(120), 8, 5)
        else:
            page = adapter.recognize(bytes(120), 8, 5)
            assert page.words == () and page.text == ""
        assert calls[-1][0] == "TessBaseAPIClear"


def test_shared_parser_total_text_budget_counts_transcript_and_words():
    text = "x" * 2048
    assert len(engines._page(((text, (0, 0, 1, 1)) for _ in range(31)), 1, 1).words) == 31
    with rejected("engine_limit"):
        engines._page(((text, (0, 0, 1, 1)) for _ in range(32)), 1, 1)


@pytest.fixture
def paddle(monkeypatch):
    calls = []
    state = SimpleNamespace(
        texts=["Total: - 10.00", "Date: 2037-01-02"],
        boxes=[[[0, 3], [8, 3], [8, 5], [0, 5]], [[0, 0], [8, 0], [8, 2], [0, 2]]],
        fail=False,
    )

    class Array:
        def __init__(self, raw):
            self.raw = raw

        def reshape(self, *shape):
            assert shape == (5, 8, 3)
            return self

        def __getitem__(self, indices):
            assert indices == (slice(None), slice(None), slice(None, None, -1))
            return Array(b"".join(self.raw[i : i + 3][::-1] for i in range(0, len(self.raw), 3)))

    class Pipeline:
        def predict_iter(self, image):
            calls.append(("image", image.raw))
            try:
                if state.fail:
                    raise RuntimeError("fictional SDK private input")
                yield {"rec_texts": state.texts, "rec_polys": state.boxes}
            finally:
                calls.append(("generator_closed", None))

        def close(self):
            calls.append(("closed", None))

    def create(**arguments):
        calls.append(("create", arguments))
        return Pipeline()

    modules = {
        name: SimpleNamespace(__version__=version)
        for name, version in {
            "paddle": "3.4.0",
            "paddlex": "3.7.0",
            "paddleocr": "3.7.0",
            "cv2": "4.10.0",
        }.items()
    }
    modules["paddleocr"].PaddleOCR = create
    modules["cv2"].setNumThreads = lambda count: calls.append(("threads", count))
    modules["cv2"].getNumThreads = lambda: 1
    modules["numpy"] = SimpleNamespace(
        uint8=object(),
        frombuffer=lambda raw, dtype: Array(raw),
        ascontiguousarray=lambda value: value,
    )
    monkeypatch.setattr(engines.importlib, "import_module", lambda name: modules[name])
    monkeypatch.setattr(engines.sys, "platform", "linux")
    return state, calls


def test_paddle_public_line_pairs_bgr_and_single_initialization(paddle, tmp_path):
    _, calls = paddle
    with engines.create_adapter({"engine": "paddle"}, tmp_path) as adapter:
        for _ in range(2):
            page = adapter.recognize(bytes([1, 2, 3]) * 40, 8, 5)
            assert page.text == "Date: 2037-01-02\nTotal: - 10.00"
            assert page.words[1].bbox == (0, 3, 8, 5)
        assert str(tmp_path) not in json.dumps(adapter.metadata)
    adapter.close()
    assert [v for k, v in calls if k == "image"] == [bytes([3, 2, 1]) * 40] * 2
    assert len([1 for k, _ in calls if k == "create"]) == 1
    assert len([1 for k, _ in calls if k == "closed"]) == 1
    params = next(v for k, v in calls if k == "create")
    assert params["enable_mkldnn"] is False and params["precision"] == "fp32"
    assert params["cpu_threads"] == 1 and params["return_word_box"] is False
    assert not any(
        params[key]
        for key in (
            "enable_hpi",
            "use_doc_orientation_classify",
            "use_doc_unwarping",
            "use_textline_orientation",
        )
    )
    assert params["paddlex_config"]["SubModules"]["TextDetection"]["max_side_limit"] == 4000


@pytest.mark.parametrize("kind", ["mismatch", "nan", "outside", "triangle", "too_many", "sdk"])
def test_bad_paddle_results_fail_without_raw_errors_and_close_generator(paddle, tmp_path, kind):
    state, calls = paddle
    if kind == "mismatch":
        state.boxes.pop()
    if kind == "nan":
        state.boxes[0][0][0] = float("nan")
    if kind == "outside":
        state.boxes[0][0][0] = -1
    if kind == "triangle":
        state.boxes[0].pop()
    if kind == "too_many":
        state.texts, state.boxes = ["x"] * 2001, [state.boxes[0]] * 2001
    if kind == "sdk":
        state.fail = True
    reason = (
        "engine_limit"
        if kind == "too_many"
        else "engine_recognition_failed"
        if kind == "sdk"
        else "engine_result_invalid"
    )
    with engines.create_adapter({"engine": "paddle"}, tmp_path) as adapter:
        with rejected(reason):
            adapter.recognize(bytes(120), 8, 5)
    assert ("generator_closed", None) in calls and calls[-1] == ("closed", None)


@pytest.mark.parametrize(
    "profile",
    [
        None,
        {},
        {"engine": "unknown"},
        {"engine": "paddle", "psm": 3},
        {"engine": "tesseract", "model_set": "fast", "psm": True},
        {"engine": "tesseract", "model_set": "fast", "psm": 1},
        {"engine": "tesseract", "model_set": "../best", "psm": 3},
    ],
)
def test_profiles_are_closed_and_validated_before_import(profile, tmp_path):
    with rejected("engine_profile_invalid"):
        engines.create_adapter(profile, tmp_path)


def test_optional_import_failure_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(engines.sys, "platform", "linux")

    def unavailable(_):
        raise ImportError("fictional private dependency path")

    monkeypatch.setattr(engines.importlib, "import_module", unavailable)
    with rejected("engine_unavailable") as caught:
        engines.create_adapter({"engine": "paddle"}, tmp_path)
    assert caught.value.__cause__ is None
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "rgb,width,height,reason",
    [
        (b"", 8, 5, "engine_result_invalid"),
        (bytes(120), True, 5, "engine_result_invalid"),
        (bytes(120), 0, 5, "engine_result_invalid"),
        (b"", 10001, 1, "engine_limit"),
        (b"", 5000, 5000, "engine_limit"),
    ],
)
def test_raster_bounds_reject_before_native_recognition(tess, tmp_path, rgb, width, height, reason):
    _, calls, _ = tess
    with engines.create_adapter(
        {"engine": "tesseract", "model_set": "fast", "psm": 3}, tmp_path
    ) as adapter:
        with rejected(reason):
            adapter.recognize(rgb, width, height)
    assert not any(name == "TessBaseAPISetImage" for name, _ in calls)
