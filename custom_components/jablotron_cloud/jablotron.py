"""Client for Jablotron Cloud API."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
import logging
import sys
import threading
import time
from typing import Literal

from jablotronpy import (
    IncorrectPinCodeException,
    Jablotron,
    JablotronProgrammableGates,
    JablotronSectionControlResponse,
    JablotronSections,
    JablotronService,
    JablotronServiceInformation,
    JablotronThermoDevice,
    SessionExpiredException,
    TooManyRequestsException,
    UnauthorizedException,
)

from .const import BYPASS_CONTROL_ERRORS, REQUEST_TIMEOUT_SECONDS
from .types import JablotronSectionControlResult, JablotronServiceData

_LOGGER = logging.getLogger(__name__)


def _install_request_timeout() -> None:
    """Give every jablotronpy HTTP request a default timeout.

    jablotronpy issues all of its calls through a module-level ``post`` imported from
    ``requests``, with no timeout argument, so a connection that accepts but never answers
    hangs the calling worker thread for good. Wrapping that one name covers polling and
    control alike, which is safer than reproducing the library's status-code handling here
    just to pass a timeout through.
    """

    module = sys.modules[Jablotron.__module__]
    if getattr(module.post, "_jablotron_timeout_installed", False):
        return

    original_post = module.post

    @wraps(original_post)
    def post_with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
        return original_post(*args, **kwargs)

    post_with_timeout._jablotron_timeout_installed = True  # noqa: SLF001
    module.post = post_with_timeout


_install_request_timeout()

# How many times a section control request is attempted before giving up, and how long to
# wait between attempts. control_section() runs in an executor thread, so the sleep blocks
# only that worker. Three attempts spaced 2s apart keep the worst case (~4s of waiting plus
# the requests themselves) inside the alarm command's tolerable latency.
CONTROL_ATTEMPTS = 3
CONTROL_RETRY_DELAY_SECONDS = 2


def _find_bypass_error(response_data: JablotronSectionControlResponse, component_id: str) -> str | None:
    """Return the bypass control error reported for a component, when the cloud reported one."""

    for error in response_data.get("control-errors") or []:
        if error["component-id"] == component_id and error["control-error"] in BYPASS_CONTROL_ERRORS:
            return error["control-error"]

    return None


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
        self._username = username
        self._password = password
        self._default_pin = default_pin
        # The bridge doubles as the session marker: None means not logged in, and a
        # bridge instance carries the session cookie set by perform_login().
        self._bridge: Jablotron | None = None
        self._login_lock = threading.Lock()
        # Serialises every cloud call so a scheduled poll can never interleave with a
        # command. Without it the poll that runs while an arm request is still being
        # negotiated reports the section as it was BEFORE the command — correctly, since
        # the panel has not accepted anything yet — and that accurate-but-premature reading
        # lands on top of the assumed transition. Commands wait for an in-flight poll and
        # polls wait for an in-flight command; REQUEST_TIMEOUT_SECONDS bounds either wait.
        #
        # Reentrant because the operations that must be atomic span several API calls, each
        # of which locks again on its own: see exclusive().
        self._api_lock = threading.RLock()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold the cloud connection for an operation that spans several requests.

        Locking each request individually is not enough whenever a sequence of them has to
        be treated as one, because the gaps between them are exactly where a poll and a
        command interleave. Two such sequences exist: a control request and its retries
        (the backoff between attempts is a gap), and a full poll sweep (a command slipping
        between the section and gate requests would afterwards be undone by section data
        that was read before it ran). Must only be used from executor threads.
        """

        with self._api_lock:
            yield

    def get_default_pin(self) -> str | None:
        """Return the default PIN code."""

        return self._default_pin

    def validate_login(self) -> None:
        """Validate credentials by performing a login."""

        self._ensure_logged_in()

    def _ensure_logged_in(self) -> Jablotron:
        """Return a logged-in bridge, performing a login when there is none.

        The session is reused until an API call fails with an expired session;
        there is no time-based refresh, so no login request is spent while the
        session stays valid. Must only be called from executor threads.
        """

        with self._login_lock:
            if self._bridge is None:
                _LOGGER.debug("Logging in to Jablotron Cloud")
                # A fresh bridge is created for every login so the login request is not sent
                # with a stale session cookie, and it is assigned only after the login
                # succeeds, so a failed login never leaves a broken session behind.
                bridge = Jablotron(self._username, self._password, self._default_pin)
                bridge.perform_login()
                self._bridge = bridge
            return self._bridge

    def _invalidate_session(self, bridge: Jablotron) -> None:
        """Drop the bridge unless another thread already re-logged in with a new one."""

        with self._login_lock:
            if self._bridge is bridge:
                self._bridge = None

    def _api_call[T](self, func: Callable[[Jablotron], T]) -> T:
        """Run an API call, transparently re-logging in once when the session is no longer valid.

        An expired session and bad credentials both surface as UnauthorizedException, so the
        re-login disambiguates them: with bad credentials the re-login itself raises
        UnauthorizedException, which is the only way an auth error propagates to callers.
        """

        with self._api_lock:
            bridge = self._ensure_logged_in()
            try:
                return func(bridge)
            except (SessionExpiredException, UnauthorizedException):
                _LOGGER.debug("Jablotron Cloud session is no longer valid, re-logging in and retrying")
                self._invalidate_session(bridge)
                return func(self._ensure_logged_in())

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
    ) -> JablotronSectionControlResult:
        """Set section of the specified service to the desired state.

        Follows the same two steps as the official MyJablotron app: the state is requested
        normally first, and only when the cloud answers that active devices block the section
        is the request repeated with the bypass confirmed. Sections whose devices are all
        closed therefore still cost a single request, and a bypass becomes an observable event
        instead of something that silently happens on every arm.

        Transient cloud failures are retried, because Jablotron Cloud intermittently answers
        an otherwise valid control request with an error. Arming and disarming are idempotent
        — the panel ends in the requested state whether it receives the command once or three
        times — so replaying a request that may already have landed is safe.

        Three classes of failure are deliberately NOT retried:
          * a rejected PIN, because repeating it cannot help and the panel locks out after
            enough consecutive bad codes;
          * an expired session, which _api_call already resolves by re-logging in once;
          * a rate limit, where retrying is precisely what the cloud is asking us to stop.
        """

        # The retries are inside the exclusive block, not around it: releasing between
        # attempts would let a poll run during the backoff and report the section as it was
        # before the command, which is the very interleaving this serialisation exists to
        # prevent. A command therefore holds the connection until it has finished retrying.
        with self.exclusive():
            attempt = 0
            while True:
                attempt += 1
                try:
                    return self._api_call(
                        lambda bridge: self._control_section(
                            bridge,
                            service_id=service_id,
                            component_id=component_id,
                            state=state,
                            pin_code=pin_code,
                            service_type=service_type,
                            force=force,
                        )
                    )
                except (IncorrectPinCodeException, UnauthorizedException, TooManyRequestsException):
                    raise
                except Exception as ex:  # noqa: BLE001
                    if attempt >= CONTROL_ATTEMPTS:
                        _LOGGER.error(
                            "Control request '%s' for section '%s' failed on all %d attempts: %s",
                            state,
                            component_id,
                            CONTROL_ATTEMPTS,
                            ex,
                        )
                        raise
                    _LOGGER.warning(
                        "Control request '%s' for section '%s' failed on attempt %d/%d (%s), retrying in %ss",
                        state,
                        component_id,
                        attempt,
                        CONTROL_ATTEMPTS,
                        ex,
                        CONTROL_RETRY_DELAY_SECONDS,
                    )
                    time.sleep(CONTROL_RETRY_DELAY_SECONDS)

    @staticmethod
    def _control_section(
        bridge: Jablotron,
        *,
        service_id: int,
        component_id: str,
        state: Literal["ARM", "PARTIAL_ARM", "DISARM"],
        pin_code: str | None,
        service_type: str,
        force: bool,
    ) -> JablotronSectionControlResult:
        """Request a section state, confirming a bypass only when the cloud asks for one."""

        # The payload is built here instead of calling ``bridge.control_section()`` because
        # jablotronpy 0.7.4 places the ``force`` flag next to ``component-id``, where the cloud
        # silently ignores it: arming a section that has an open window then always answers
        # ``BYPASS-TIMED`` and nothing is armed, however the integration is configured. The flag
        # is only honoured inside the ``actions`` object. Verified against a JA-106K panel on
        # 2026-08-08; see https://github.com/fdegier/JablotronPy for the upstream bug.
        pin_code = bridge._get_provided_pin_or_default_pin(pin_code)  # noqa: SLF001

        def request(with_bypass: bool) -> JablotronSectionControlResponse:
            """Send one control request, bypassing active devices when asked to."""

            response = bridge._send_request(  # noqa: SLF001
                endpoint=f"{service_type}/controlComponent.json",
                payload={
                    "service-id": service_id,
                    "authorization": {"authorization-code": pin_code},
                    "control-components": [
                        {
                            "actions": {
                                "action": "CONTROL-SECTION",
                                "value": state.upper(),
                                "force": with_bypass,
                            },
                            "component-id": component_id,
                        }
                    ],
                },
            )

            return response.json().get("data", {})

        # jablotronpy's helper is reused for its EXCEPTIONS only — WRONG-CODE and every other
        # control error raise exactly as upstream. Its return value is discarded on purpose:
        # it reports success only when the response already lists the section at the requested
        # state, which is impossible while the panel counts down its exit delay, so a perfectly
        # good ARM frequently comes back False. An error-free response means the cloud accepted
        # the command, and that is what this returns.
        def accepted(response_data: JablotronSectionControlResponse) -> bool:
            """Raise on any real control error, otherwise report the request as accepted."""

            bridge._was_control_action_successful(response_data, component_id, state)  # noqa: SLF001
            return True

        response_data = request(with_bypass=False)
        bypass_error = _find_bypass_error(response_data, component_id)

        # Either nothing had to be bypassed or bypassing is switched off in the configuration.
        if bypass_error is None or not force:
            return JablotronSectionControlResult(success=accepted(response_data))

        # Active devices block the section and bypassing is allowed, so confirm it the way the
        # app does once the user acknowledges its bypass prompt.
        _LOGGER.debug(
            "Section '%s' answered '%s', repeating the request with the bypass confirmed",
            component_id,
            bypass_error,
        )
        response_data = request(with_bypass=True)
        success = accepted(response_data)

        return JablotronSectionControlResult(success=success, bypassed=success, control_error=bypass_error)

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
