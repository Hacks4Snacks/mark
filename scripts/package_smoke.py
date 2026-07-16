from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def verify_web_assets(client, source_web: Path) -> dict[str, int]:
    """Verify the installed app serves every source web file byte-for-byte."""
    checked: dict[str, int] = {}
    for source in sorted(path for path in source_web.rglob("*") if path.is_file()):
        relative = source.relative_to(source_web).as_posix()
        url = "/" if relative == "index.html" else f"/{relative}"
        response = client.get(url)
        if response.status_code != 200:
            raise AssertionError(
                f"packaged web asset {url!r} returned {response.status_code}"
            )
        expected = source.read_bytes()
        if response.content != expected:
            raise AssertionError(f"packaged web asset {url!r} differs from source")
        checked[url] = len(response.content)
    return checked


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--require-installed",
        action="store_true",
        help="fail if Mark resolves from the source checkout instead of site-packages",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as data_dir:
        os.environ["MARK_AUTO_SYNC"] = "0"
        os.environ["MARK_DATA_DIR"] = data_dir

        from fastapi.testclient import TestClient

        import mark
        from mark.app import create_app
        from mark.model_pricing import load_registry

        repo_root = Path(__file__).resolve().parents[1]
        source_web = repo_root / "mark" / "web"
        package_path = Path(mark.__file__).resolve()
        if args.require_installed and package_path.is_relative_to(repo_root):
            raise SystemExit(
                f"source checkout imported instead of wheel: {package_path}"
            )
        registry = load_registry()
        if not registry.get("models"):
            raise AssertionError("packaged model pricing registry is empty")

        with (
            patch("mark.background.start"),
            patch("mark.background.stop"),
            patch("mark.background.mark_http_ready"),
            TestClient(create_app()) as client,
        ):
            checked = verify_web_assets(client, source_web)

    print(f"Verified {len(checked)} packaged web routes from {package_path}")
    for url in sorted(checked):
        print(f"  {url} ({checked[url]} bytes)")


if __name__ == "__main__":
    main()
