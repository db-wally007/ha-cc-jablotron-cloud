"""Client for Jablotron Cloud API."""

from collections.abc import Callable
import logging
import threading
import time
from typing import Literal

from jablotronpy import (
    Jablotron,
    JablotronApiException,
    JablotronProgrammableGates,
    JablotronSections,
    JablotronService,
    JablotronServiceInformation,
    JablotronThermoDevice,
    SessionExpiredException,
    UnauthorizedException,
)

from .const import SESSION_MAX_AGE
from .types import JablotronServiceData

_LOGGER = logging.getLogger(__name__)

try:
    from jablotronpy import TooManyRequestsException  # type: ignore[attr-defined]
except ImportError:

    class TooManyRequestsException(JablotronApiException):  # type: ignore[no-redef]
        """Exception raised when request fails with 429 status code.

        Fallback definition until jablotronpy ships 429 support.
        Remove once the upstream release is pinned in manifest.json.
        """

        retry_after: int | None = None


class JablotronClient:
    """Client for Jablotron Cloud API with a persistent, self-healing session."""

    def __init__(
        self,
        username: str,
        password: str,
        default_pin: str | None = None,
        force_arm: bool = True,
    ) -> None:
        """Initialize Jablotron client."""

        self.services: dict[int, JablotronServiceData] = {}
        self.force_arm = force_arm
        self._default_pin = default_pin
        # Single bridge instance so the session cookie set by perform_login() is reused across calls
        self._bridge = Jablotron(username, password, default_pin)
        self._login_lock = threading.Lock()
        self._session_generation = 0
        self._session_created_at = 0.0

    def get_default_pin(self) -> str | None:
        """Return the default PIN code."""

        return self._default_pin

    def validate_login(self) -> None:
        """Validate credentials by performing a login."""

        self._ensure_logged_in()

    def _ensure_logged_in(self) -> int:
        """Log in when there is no valid session and return the current session generation.

        The session is refreshed proactively once it approaches the server-side
        expiry (~30 minutes) so that regular polling never hits an expired session.
        Must only be called from executor threads.
        """

        with self._login_lock:
            session_age = time.monotonic() - self._session_created_at
            if self._session_generation == 0 or session_age > SESSION_MAX_AGE:
                _LOGGER.debug("Logging in to Jablotron Cloud")
                self._bridge.perform_login()
                self._session_generation += 1
                self._session_created_at = time.monotonic()
            return self._session_generation

    def _invalidate_session(self, generation: int) -> None:
        """Invalidate the session unless another thread re-logged in in the meantime."""

        with self._login_lock:
            if self._session_generation == generation:
                self._session_generation = 0

    def _api_call[T](self, func: Callable[[Jablotron], T]) -> T:
        """Run an API call, transparently re-logging in once when the session is no longer valid.

        An expired session and bad credentials both surface as UnauthorizedException, so the
        re-login disambiguates them: with bad credentials the re-login itself raises
        UnauthorizedException, which is the only way an auth error propagates to callers.
        """

        generation = self._ensure_logged_in()
        try:
            return func(self._bridge)
        except (SessionExpiredException, UnauthorizedException):
            _LOGGER.debug("Jablotron Cloud session is no longer valid, re-logging in and retrying")
            self._invalidate_session(generation)
            self._ensure_logged_in()
            return func(self._bridge)

    def get_services(self) -> list[JablotronService]:
        """Return list of services associated with the Jablotron Cloud account."""

        return self._api_call(lambda bridge: bridge.get_services())

    def get_service_information(self, service_id: int) -> JablotronServiceInformation:
        """Return information about the specified service."""

        return self._api_call(lambda bridge: bridge.get_service_information(service_id))

    def get_sections(self, service_id: int, service_type: str = "JA100") -> JablotronSections:
        """Return sections and their states for the specified service."""

        return self._api_call(lambda bridge: bridge.get_sections(service_id, service_type))

    def get_programmable_gates(self, service_id: int, service_type: str = "JA100") -> JablotronProgrammableGates:
        """Return programmable gates and their states for the specified service."""

        return self._api_call(lambda bridge: bridge.get_programmable_gates(service_id, service_type))

    def get_thermo_devices(self, service_id: int, service_type: str = "JA100") -> list[JablotronThermoDevice]:
        """Return list of thermo devices for the specified service."""

        return self._api_call(lambda bridge: bridge.get_thermo_devices(service_id, service_type))

    def control_section(
        self,
        service_id: int,
        component_id: str,
        state: Literal["ARM", "PARTIAL_ARM", "DISARM"],
        pin_code: str | None = None,
        service_type: str = "JA100",
        force: bool = False,
    ) -> bool:
        """Set section of the specified service to the desired state."""

        return self._api_call(
            lambda bridge: bridge.control_section(service_id, component_id, state, pin_code, service_type, force)
        )

    def control_programmable_gate(
        self,
        service_id: int,
        component_id: str,
        state: Literal["ON", "OFF"],
        pin_code: str | None = None,
        service_type: str = "JA100",
        force: bool = False,
    ) -> bool:
        """Set programmable gate of the specified service to the desired state."""

        return self._api_call(
            lambda bridge: bridge.control_programmable_gate(
                service_id, component_id, state, pin_code, service_type, force
            )
        )

    def control_thermo_device(
        self,
        service_id: int,
        object_device_id: str,
        heating_mode: Literal["MANUAL", "SCHEDULED", "OFF", "ON"] | None = None,
        temperature: float | None = None,
        service_type: str = "JA100",
    ) -> bool:
        """Set thermo device of the specified service to the desired heating mode or temperature."""

        return self._api_call(
            lambda bridge: bridge.control_thermo_device(
                service_id, object_device_id, heating_mode, temperature, service_type
            )
        )
