"""Tests for log configuration."""

from __future__ import annotations

import logging

import pytest

from gpt_oss_shim.shim import configure_logging


@pytest.fixture(autouse=True)
def _restore_levels():
    names = ("gpt_oss_shim", "httpx", "httpcore")
    saved = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_shim_logger_follows_the_configured_level() -> None:
    configure_logging("warning")

    assert logging.getLogger("gpt_oss_shim").level == logging.WARNING


def test_http_client_request_lines_are_not_logged_at_info() -> None:
    # httpx logs every request URL at INFO, which includes the Azure resource name.
    configure_logging("info")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_debug_level_keeps_http_client_logs() -> None:
    configure_logging("debug")

    assert logging.getLogger("httpx").level == logging.DEBUG
