"""Public-release validation checks for Casita.

This is intentionally lightweight: it catches private operational strings and
fixture leaks without turning the interview repo into a fully tested project.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = [
    ROOT / "fixtures" / "demo.sqlite",
    ROOT / "src" / "casita" / "fixtures" / "demo.sqlite",
]

PRIVATE_PATTERNS = {
    "selected home": re.compile(r"Blithedale", re.IGNORECASE),
    "dog names": re.compile(r"Limoncello|Pancetta", re.IGNORECASE),
    "private infra": re.compile(r"casita-mb|openclaw-mb-state", re.IGNORECASE),
    "api key": re.compile(r"AIza[0-9A-Za-z_-]+"),
    "private email": re.compile(r"(matin@|mtamizi@|@imperfect\.)", re.IGNORECASE),
    "phone number": re.compile(
        r"(?<![\d.-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?![\d.-])"
    ),
    "private prompt detail": re.compile(r"Creative Director|MX plates", re.IGNORECASE),
}

PERSONAL_NAME_PATTERN = re.compile(r"\b(Matin|Bibiana|matin|bibiana)\b")

TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".example",
    ".envrc",
    ".gitignore",
    ".gitattributes",
    ".yml",
    ".yaml",
}

def _is_text_path(path: Path) -> bool:
    return path.name in {"Makefile", "LICENSE"} or path.suffix in TEXT_SUFFIXES


def _iter_project_text() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    ignored_dirs = {".git", ".venv", ".cache", "site", "tmp"}
    for path in ROOT.rglob("*"):
        if path == Path(__file__).resolve():
            continue
        if any(part in ignored_dirs for part in path.relative_to(ROOT).parts):
            continue
        if not path.is_file() or not _is_text_path(path):
            continue
        out.append((path, path.read_text(encoding="utf-8")))
    return out


def _fixture_text(fixture: Path) -> str:
    if not fixture.exists():
        raise SystemExit(f"Missing fixture: {fixture}")
    chunks: list[str] = []
    with sqlite3.connect(fixture) as conn:
        conn.row_factory = sqlite3.Row
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if not row["name"].startswith("sqlite_")
        ]
        for table in tables:
            columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})")
                if row["type"].upper() in {"TEXT", "TIMESTAMP"} or not row["type"]
            ]
            if not columns:
                continue
            quoted = ", ".join(f'"{col}"' for col in columns)
            for row in conn.execute(f'SELECT {quoted} FROM "{table}"'):
                chunks.extend(str(value) for value in row if value is not None)
    return "\n".join(chunks)


def main() -> None:
    failures: list[str] = []
    for path, text in _iter_project_text():
        patterns = PRIVATE_PATTERNS.copy()
        patterns["personal names"] = PERSONAL_NAME_PATTERN
        for label, pattern in patterns.items():
            if pattern.search(text):
                rel = path.relative_to(ROOT)
                failures.append(f"{rel}: matched {label}")

    for fixture in FIXTURES:
        fixture_text = _fixture_text(fixture)
        for label, pattern in {**PRIVATE_PATTERNS, "personal names": PERSONAL_NAME_PATTERN}.items():
            if pattern.search(fixture_text):
                failures.append(f"{fixture.relative_to(ROOT)}: matched {label}")

    if failures:
        raise SystemExit("Public validation failed:\n- " + "\n- ".join(failures))
    print("public validation passed")


if __name__ == "__main__":
    main()
