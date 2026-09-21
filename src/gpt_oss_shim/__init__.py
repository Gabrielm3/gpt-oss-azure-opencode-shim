"""Compatibility shim between OpenCode and Azure-hosted OSS models."""

from .shim import __version__, create_app, main

__all__ = ["__version__", "create_app", "main"]
