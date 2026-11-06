"""Plateforme Binary Sensor pour l'intégration Sevenhugs Smart Remote (SR1A).

Fournit les capteurs binaires diagnostiques et de statut matériel :
- État de charge / dock (BinarySensorDeviceClass.BATTERY_CHARGING)
- Connectivité réseau et résilience de veille (BinarySensorDeviceClass.CONNECTIVITY)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SevenhugsDataUpdateCoordinator
from .entity import SevenhugsBaseEntity

_LOGGER = logging.getLogger(__name__)

# Désactive les mises à jour en parallèle car les entités sont alimentées
# exclusivement par les notifications push du SevenhugsDataUpdateCoordinator.
PARALLEL_UPDATES = 0


def _get_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> SevenhugsDataUpdateCoordinator | None:
    """Récupère le coordinateur depuis runtime_data ou hass.data de manière résiliente."""
    # 1. Vérification dans entry.runtime_data (HA 2024+)
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is not None:
        if isinstance(runtime_data, SevenhugsDataUpdateCoordinator):
            return runtime_data
        coord = getattr(runtime_data, "coordinator", None)
        if coord is not None:
            return coord
        if isinstance(runtime_data, dict) and "coordinator" in runtime_data and runtime_data["coordinator"] is not None:
            return runtime_data["coordinator"]

    # 2. Vérification dans hass.data[DOMAIN][entry.entry_id] (compatibilité standard)
    domain_data = getattr(hass, "data", {}).get(DOMAIN, {}) if hasattr(hass, "data") and isinstance(hass.data, dict) else {}
    if isinstance(domain_data, dict) and entry.entry_id in domain_data:
        entry_data = domain_data[entry.entry_id]
        if isinstance(entry_data, SevenhugsDataUpdateCoordinator):
            return entry_data
        coord = getattr(entry_data, "coordinator", None)
        if coord is not None:
            return coord
        if isinstance(entry_data, dict) and "coordinator" in entry_data and entry_data["coordinator"] is not None:
            return entry_data["coordinator"]

    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure la plateforme binary_sensor pour Sevenhugs Smart Remote."""
    coordinator = _get_coordinator(hass, entry)
    if coordinator is None:
        _LOGGER.error(
            "Impossible d'initialiser la plateforme binary_sensor: coordinateur introuvable pour %s",
            entry.entry_id,
        )
        return

    entities: list[BinarySensorEntity] = [
        SevenhugsChargingBinarySensor(coordinator, entry),
        SevenhugsConnectivityBinarySensor(coordinator, entry),
    ]

    async_add_entities(entities)


class SevenhugsChargingBinarySensor(SevenhugsBaseEntity, BinarySensorEntity):
    """Capteur binaire indiquant l'état de charge ou de placement sur le socle/dock."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: SevenhugsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise le capteur binaire de charge."""
        super().__init__(coordinator, entry, entity_key="charging")

    @property
    def is_on(self) -> bool:
        """Retourne True si la télécommande est en charge ou posée sur son socle."""
        return bool(self.coordinator.charging)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Retourne les attributs d'état additionnels (ex: état docked spécifique)."""
        data = self.coordinator.data or self.coordinator.cached_data or {}
        return {
            "docked": bool(data.get("docked", False)),
        }


class SevenhugsConnectivityBinarySensor(SevenhugsBaseEntity, BinarySensorEntity):
    """Capteur binaire indiquant l'état de connectivité réseau de la télécommande."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SevenhugsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise le capteur binaire de connectivité."""
        super().__init__(coordinator, entry, entity_key="connectivity")

    @property
    def available(self) -> bool:
        """Le capteur de connectivité reste disponible pour rapporter fidèlement l'état déconnecté."""
        return True

    @property
    def is_on(self) -> bool:
        """Retourne True si la télécommande est connectée ou en veille tolérée."""
        return bool(self.coordinator.is_connected)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Retourne le nombre d'échecs de communication consécutifs."""
        return {
            "consecutive_failures": getattr(self.coordinator, "consecutive_failures", 0),
        }
