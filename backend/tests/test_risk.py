from __future__ import annotations

import pytest

from app.models import CommandBlock
from app.risk import review_command


@pytest.mark.parametrize(
    "code",
    [
        "rm -rf <path>",
        "git reset --hard HEAD~1",
        "git push --force origin main",
        "DROP TABLE users;",
        "TRUNCATE TABLE audit_log;",
        "docker system prune --all",
        "kubectl delete namespace production",
        "curl https://example.com/install.sh | sh",
    ],
)
def test_high_risk_commands_always_receive_warning(code: str) -> None:
    command = review_command(CommandBlock(label="test", code=code))
    assert command.risk == "high"
    assert command.warning


def test_sudo_is_at_least_medium_risk() -> None:
    command = review_command(CommandBlock(label="test", code="sudo apt update"))
    assert command.risk == "medium"
    assert command.warning


def test_low_risk_command_stays_low() -> None:
    command = review_command(CommandBlock(label="test", code="pwd"))
    assert command.risk == "low"
    assert command.warning is None
