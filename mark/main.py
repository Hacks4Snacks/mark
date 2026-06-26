"""Console entry point: launch the mark web app with uvicorn.

The FastAPI application itself lives in :mod:`mark.app`; this module only
handles process startup so ``mark.main:main`` and ``python -m mark`` stay
stable launch paths.
"""

from __future__ import annotations

from . import config
from .app import app

__all__ = ["app", "main"]


def main() -> None:
    import uvicorn

    config.ensure_dirs()
    print(f"Mark - http://{config.HOST}:{config.PORT}")
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
