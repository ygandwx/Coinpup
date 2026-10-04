"""Private fixed bootstrap: install OS limits before site hooks or processor imports."""

import errno
import json
import os
import stat
import sys
from contextlib import redirect_stdout
from pathlib import Path


def _limits(arguments):
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    for kind, argument in zip(
        (resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_FSIZE, resource.RLIMIT_NOFILE),
        arguments,
        strict=True,
    ):
        requested = int(argument)
        if requested < 1:
            raise ValueError
        _, inherited = resource.getrlimit(kind)
        maximum = requested if inherited == resource.RLIM_INFINITY else min(requested, inherited)
        resource.setrlimit(kind, (maximum, maximum))


def main():
    if sys.platform != "linux" or len(sys.argv) != 6:
        return 70
    try:
        _limits(sys.argv[2:])
    except (ImportError, OSError, ValueError, OverflowError):
        return 70
    try:
        directory = Path.cwd()
        request_path = Path(sys.argv[1])
        if request_path.parent != directory:
            return 71
        descriptor = os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return 71
            payload = stream.read(1048577)
        if len(payload) > 1048576:
            return 71
        request = json.loads(payload.decode("utf-8"))
        if type(request) is not dict:
            return 71
        request_path.unlink()

        # -I -S removes inherited import paths and startup hooks. Trusted installed
        # dependencies are activated only now, after all resource limits are in place.
        import site

        with redirect_stdout(sys.stderr):
            site.main()
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from coinpup_api.ocr.processor import process

            result = process(request, directory)
        if type(result) is not dict:
            return 71
        sys.__stdout__.buffer.write(
            json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        sys.__stdout__.buffer.flush()
        return 0
    except ImportError:
        return 70
    except MemoryError:
        return 72
    except OSError as error:
        return 72 if error.errno in (errno.EFBIG, errno.EMFILE, errno.ENOMEM, errno.ENOSPC) else 71
    except BaseException:
        # A parser exception must not print the input, filesystem paths or a traceback.
        return 71


if __name__ == "__main__":
    raise SystemExit(main())
