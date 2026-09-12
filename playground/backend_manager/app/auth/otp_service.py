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

from datetime import datetime, timedelta, UTC
import logging
import secrets
from app.config import settings

logger = logging.getLogger("artemis.otp")


class OTPService:
    """Manages generation, storage, dispatch, and verification of One-Time Passwords."""

    def __init__(self):
        # In-memory storage: identifier -> {"code": str, "expires_at": datetime}
        self._store: dict[str, dict] = {}

    def generate_otp(self, identifier: str) -> str:
        """Generate a secure 6-digit OTP code and record its expiration."""
        # Clean identifier
        ident = identifier.strip().lower()

        # If DEV_MOCK_OTP is configured, use it for local/test environments
        if settings.DEV_MOCK_OTP:
            code = settings.DEV_MOCK_OTP
        else:
            code = f"{secrets.randbelow(900000) + 100000}"

        expires_at = datetime.now(UTC) + timedelta(seconds=settings.OTP_EXPIRE_SECONDS)
        self._store[ident] = {
            "code": code,
            "expires_at": expires_at,
        }

        # Dispatch via SMS / Email provider (mocked logger in template)
        logger.info(
            f"[OTP Service] Generated OTP for {ident}: [{code}] (Expires at: {expires_at.isoformat()})"
        )
        return code

    def verify_otp(self, identifier: str, code: str) -> bool:
        """Verify an OTP code against stored records and consume it."""
        ident = identifier.strip().lower()
        record = self._store.get(ident)

        # Allow fallback mock code for local testing
        if settings.DEV_MOCK_OTP and code == settings.DEV_MOCK_OTP:
            logger.info(f"[OTP Service] Development mock OTP accepted for {ident}")
            self._store.pop(ident, None)
            return True

        if not record:
            logger.warning(f"[OTP Service] No active OTP found for {ident}")
            return False

        if datetime.now(UTC) > record["expires_at"]:
            logger.warning(f"[OTP Service] OTP for {ident} has expired")
            self._store.pop(ident, None)
            return False

        if secrets.compare_digest(record["code"], code.strip()):
            logger.info(f"[OTP Service] OTP successfully verified for {ident}")
            self._store.pop(ident, None)  # Consume code
            return True

        logger.warning(f"[OTP Service] Invalid OTP attempt for {ident}")
        return False


otp_service = OTPService()
