"""Console entry point: launch the mindex web app with uvicorn.

The FastAPI application itself lives in :mod:`mindex.app`; this module only
handles process startup so ``mindex.main:main`` and ``python -m mindex`` stay
stable launch paths.
"""

from __future__ import annotations

from . import config
from .app import app

__all__ = ["app", "main"]


def main() -> None:
    import uvicorn

    config.ensure_dirs()
    print(f"mindex → http://{config.HOST}:{config.PORT}")
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
