"""Fixed private Invoice bootstrap; OS limits precede site hooks and optional imports."""

import errno
import os
import stat
import sys
from contextlib import redirect_stdout
from pathlib import Path


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
    if sys.platform != "linux" or len(sys.argv) != 7:
        return 70
    try:
        _limits(sys.argv[3:])
    except (ImportError, OSError, ValueError, OverflowError):
        return 70
    try:
        import site

        with redirect_stdout(sys.stderr):
            site.main()
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from coinpup_api.business.invoice_pdf import render_invoice_text_pdf
            from coinpup_api.business.invoice_render_process import CHILD_ERRORS, MAGIC
            from coinpup_api.business.invoice_render_snapshot import MAX_RENDER_SNAPSHOT_BYTES
            from coinpup_api.ledger.errors import MoneyError

            descriptor = os.open("snapshot.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or not 1 <= info.st_size <= MAX_RENDER_SNAPSHOT_BYTES
                ):
                    return 71
                content = stream.read(MAX_RENDER_SNAPSHOT_BYTES + 1)
            Path("snapshot.json").unlink()
            try:
                outputs = tuple(
                    render_invoice_text_pdf(
                        content,
                        expected_sha256=sys.argv[1],
                        language=language,
                        font_directory=sys.argv[2],
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
