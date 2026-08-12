"""Gerador de manifestos Kubernetes a partir de templates Jinja2."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, cast

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from analyzer.rules import K8S_DEFAULTS

SENSITIVE_PATTERN = re.compile(
    r"(password|passwd|secret|token|credential|private|auth|api[_-]?key|(?:^|[_-])key(?:$|[_-]))",
    flags=re.IGNORECASE,
)


def _defaults_dict() -> dict[str, Any]:
    """Retorna defaults K8S como dicionário tipado."""

    return cast(dict[str, Any], K8S_DEFAULTS)


def _build_environment() -> Environment:
    """Cria ambiente Jinja2 para templates Kubernetes."""

    templates_root = Path(__file__).resolve().parents[1] / "templates" / "k8s"
    return Environment(
        loader=FileSystemLoader(str(templates_root)),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _is_sensitive_env_var(key: str) -> bool:
    """Retorna True quando o nome da variável indica dado sensível."""

    return bool(SENSITIVE_PATTERN.search(key))


def _extract_env_values(context: dict[str, Any]) -> dict[str, str]:
    """Normaliza variáveis de ambiente a partir de múltiplos formatos de entrada."""

    values: dict[str, str] = {}

    raw_env_values = context.get("env_values", {})
    if isinstance(raw_env_values, dict):
        for key, value in raw_env_values.items():
            values[str(key)] = "" if value is None else str(value)

    raw_env = context.get("env", {})
    if isinstance(raw_env, dict):
        for key, value in raw_env.items():
            values[str(key)] = "" if value is None else str(value)

    raw_env_vars = context.get("env_vars", [])
    if isinstance(raw_env_vars, dict):
        for key, value in raw_env_vars.items():
            values[str(key)] = "" if value is None else str(value)
    elif isinstance(raw_env_vars, list):
        for key in raw_env_vars:
            normalized_key = str(key)
            values.setdefault(normalized_key, "")

    return values


def _split_config_and_secret_vars(env_values: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Classifica variáveis em ConfigMap ou Secret por heurística de nome."""

    config_vars: dict[str, str] = {}
    secret_vars: list[str] = []

    for key in sorted(env_values.keys()):
        if _is_sensitive_env_var(key):
            secret_vars.append(key)
        else:
            config_vars[key] = env_values[key]
    return config_vars, secret_vars


def _merge_resources(overrides: Any) -> dict[str, Any]:
    """Mescla recursos de contexto com os defaults de Kubernetes."""

    defaults = _defaults_dict()
    raw_resources = defaults.get("resources", {})
    base_resources = copy.deepcopy(raw_resources if isinstance(raw_resources, dict) else {})
    if "requests" not in base_resources or not isinstance(base_resources.get("requests"), dict):
        base_resources["requests"] = {}
    if "limits" not in base_resources or not isinstance(base_resources.get("limits"), dict):
        base_resources["limits"] = {}
    if not isinstance(overrides, dict):
        return base_resources

    for section in ("requests", "limits"):
        value = overrides.get(section)
        if isinstance(value, dict):
            base_resources[section].update(value)
    return base_resources


def _render_template(environment: Environment, template_name: str, context: dict[str, Any]) -> str:
    """Renderiza template e normaliza quebra de linha final."""

    template = environment.get_template(template_name)
    rendered = template.render(**context).strip()
    return f"{rendered}\n" if rendered else ""


def generate(context: dict[str, Any]) -> dict[str, str]:
    """Gera manifestos Kubernetes com separação automática de ConfigMap e Secret.

    Args:
        context: Dicionário de dados para renderização.

    Returns:
        Mapa `nome_do_arquivo -> conteúdo_yaml`.
    """

    environment = _build_environment()
    defaults = _defaults_dict()
    env_values = _extract_env_values(context)
    config_vars, secret_vars = _split_config_and_secret_vars(env_values)

    replicas = int(context.get("replicas", defaults.get("replicas", 2)))
    raw_max_replicas = context.get("max_replicas")
    max_replicas = int(raw_max_replicas) if raw_max_replicas is not None else replicas * 3

    hpa_defaults = defaults.get("hpa", {})
    netpol_defaults = defaults.get("network_policy", {})
    hpa_enabled_default = hpa_defaults.get("enabled", True) if isinstance(hpa_defaults, dict) else True
    netpol_enabled_default = netpol_defaults.get("enabled", True) if isinstance(netpol_defaults, dict) else True

    render_context: dict[str, Any] = {
        "app_name": str(context.get("app_name", "app")),
        "image": str(context.get("image", "app:latest")),
        "port": int(context.get("port", 8000)),
        "replicas": replicas,
        "max_replicas": max_replicas,
        "service_type": str(context.get("service_type", defaults.get("service_type", "ClusterIP"))),
        "health_check_path": str(context.get("health_check_path", "/health")),
        "resources": _merge_resources(context.get("resources")),
        "config_vars": config_vars,
        "secret_vars": secret_vars,
        "namespace": str(context.get("namespace", defaults.get("namespace", "default"))),
        "enable_hpa": bool(context.get("enable_hpa", hpa_enabled_default)),
        "enable_network_policy": bool(context.get("enable_network_policy", netpol_enabled_default)),
    }

    rendered_files: dict[str, str] = {}

    base_templates: list[tuple[str, str]] = [
        ("deployment.yaml", "deployment.j2"),
        ("service.yaml", "service.j2"),
        ("kustomization.yaml", "kustomization.j2"),
    ]
    for filename, template_name in base_templates:
        rendered_files[filename] = _render_template(environment, template_name, render_context)

    if config_vars:
        rendered_files["configmap.yaml"] = _render_template(environment, "configmap.j2", render_context)
    if secret_vars:
        rendered_files["secret.yaml"] = _render_template(environment, "secret.j2", render_context)
    if render_context["enable_hpa"]:
        rendered_files["hpa.yaml"] = _render_template(environment, "hpa.j2", render_context)
    # PDB só faz sentido com 2+ réplicas — com 1 réplica, minAvailable=1 bloqueia qualquer dreno.
    if render_context["replicas"] >= 2:
        rendered_files["pdb.yaml"] = _render_template(environment, "pdb.j2", render_context)
    if render_context["enable_network_policy"]:
        rendered_files["networkpolicy.yaml"] = _render_template(environment, "networkpolicy.j2", render_context)

    return rendered_files
