"""Regras e defaults usados na geracao de artefatos."""

from __future__ import annotations

LANGUAGE_DEFAULTS: dict[str, dict[str, object]] = {
    "node": {
        "base_image": "node:20-alpine",
        "builder_image": "node:20-alpine",
        "port": 3000,
        "start_command": "npm run start",
        "build_tool": "npm",
        "health_check_path": "/health",
        "has_build_step": False,
    },
    "python": {
        "base_image": "python:3.12-alpine",
        "port": 8000,
        "start_command": "python main.py",
        "build_tool": "pip",
        "health_check_path": "/health",
        "has_build_step": False,
    },
    "java": {
        "base_image": "eclipse-temurin:21-jre-alpine",
        "builder_image": "maven:3.9-eclipse-temurin-21-alpine",
        "port": 8080,
        "start_command": "java -jar app.jar",
        "build_tool": "maven",
        "health_check_path": "/actuator/health",
        "has_build_step": True,
    },
    "go": {
        "base_image": "golang:1.22-alpine",
        "port": 8080,
        "start_command": "./app",
        "build_tool": "go",
        "health_check_path": "/health",
        "has_build_step": True,
    },
    "dotnet": {
        "base_image": "mcr.microsoft.com/dotnet/aspnet:8.0-alpine",
        "builder_image": "mcr.microsoft.com/dotnet/sdk:8.0-alpine",
        "port": 8080,
        "start_command": "dotnet run",
        "build_tool": "dotnet",
        "health_check_path": "/health",
        "has_build_step": True,
    },
}

FRAMEWORK_OVERRIDES: dict[str, dict[str, object]] = {
    "nextjs": {
        "port": 3000,
        "start_command": "npm run start",
        "has_build_step": True,
        "health_check_path": "/api/health",
    },
    "nestjs": {
        "port": 3000,
        "start_command": "npm run start:prod",
        "has_build_step": True,
        "health_check_path": "/health",
    },
    "fastapi": {
        "port": 8000,
        "start_command": "uvicorn main:app --host 0.0.0.0 --port 8000",
        "build_tool": "uv",
        "health_check_path": "/health",
    },
    "django": {
        "port": 8000,
        "start_command": "python manage.py runserver 0.0.0.0:8000",
        "health_check_path": "/health",
    },
    "flask": {
        "port": 5000,
        "start_command": "flask run --host 0.0.0.0 --port 5000",
        "health_check_path": "/health",
    },
    "springboot": {
        "port": 8080,
        "start_command": "java -jar app.jar",
        "build_tool": "maven",
        "has_build_step": True,
        "health_check_path": "/actuator/health",
    },
    "springboot_gradle": {
        "port": 8080,
        "start_command": "./gradlew bootRun",
        "build_tool": "gradle",
        "has_build_step": True,
        "health_check_path": "/actuator/health",
    },
}

K8S_DEFAULTS: dict[str, object] = {
    "namespace": "default",
    "replicas": 2,
    "service_type": "ClusterIP",
    "container_port_name": "http",
    "hpa": {
        "enabled": True,
        "min_replicas": 2,
        "max_replicas_multiplier": 3,
        "cpu_average_utilization": 70,
    },
    "resources": {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"},
    },
    "network_policy": {
        "enabled": True,
        "allow_dns": True,
    },
}
