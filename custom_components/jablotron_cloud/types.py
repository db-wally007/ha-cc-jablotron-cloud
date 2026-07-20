"""Types for Jablotron Cloud integration."""

from dataclasses import dataclass
from typing import TypedDict

from jablotronpy import JablotronProgrammableGates, JablotronSections, JablotronThermoDevice


@dataclass(frozen=True)
class JablotronServiceCapabilities:
    """Endpoints a Jablotron service actually provides data for (discovered during setup)."""

    has_gates: bool
    has_sections: bool
    has_thermo: bool


class JablotronServiceData(TypedDict):
    """Typed dictionary representing data for a single Jablotron service."""

    name: str
    type: str
    firmware: str
    alarm: JablotronSections
    gates: JablotronProgrammableGates
    thermo: list[JablotronThermoDevice]
