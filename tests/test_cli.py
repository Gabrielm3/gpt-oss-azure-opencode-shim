"""Tests for the installed command-line entry points."""

from __future__ import annotations

from importlib.metadata import version

import pytest

import gpt_oss_shim
from gpt_oss_shim import report, shim


def test_version_comes_from_the_installed_distribution() -> None:
    assert gpt_oss_shim.__version__ == version("gpt-oss-azure-opencode-shim")


def test_version_flag_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        shim.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == (
        f"gpt-oss-azure-opencode-shim {gpt_oss_shim.__version__}"
    )


def test_help_points_to_environment_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        shim.main(["--help"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "UPSTREAM_URL" in out
    assert "AZURE_FOUNDRY_API_KEY" in out


def test_missing_configuration_exits_with_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UPSTREAM_URL", raising=False)
    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)

    assert shim.main([]) == 1


@pytest.mark.parametrize("days", ["0", "-3", "x"])
def test_report_rejects_a_window_that_is_not_a_positive_number(tmp_path, days: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        report.main([str(tmp_path), "--days", days])

    assert exit_info.value.code == 2
