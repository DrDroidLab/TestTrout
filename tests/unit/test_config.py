import pytest

from testtrout.domain.config import (
    Entrypoint,
    Permission,
    SecretResolutionError,
    resolve_secret,
)


def test_env_reference_is_resolved(monkeypatch):
    monkeypatch.setenv("QA_TEST_SECRET", "s3cret")
    assert resolve_secret("env:QA_TEST_SECRET") == "s3cret"


def test_plain_value_passes_through():
    assert resolve_secret("literal") == "literal"


def test_missing_env_reference_fails_loudly(monkeypatch):
    """Better a clear error here than an opaque auth failure three layers down."""
    monkeypatch.delenv("QA_ABSENT", raising=False)
    with pytest.raises(SecretResolutionError):
        resolve_secret("env:QA_ABSENT")


def test_entrypoint_is_read_only_by_default():
    """The default must never allow writes. This is the guard on production."""
    assert Entrypoint(name="prod", url="https://app.example.com").writable is False


def test_write_requires_both_disposable_and_permission():
    """Two independent switches, so one careless edit cannot expose production."""
    assert not Entrypoint(
        name="a", url="https://x.dev", disposable=True, allow=[Permission.READ]
    ).writable
    assert not Entrypoint(
        name="b", url="https://x.dev", disposable=False, allow=[Permission.WRITE]
    ).writable
    assert Entrypoint(
        name="c", url="https://x.dev", disposable=True, allow=[Permission.WRITE]
    ).writable


def test_relative_url_is_rejected():
    with pytest.raises(ValueError, match="absolute"):
        Entrypoint(name="bad", url="localhost:3000")
