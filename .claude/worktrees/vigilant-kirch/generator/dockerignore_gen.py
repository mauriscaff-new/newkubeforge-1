"""Gerador de `.dockerignore` por linguagem."""

from __future__ import annotations


COMMON_PATTERNS: list[str] = [
    ".git",
    ".gitignore",
    ".env",
    ".env.*",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "*.log",
    "*.pem",
    "*.key",
    "*.p12",
]

LANGUAGE_PATTERNS: dict[str, list[str]] = {
    "python": ["*.pyc", "*.pyo", ".mypy_cache", "htmlcov", ".coverage"],
    "node": ["node_modules", "npm-debug.log*", "yarn-error.log*", ".next", "dist", "coverage"],
    "java": [".gradle", "target", "*.class", "*.jar", "*.war"],
    "go": ["bin", "pkg", "coverage.out"],
    "dotnet": ["bin", "obj", "*.user", "*.suo"],
}


def generate(language: str) -> str:
    """Retorna conteúdo de `.dockerignore` conforme a linguagem principal."""

    normalized = language.strip().lower()
    language_patterns = LANGUAGE_PATTERNS.get(normalized, [])
    lines = COMMON_PATTERNS + language_patterns
    # Preserva ordem e remove duplicados.
    unique_lines = list(dict.fromkeys(lines))
    return "\n".join(unique_lines) + "\n"
