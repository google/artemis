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

from datetime import UTC, datetime, timedelta
import logging
import math
import secrets
from app.config import settings

logger = logging.getLogger("artemis.otp")


class OTPResendCooldownError(Exception):
    """Raised when a new OTP is requested before the resend cooldown has elapsed."""

    def __init__(self, retry_after_seconds: int):
        super().__init__(f"OTP resend cooldown active; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class OTPService:
    """Manages generation, storage, dispatch, and verification of One-Time Passwords."""

    def __init__(self):
        # In-memory storage: identifier ->
        #   {"code": str, "issued_at": datetime, "expires_at": datetime, "attempts": int}
        self._store: dict[str, dict] = {}

    def generate_otp(self, identifier: str) -> str:
        """Generate a secure 6-digit OTP code and record its expiration.

        Raises OTPResendCooldownError if the current code was issued less than
        OTP_RESEND_COOLDOWN_SECONDS ago, so the per-code attempt limit cannot be
        reset by requesting new codes in a loop.
        """
        # Clean identifier
        ident = identifier.strip().lower()
        now = datetime.now(UTC)

        current = self._store.get(ident)
        if current is not None:
            cooldown = timedelta(seconds=settings.OTP_RESEND_COOLDOWN_SECONDS)
            resend_at = current["issued_at"] + cooldown
            if now < resend_at:
                raise OTPResendCooldownError(math.ceil((resend_at - now).total_seconds()))

        # DEV_MOCK_OTP issues a fixed code for local development (rejected in production)
        if settings.DEV_MOCK_OTP:
            code = settings.DEV_MOCK_OTP
        else:
            code = f"{secrets.randbelow(900000) + 100000}"

        expires_at = now + timedelta(seconds=settings.OTP_EXPIRE_SECONDS)
        self._store[ident] = {
            "code": code,
            "issued_at": now,
            "expires_at": expires_at,
            "attempts": 0,
        }

        # No SMS / Email provider is wired into this template yet. Never log the code
        # itself: anyone with log access could use it to sign in as this user.
        logger.warning(
            f"[OTP Service] Issued OTP for {ident} (Expires at: {expires_at.isoformat()}), "
            "but no delivery provider is configured"
        )
        return code

    def verify_otp(self, identifier: str, code: str) -> bool:
        """Verify an OTP code against the code issued to this identifier and consume it.

        There is deliberately no bypass path: DEV_MOCK_OTP only changes which code
        `generate_otp` issues, so a code must always have been requested first.
        """
        ident = identifier.strip().lower()
        record = self._store.get(ident)

        if not record:
            logger.warning(f"[OTP Service] No active OTP found for {ident}")
            return False

        if datetime.now(UTC) > record["expires_at"]:
            logger.warning(f"[OTP Service] OTP for {ident} has expired")
            self._store.pop(ident, None)
            return False

        # A locked record stays stored until it expires or is replaced after the resend
        # cooldown; deleting it would let a new code (and new attempts) be issued at once.
        if record["attempts"] >= settings.OTP_MAX_ATTEMPTS:
            logger.warning(f"[OTP Service] OTP for {ident} is locked after too many attempts")
            return False

        # Compare bytes: compare_digest raises TypeError for non-ASCII str input.
        if secrets.compare_digest(record["code"].encode(), code.strip().encode()):
            logger.info(f"[OTP Service] OTP successfully verified for {ident}")
            self._store.pop(ident, None)  # Consume code
            return True

        record["attempts"] += 1
        if record["attempts"] >= settings.OTP_MAX_ATTEMPTS:
            logger.warning(f"[OTP Service] Too many invalid OTP attempts for {ident}; code locked")
        else:
            logger.warning(f"[OTP Service] Invalid OTP attempt for {ident}")
        return False


otp_service = OTPService()
