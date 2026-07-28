"""Guard user-visible text against the legacy product brand."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY_BRAND = "Deep" + "Tutor"
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
STANDALONE_BRAND_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_]){LEGACY_BRAND}(?![A-Za-z0-9_])"
)


def _tracked_text_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths: list[Path] = []
    for relative_path in result.stdout.decode("utf-8").split("\0"):
        if not relative_path:
            continue
        path = ROOT / relative_path
        if not path.is_file():
            continue
        with path.open("rb") as tracked_file:
            sample = tracked_file.read(8192)
        if b"\0" not in sample:
            paths.append(path)
    return paths


def test_visible_branding_has_no_unprotected_legacy_name() -> None:
    offenders: list[str] = []

    for path in _tracked_text_files():
        content = path.read_text(encoding="utf-8", errors="ignore")
        content = URL_PATTERN.sub("", content)
        content = content.replace(f"HKUDS/{LEGACY_BRAND}", "")
        content = content.replace(f"xinlingzhifei/{LEGACY_BRAND}", "")
        content = content.replace(f"HKUDS.{LEGACY_BRAND}", "")
        if STANDALONE_BRAND_PATTERN.search(content):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == [], (
        "Replace standalone legacy brand copy with yFeiSTAI; keep only URLs, "
        f"repository identifiers, and compatibility symbols: {offenders}"
    )
