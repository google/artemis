# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Playground Backend Manager sign-in must not be bypassable or brute-forceable.

Covers the settings guards (required JWT secret, no fixed OTP in production) and the
OTP service (no mock-code bypass, single use, attempt lock, resend cooldown, no code
in logs). The backend is a standalone app whose package is named ``app``; it is
imported from its own directory for each test and removed from ``sys.modules`` again.
"""

from datetime import timedelta
import importlib
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

from pydantic import ValidationError
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3] / "playground" / "backend_manager"
VALID_SECRET = "s" * 64
IDENT = "user@example.com"


def _drop_backend_modules() -> None:
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]


@pytest.fixture
def backend(monkeypatch, tmp_path):
    # Hermetic settings: no inherited env vars and no developer .env in the CWD.
    for var in ("JWT_SECRET_KEY", "APP_ENV", "DEV_MOCK_OTP"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JWT_SECRET_KEY", VALID_SECRET)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(BACKEND_ROOT))
    _drop_backend_modules()

    config = importlib.import_module("app.config")
    otp = importlib.import_module("app.auth.otp_service")
    try:
        yield SimpleNamespace(config=config, otp=otp, service=otp.OTPService())
    finally:
        _drop_backend_modules()


def _settings(backend, **overrides):
    return backend.config.Settings(_env_file=None, **overrides)


# --- Settings guards --------------------------------------------------------------------


def test_jwt_secret_is_required(backend, monkeypatch):
    monkeypatch.delenv("JWT_SECRET_KEY")
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        _settings(backend)


def test_short_jwt_secret_is_rejected(backend):
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY"):
        _settings(backend, JWT_SECRET_KEY="change-me")


@pytest.mark.parametrize("app_env", ["production", "Production", " PRODUCTION "])
def test_mock_otp_is_rejected_in_production(backend, app_env):
    with pytest.raises(ValidationError, match="DEV_MOCK_OTP must be empty"):
        _settings(backend, APP_ENV=app_env, DEV_MOCK_OTP="123456")


def test_production_without_mock_otp_starts(backend):
    settings = _settings(backend, APP_ENV="production")
    assert settings.DEV_MOCK_OTP == ""


def test_mock_otp_is_allowed_outside_production(backend):
    settings = _settings(backend, APP_ENV="development", DEV_MOCK_OTP="123456")
    assert settings.DEV_MOCK_OTP == "123456"


# --- OTP service ------------------------------------------------------------------------


def test_mock_code_without_issued_otp_is_rejected(backend, monkeypatch):
    """Regression: the mock code used to be accepted for any identifier, unrequested."""
    monkeypatch.setattr(backend.config.settings, "DEV_MOCK_OTP", "123456")
    assert backend.service.verify_otp("victim@example.com", "123456") is False


def test_issued_code_verifies_once(backend):
    code = backend.service.generate_otp(IDENT)
    assert backend.service.verify_otp(IDENT, code) is True
    assert backend.service.verify_otp(IDENT, code) is False


def test_mock_code_verifies_after_it_is_issued(backend, monkeypatch):
    monkeypatch.setattr(backend.config.settings, "DEV_MOCK_OTP", "123456")
    backend.service.generate_otp(IDENT)
    assert backend.service.verify_otp(IDENT, "123456") is True


def test_issued_code_is_not_logged(backend, caplog):
    with caplog.at_level(logging.DEBUG, logger="artemis.otp"):
        code = backend.service.generate_otp(IDENT)
    assert caplog.records
    assert code not in caplog.text


def test_code_is_locked_after_max_attempts(backend):
    code = backend.service.generate_otp(IDENT)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(backend.config.settings.OTP_MAX_ATTEMPTS):
        assert backend.service.verify_otp(IDENT, wrong) is False
    assert backend.service.verify_otp(IDENT, code) is False


def test_resend_within_cooldown_is_refused(backend):
    backend.service.generate_otp(IDENT)
    with pytest.raises(backend.otp.OTPResendCooldownError) as exc_info:
        backend.service.generate_otp(IDENT)
    cooldown = backend.config.settings.OTP_RESEND_COOLDOWN_SECONDS
    assert 0 < exc_info.value.retry_after_seconds <= cooldown


def test_lockout_cannot_be_reset_by_immediate_resend(backend):
    """Regression: resending right after a lockout used to grant fresh attempts at once."""
    backend.service.generate_otp(IDENT)
    for _ in range(backend.config.settings.OTP_MAX_ATTEMPTS):
        backend.service.verify_otp(IDENT, "not-the-code")
    with pytest.raises(backend.otp.OTPResendCooldownError):
        backend.service.generate_otp(IDENT)


def test_resend_after_cooldown_issues_a_new_code(backend):
    backend.service.generate_otp(IDENT)
    for _ in range(backend.config.settings.OTP_MAX_ATTEMPTS):
        backend.service.verify_otp(IDENT, "not-the-code")
    cooldown = timedelta(seconds=backend.config.settings.OTP_RESEND_COOLDOWN_SECONDS)
    backend.service._store[IDENT]["issued_at"] -= cooldown

    code = backend.service.generate_otp(IDENT)
    assert backend.service.verify_otp(IDENT, code) is True


def test_expired_code_is_rejected(backend):
    code = backend.service.generate_otp(IDENT)
    lifetime = timedelta(seconds=backend.config.settings.OTP_EXPIRE_SECONDS + 1)
    backend.service._store[IDENT]["expires_at"] -= lifetime
    assert backend.service.verify_otp(IDENT, code) is False


def test_non_ascii_code_is_rejected_without_error(backend):
    backend.service.generate_otp(IDENT)
    assert backend.service.verify_otp(IDENT, "１２３４５６") is False
