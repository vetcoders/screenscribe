"""Git commit provenance for ``screenscribe --version``.

Wheels built from a git checkout carry values baked by the ``hatch_build.py``
build hook; the defaults here mark a build without provenance (e.g. a wheel
rebuilt from an sdist), in which case the CLI probes the live work tree
instead. Never edited by hand.
"""

COMMIT: str | None = None
DIRTY: bool = False
