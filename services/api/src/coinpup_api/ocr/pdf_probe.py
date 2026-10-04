"""Conservative PDF routing inside the resource-limited child, never text extraction."""

import logging
from dataclasses import dataclass, fields
from io import BytesIO

MAX_SOURCE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class ProbeLimits:
    max_pages: int = 50
    page_tree_entries: int = 512
    page_tree_depth: int = 16
    stream_bytes: int = 8388608
    total_stream_bytes: int = 33554432
    content_streams: int = 128
    operations: int = 20000
    operand_items: int = 50000
    operand_depth: int = 16
    form_invocations: int = 128
    form_depth: int = 12
    indirect_hops: int = 32
    objects: int = 4096
    resource_entries: int = 512
    filters: int = 4

    def __post_init__(self):
        if any(
            type(value := getattr(self, field.name)) is not int or not 1 <= value <= field.default
            for field in fields(self)
        ):
            raise ValueError("Invalid PDF probe limits.")


def manual_result(reason):
    return {"version": 1, "page_count": None, "pages": [], "reason_code": reason}


class _Uncertain(Exception):
    def __init__(self, reason="unsupported_pdf"):
        self.reason = reason
        super().__init__("PDF requires manual review.")


class _Warnings(logging.Handler):
    """Do not format or retain parser messages, which may include original content."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.seen = False

    def emit(self, record):
        self.seen = True


_SHOW = {b"Tj", b"TJ", b"'", b'"'}
_OPERATORS = set(
    b"q Q cm w J j M d ri i gs m l c v y h re S s f F f* B B* b b* n W W* "
    b"BT ET Tc Tw Tz TL Tf Tr Ts Td TD Tm T* Tj TJ ' \" d0 d1 CS cs SC SCN sc scn "
    b"G g RG rg K k sh Do MP DP BMC BDC EMC BX EX".split()
)
_FILTERS = {
    "/FlateDecode",
    "/Fl",
    "/LZWDecode",
    "/LZW",
    "/RunLengthDecode",
    "/RL",
    "/ASCIIHexDecode",
    "/AHx",
    "/ASCII85Decode",
    "/A85",
}


class _Probe:
    def __init__(self, reader, limits, warning, visitor=None):
        from pypdf import generic

        self.g = generic
        self.reader, self.limits, self.warning = reader, limits, warning
        self.visitor, self.images = visitor, []
        self.references = set()
        self.total_bytes = 0
        self.present = False
        self.op_count = self.operand_count = self.calls = 0

    def resolve(self, value):
        seen = set()
        while isinstance(value, self.g.IndirectObject):
            key = (value.idnum, value.generation)
            if key in seen or value.pdf is not self.reader:
                raise _Uncertain("invalid_pdf")
            seen.add(key)
            self.references.add(key)
            if len(seen) > self.limits.indirect_hops or len(self.references) > self.limits.objects:
                raise _Uncertain("probe_limit")
            value = value.get_object()
            if value is None:
                raise _Uncertain("invalid_pdf")
        if len(self.reader.resolved_objects) > self.limits.objects:
            raise _Uncertain("probe_limit")
        return value

    def dictionary(self, value):
        value = self.resolve(value)
        if not isinstance(value, self.g.DictionaryObject):
            raise _Uncertain("invalid_pdf")
        if len(value) > self.limits.resource_entries:
            raise _Uncertain("probe_limit")
        return value

    def features(self, obj):
        if any(
            key in obj
            for key in (
                "/AcroForm",
                "/XFA",
                "/PS",
                "/Ref",
                "/OC",
                "/AA",
                "/F",
                "/FFilter",
                "/FDecodeParms",
                "/OpenAction",
                "/OPI",
            )
        ):
            raise _Uncertain()
        if "/SMask" in obj and self.resolve(obj.get("/SMask")) != "/None":
            raise _Uncertain()
        for key, kind in (
            ("/Annots", self.g.ArrayObject),
            ("/Pattern", self.g.DictionaryObject),
            ("/Properties", self.g.DictionaryObject),
        ):
            value = self.resolve(obj.get(key))
            if value is not None and (not isinstance(value, kind) or value):
                raise _Uncertain()

    def resources(self, value):
        resources = self.dictionary(value) if value is not None else self.g.DictionaryObject()
        self.features(resources)
        states = resources.get("/ExtGState")
        if states is not None:
            for state in self.dictionary(states).values():
                self.features(self.dictionary(state))
        return resources

    def stream_data(self, stream):
        stream = self.resolve(stream)
        if not isinstance(stream, self.g.StreamObject):
            raise _Uncertain("invalid_pdf")
        if any(key in stream for key in ("/F", "/FFilter", "/FDecodeParms")):
            raise _Uncertain()
        filters = self.resolve(stream.get("/Filter"))
        filters = [] if filters is None else (filters if isinstance(filters, list) else [filters])
        if len(filters) > self.limits.filters:
            raise _Uncertain("probe_limit")
        if any(self.resolve(item) not in _FILTERS for item in filters):
            raise _Uncertain()
        params = self.resolve(stream.get("/DecodeParms"))
        if params is not None:
            params = params if isinstance(params, list) else [params]
            if len(params) != len(filters):
                raise _Uncertain("invalid_pdf")
            for item in params:
                item = self.resolve(item)
                if not isinstance(item, (self.g.DictionaryObject, self.g.NullObject)):
                    raise _Uncertain("invalid_pdf")
                self.operands(item)
        data = stream.get_data()
        self.total_bytes += len(data)
        if (
            len(data) > self.limits.stream_bytes
            or self.total_bytes > self.limits.total_stream_bytes
        ):
            raise _Uncertain("probe_limit")
        return data

    def operands(self, value, depth=0):
        self.operand_count += 1
        if depth > self.limits.operand_depth or self.operand_count > self.limits.operand_items:
            raise _Uncertain("probe_limit")
        if isinstance(value, self.g.IndirectObject):
            raise _Uncertain("invalid_pdf")
        if isinstance(value, dict):
            if "/ActualText" in value or "/Alt" in value:
                raise _Uncertain()
            for item in value.values():
                self.operands(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                self.operands(item, depth + 1)

    def walk(self, contents, resources, active=(), depth=0):
        if depth > self.limits.form_depth:
            raise _Uncertain("probe_limit")
        contents = self.resolve(contents)
        if contents is None or isinstance(contents, self.g.NullObject):
            return
        streams = contents if isinstance(contents, list) else [contents]
        if len(streams) > self.limits.content_streams:
            raise _Uncertain("probe_limit")
        chunks, length = [], 0
        for stream in streams:
            data = self.stream_data(stream)
            length += len(data) + bool(chunks)
            if length > self.limits.stream_bytes:
                raise _Uncertain("probe_limit")
            chunks.append(data)
        combined = self.g.DecodedStreamObject()
        # A private sentinel exposes trailing operands pypdf otherwise silently discards.
        combined.set_data(b"\n".join(chunks) + b"\nCoinpupProbeEnd\n")
        # pypdf materializes operations; byte limits precede it, OS limits bound allocation.
        operations = self.g.ContentStream(combined, self.reader).operations
        self.present |= any(operator in _SHOW for _, operator in operations)
        if not operations or operations.pop() != ([], b"CoinpupProbeEnd"):
            raise _Uncertain("invalid_pdf")
        self.op_count += len(operations)
        if self.op_count > self.limits.operations:
            raise _Uncertain("probe_limit")
        states = []
        closes = {b"Q": b"q", b"ET": b"BT", b"EX": b"BX", b"EMC": b"BMC"}
        for operands, operator in operations:
            self.operands(operands)
            if operator in (b"q", b"BT", b"BX", b"BMC", b"BDC"):
                key = b"BMC" if operator == b"BDC" else operator
                if operator == b"BT" and key in states:
                    raise _Uncertain("invalid_pdf")
                states.append(key)
            if operator in closes:
                if not states or states.pop() != closes[operator]:
                    raise _Uncertain("invalid_pdf")
            if operator == b"INLINE IMAGE":
                # pypdf's EI recovery is ambiguous; do not certify absence after it.
                raise _Uncertain()
            if operator not in _OPERATORS:
                raise _Uncertain()
            if operator == b"sh":
                raise _Uncertain()  # Shading/function interpretation is not part of this probe.
            if operator == b"gs":
                if len(operands) != 1 or not isinstance(operands[0], self.g.NameObject):
                    raise _Uncertain("invalid_pdf")
                states_by_name = self.dictionary(resources.get("/ExtGState"))
                self.features(self.dictionary(states_by_name.get(operands[0])))
            if operator == b"Do":
                if len(operands) != 1 or not isinstance(operands[0], self.g.NameObject):
                    raise _Uncertain("invalid_pdf")
                objects = self.dictionary(resources.get("/XObject"))
                target = self.dictionary(objects.get(operands[0]))
                self.features(target)
                if not isinstance(target, self.g.StreamObject):
                    raise _Uncertain("invalid_pdf")
                subtype = self.resolve(target.get("/Subtype"))
                if subtype == "/Image":
                    if self.visitor is not None:
                        self.images.append((target, resources))
                    continue  # Raster samples cannot contain PDF text operators.
                if subtype != "/Form" or self.resolve(target.get("/FormType", 1)) != 1:
                    raise _Uncertain()
                self.calls += 1
                if self.calls > self.limits.form_invocations:
                    raise _Uncertain("probe_limit")
                if id(target) in active:
                    raise _Uncertain("invalid_pdf")
                nested = self.resources(target.get("/Resources", resources))
                self.walk(target, nested, (*active, id(target)), depth + 1)
        if states:
            raise _Uncertain("invalid_pdf")

    def page(self, page, index):
        self.present = False
        self.images = []
        self.op_count = self.operand_count = self.calls = 0
        reason = None
        try:
            self.features(page)
            resources = self.resources(page.get("/Resources"))
            self.walk(page.get("/Contents"), resources)
            if self.visitor is not None and not self.present and not self.warning.seen:
                self.visitor(page, index, self.images, self)
        except _Uncertain as error:
            reason = error.reason
        except self.limit_error:
            reason = "probe_limit"
        except MemoryError:
            raise
        except Exception:
            reason = "invalid_pdf"
        if self.warning.seen:
            reason = "parser_warning"
        layer = "present" if self.present else ("unknown" if reason else "absent")
        return {
            "page_index": index,
            "layer": layer,
            "route": "manual" if reason else ("extract" if self.present else "render"),
            "reason_code": reason,
        }


_DEFAULT_LIMITS = ProbeLimits()


def probe_pdf(data: bytes, limits: ProbeLimits = _DEFAULT_LIMITS, *, _visitor=None) -> dict:
    """Only a complete supported traversal can certify absence; never use extracted text."""
    from pypdf import Configuration, PdfReader, apply_configuration
    from pypdf.errors import FileNotDecryptedError, LimitReachedError

    if type(data) is not bytes or not 0 < len(data) <= MAX_SOURCE_BYTES:
        return manual_result("source_invalid")
    if not isinstance(limits, ProbeLimits):
        return manual_result("request_invalid")
    warning = _Warnings()
    logger = logging.getLogger("pypdf")
    handlers, propagate, level = logger.handlers, logger.propagate, logger.level
    logger.handlers, logger.propagate, logger.level = [warning], False, logging.WARNING
    configuration = Configuration(
        maximum_declared_stream_length=limits.stream_bytes,
        array_based_stream_maximum_output_length=limits.stream_bytes,
        zlib_maximum_output_length=limits.stream_bytes,
        lzw_maximum_output_length=limits.stream_bytes,
        run_length_maximum_output_length=limits.stream_bytes,
        zlib_maximum_recovery_input_length=limits.stream_bytes,
        flate_maximum_columns=limits.stream_bytes,
        flate_maximum_row_length=limits.stream_bytes,
        image_maximum_buffer_size=limits.stream_bytes,
        jbig2_maximum_output_length=limits.stream_bytes,
        page_tree_maximum_entries=limits.page_tree_entries,
        page_tree_maximum_depth=limits.page_tree_depth,
        jbig2dec_binary=None,
        disable_legacy_handling=True,
    )
    try:
        with apply_configuration(configuration), BytesIO(data) as stream:
            reader = PdfReader(stream, strict=True, root_object_recovery_limit=0)
            if reader.is_encrypted:
                return manual_result("encrypted_pdf")
            probe = _Probe(reader, limits, warning, _visitor)
            probe.limit_error = LimitReachedError
            probe.features(probe.dictionary(reader.trailer.get("/Root")))
            pages = reader.pages
            count = len(pages)
            if not 0 < count <= limits.max_pages:
                return manual_result("probe_limit")
            if warning.seen:
                return manual_result("parser_warning")
            results = [probe.page(page, index) for index, page in enumerate(pages)]
            if warning.seen:
                for result in results:
                    result.update(route="manual", reason_code="parser_warning")
                    if result["layer"] == "absent":
                        result["layer"] = "unknown"
            return {
                "version": 1,
                "page_count": count,
                "pages": results,
                "reason_code": None,
            }
    except FileNotDecryptedError:
        return manual_result("encrypted_pdf")
    except LimitReachedError:
        return manual_result("probe_limit")
    except _Uncertain as error:
        return manual_result(error.reason)
    except MemoryError:
        raise
    except Exception:
        return manual_result("invalid_pdf")
    finally:
        logger.handlers, logger.propagate, logger.level = handlers, propagate, level
