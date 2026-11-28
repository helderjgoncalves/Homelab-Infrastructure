"""python -m updater, from updates/. See main.py for the options."""

import sys

from .main import main
from .runtime import DockerError, log

try:
    raise SystemExit(main(sys.argv[1:]))
except DockerError as exc:
    log(str(exc))
    raise SystemExit(1)
