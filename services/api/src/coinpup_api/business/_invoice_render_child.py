"""Fixed private Invoice bootstrap; OS limits precede site hooks and optional imports."""

import errno
import os
import stat
import sys
from contextlib import redirect_stdout
from pathlib import Path


def _read_private(name, maximum):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= maximum:
            raise ValueError
        content = stream.read(maximum + 1)
    Path(name).unlink()
    return content


def _limits(arguments):
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    for kind, text in zip(
        (resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_FSIZE, resource.RLIMIT_NOFILE),
        arguments,
        strict=True,
    ):
        value = int(text)
        if value < 1:
            raise ValueError
        inherited = resource.getrlimit(kind)
        maximum = min([value] + [bound for bound in inherited if bound != resource.RLIM_INFINITY])
        resource.setrlimit(kind, (maximum, maximum))


def main():
    checking_logo = len(sys.argv) == 9 and sys.argv[1] == "--check-logo"
    if sys.platform != "linux" or (not checking_logo and len(sys.argv) != 7):
        return 70
    try:
        _limits(sys.argv[-4:])
    except (ImportError, OSError, ValueError, OverflowError):
        return 70
    try:
        import site

        with redirect_stdout(sys.stderr):
            site.main()
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from coinpup_api.business.invoice_logo import (
                MAX_LOGO_BYTES,
                DeterministicInvoiceLogoError,
                decode_invoice_logo,
            )
            from coinpup_api.business.invoice_render_process import (
                CHILD_ERRORS,
                INVALID_LOGO,
                MAGIC,
            )
            from coinpup_api.ledger.errors import MoneyError

            if checking_logo:
                _, media_type, digest, size = sys.argv[1:5]
                try:
                    decode_invoice_logo(
                        _read_private("logo.bin", MAX_LOGO_BYTES),
                        {"media_type": media_type, "sha256": digest, "byte_size": int(size)},
                    )
                except DeterministicInvoiceLogoError:
                    return INVALID_LOGO
                except MoneyError as error:
                    return next(
                        (code for code, name in CHILD_ERRORS.items() if name == error.code), 71
                    )
                return 0

            from coinpup_api.business.invoice_pdf import render_invoice_text_pdf
            from coinpup_api.business.invoice_render_snapshot import MAX_RENDER_SNAPSHOT_BYTES

            content = _read_private("snapshot.json", MAX_RENDER_SNAPSHOT_BYTES)
            try:
                logo_content = _read_private("logo.bin", MAX_LOGO_BYTES)
            except FileNotFoundError:
                logo_content = None
            try:
                outputs = tuple(
                    render_invoice_text_pdf(
                        content,
                        expected_sha256=sys.argv[1],
                        language=language,
                        font_directory=sys.argv[2],
                        logo_content=logo_content,
                    )
                    for language in ("zh", "en")
                )
            except MoneyError as error:
                return next((code for code, name in CHILD_ERRORS.items() if name == error.code), 71)
        import hashlib

        # Emit nothing until both renders complete; the parent verifies the complete frame.
        header = MAGIC + bytes.fromhex(sys.argv[1])
        header += b"".join(len(output).to_bytes(4, "big") for output in outputs)
        header += b"".join(hashlib.sha256(output).digest() for output in outputs)
        sys.__stdout__.buffer.write(header)
        for output in outputs:
            sys.__stdout__.buffer.write(output)
        sys.__stdout__.buffer.flush()
        return 0
    except ImportError:
        return 70
    except MemoryError:
        return 72
    except OSError as error:
        return 72 if error.errno in (errno.EFBIG, errno.EMFILE, errno.ENOMEM, errno.ENOSPC) else 71
    except BaseException:
        return 71


if __name__ == "__main__":
    raise SystemExit(main())
