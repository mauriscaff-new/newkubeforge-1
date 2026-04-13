"""Gerador de Dockerfile a partir de templates Jinja2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def _split_filter(value: str, separator: str = ",") -> list[str]:
    """Quebra string pelo separador e remove espaços vazios."""

    return [item.strip() for item in value.split(separator) if item.strip()]


def _build_environment() -> Environment:
    """Cria ambiente Jinja2 com validação estrita de variáveis."""

    templates_root = Path(__file__).resolve().parents[1] / "templates"
    environment = Environment(
        loader=FileSystemLoader(str(templates_root)),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["split"] = _split_filter
    return environment


def _resolve_template_name(language: str, has_build_step: bool) -> str:
    """Resolve template de Dockerfile com base na linguagem e build step."""

    normalized_language = language.strip().lower()
    if normalized_language == "node":
        return "node/dockerfile_build.j2" if has_build_step else "node/dockerfile_simple.j2"
    if normalized_language == "python":
        return "python/dockerfile.j2"
    if normalized_language == "java":
        return "java/dockerfile.j2"
    if normalized_language == "go":
        return "go/dockerfile.j2"
    if normalized_language == "dotnet":
        return "dotnet/dockerfile.j2"
    raise ValueError(f"Linguagem sem template de Dockerfile: {language}")


def generate(context: dict[str, Any]) -> str:
    """Renderiza Dockerfile de acordo com linguagem e estratégia de build.

    Args:
        context: Dicionário com dados de renderização.

    Returns:
        Conteúdo do Dockerfile renderizado.
    """

    language = str(context.get("language", "")).strip().lower()
    has_build_step = bool(context.get("has_build_step", False))
    template_name = _resolve_template_name(language, has_build_step)
    environment = _build_environment()
    template = environment.get_template(template_name)
    return template.render(**context).strip() + "\n"
