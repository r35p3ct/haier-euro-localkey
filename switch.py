"""Switch entities for the AC's boolean features (all confirmed).

Each switch flips one grSetDAC bit via the coordinator's group-set control path; the library refuses
any field that wasn't confirmed, so only these fire. App labels: strong=rapidMode,
quiet=muteStatus, health=healthMode, sleep=silentSleepStatus, lamp=screenDisplayStatus (the light).
On compact-12 the group-set carries the same toggles at the family's own word-10 positions
(rapid/mute/lamp confirmed on hardware; the lamp bit inverted), so `supports_field` admits them
there too and the descriptions below serve both families.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from haismart_hrdp import PANEL_BOOL_CONTROLS
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import HaismartConfigEntry, HaismartCoordinator
from .entity import HaismartEntity


@dataclass(frozen=True, kw_only=True)
class HaismartSwitchDescription(SwitchEntityDescription):
    """A boolean grSetDAC field exposed as a switch.

    ``exclusive_with`` names sibling fields the unit refuses to see set alongside this one (the
    healthy-airflow up/down pair): turning this on clears them in the SAME op, since an op that
    leaves both set is ignored by the unit.
    """

    field: str
    exclusive_with: tuple[str, ...] = ()


# Names live in strings.json via `translation_key`, and icons in icons.json, which is what makes
# them translatable at all -- hardcoded `name=` strings can only ever be English. The `key` values
# are unchanged, so `unique_id` and therefore existing entity ids survive.
SWITCHES: tuple[HaismartSwitchDescription, ...] = (
    HaismartSwitchDescription(key="strong", field="rapidMode", translation_key="strong"),
    HaismartSwitchDescription(key="quiet", field="muteStatus", translation_key="quiet"),
    HaismartSwitchDescription(key="health", field="healthMode", translation_key="health"),
    HaismartSwitchDescription(key="sleep", field="silentSleepStatus", translation_key="sleep"),
    HaismartSwitchDescription(
        key="lamp", field="screenDisplayStatus", translation_key="lamp"
    ),
    # The healthy-airflow direction pair (compact-12 word 10 bits 3/4): mutually exclusive on the
    # unit itself — an op with both set is ignored — so each clears its sibling in the same write.
    HaismartSwitchDescription(
        key="comfort_flow_up",
        field="healthyWindUp",
        translation_key="comfort_flow_up",
        exclusive_with=("healthyWindDown",),
    ),
    HaismartSwitchDescription(
        key="comfort_flow_down",
        field="healthyWindDown",
        translation_key="comfort_flow_down",
        exclusive_with=("healthyWindUp",),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HaismartConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    # Only the toggles this unit's report family can actually write. The classic family has all
    # five; compact-12 writes the same toggles at its own word-10 positions plus the healthy-airflow
    # pair (confirmed on hardware), and health via its single-parameter command. Creating a switch
    # for a field a family cannot place produced a control that read `unknown` forever and raised
    # the moment it was touched — hence the gate. Same rule the readings follow: expose what the
    # unit really has, not a button that does nothing.
    entities = [
        HaismartSwitch(coordinator, desc)
        for desc in SWITCHES
        if coordinator.supports_field(desc.field)
    ]
    # The rest of the panel's boolean control surface: functions the app renders a switch for and
    # this unit declares (not invisible), positioned by the invariant frame. Offered the way the app
    # offers them -- declaration, not a capture apiece -- rather than left as read-only sensors.
    entities.extend(
        HaismartSwitch(
            coordinator,
            HaismartSwitchDescription(
                key=PANEL_BOOL_CONTROLS[field],
                field=field,
                translation_key=PANEL_BOOL_CONTROLS[field],
            ),
        )
        for field in coordinator.panel_switch_fields()
    )
    async_add_entities(entities)


class HaismartSwitch(HaismartEntity, SwitchEntity):
    entity_description: HaismartSwitchDescription

    def __init__(
        self, coordinator: HaismartCoordinator, description: HaismartSwitchDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_id}_{description.key}"

    @property
    def available(self) -> bool:
        """Available whenever the unit is; a setting it currently ignores is not a fault.

        Boost in dry mode, quiet in fan-only, anything at all while a fault is active: the model
        states which settings a unit discards in which state. Those used to make the switch
        unavailable, which reads in the dashboard as something being broken, and takes the reading
        and its history away for as long as the mode lasts. The state is still perfectly readable
        throughout.

        So the switch stays, showing the truth, and the *command* is refused with the model's own
        reason (:meth:`HaismartEntity.raise_if_locked`). Nothing silently does nothing, which is the
        thing actually worth avoiding.
        """
        return super().available

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.current_field(self.entity_description.field)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.raise_if_locked(self.entity_description.field)
        changes: dict[str, int] = {self.entity_description.field: 1}
        # Mutually exclusive siblings ride in the same op: the unit ignores a group-set that
        # leaves both bits of such a pair set (compact-12's healthy-airflow up/down).
        for sibling in self.entity_description.exclusive_with:
            changes[sibling] = 0
        await self.coordinator.async_send_control(changes)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.raise_if_locked(self.entity_description.field)
        await self.coordinator.async_send_control({self.entity_description.field: 0})
