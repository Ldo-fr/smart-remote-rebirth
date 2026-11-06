"""Plateforme de capteurs pour Sevenhugs Smart Remote (SR1A).

Fournit les entités de télémétrie matérielle :
- SevenhugsBatterySensor : Niveau de batterie Li-ion (0-100%).
- SevenhugsWifiSignalSensor : Puissance du signal Wi-Fi / RSSI (dBm).
- SevenhugsActiveScreenSensor : Nom de l'écran ou de l'application active.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
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
    """Extrait de manière sécurisée le coordinateur depuis runtime_data ou hass.data."""
    # 1. Vérification dans entry.runtime_data (Home Assistant 2024.5+)
    if hasattr(entry, "runtime_data") and entry.runtime_data is not None:
        data = entry.runtime_data
        if isinstance(data, SevenhugsDataUpdateCoordinator):
            return data
        if hasattr(data, "coordinator") and data.coordinator is not None:
            return data.coordinator
        if isinstance(data, dict) and "coordinator" in data and data["coordinator"] is not None:
            return data["coordinator"]

    # 2. Repli dans hass.data[DOMAIN][entry.entry_id] (Home Assistant classique)
    domain_data = getattr(hass, "data", {}).get(DOMAIN, {}) if hasattr(hass, "data") and isinstance(hass.data, dict) else {}
    if isinstance(domain_data, dict) and entry.entry_id in domain_data:
        data = domain_data[entry.entry_id]
        if isinstance(data, SevenhugsDataUpdateCoordinator):
            return data
        if hasattr(data, "coordinator") and data.coordinator is not None:
            return data.coordinator
        if isinstance(data, dict) and "coordinator" in data and data["coordinator"] is not None:
            return data["coordinator"]

    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure la plateforme de capteurs Sevenhugs à partir d'une ConfigEntry."""
    coordinator = _get_coordinator(hass, entry)
    if coordinator is None:
        _LOGGER.error(
            "Impossible d'initialiser la plateforme sensor: coordinateur introuvable pour %s",
            entry.entry_id,
        )
        return

    entities: list[SensorEntity] = [
        SevenhugsBatterySensor(coordinator, entry),
        SevenhugsWifiSignalSensor(coordinator, entry),
        SevenhugsActiveScreenSensor(coordinator, entry),
    ]

    async_add_entities(entities)


class SevenhugsBatterySensor(SevenhugsBaseEntity, SensorEntity):
    """Capteur de niveau de charge de la batterie Sevenhugs Smart Remote."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: SevenhugsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise le capteur de batterie."""
        super().__init__(coordinator, entry, "battery")

    @property
    def native_value(self) -> int | None:
        """Retourne le pourcentage actuel de batterie (0-100%)."""
        return self.coordinator.battery


class SevenhugsWifiSignalSensor(SevenhugsBaseEntity, SensorEntity):
    """Capteur de force du signal Wi-Fi (RSSI) Sevenhugs Smart Remote."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SevenhugsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise le capteur de signal Wi-Fi."""
        super().__init__(coordinator, entry, "wifi_signal")

    @property
    def native_value(self) -> int | None:
        """Retourne la force du signal Wi-Fi en dBm (RSSI)."""
        return self.coordinator.wifi_signal


class SevenhugsActiveScreenSensor(SevenhugsBaseEntity, SensorEntity):
    """Capteur d'écran ou de mode actuellement actif sur la télécommande."""

    _attr_device_class = None
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cellphone"

    def __init__(
        self,
        coordinator: SevenhugsDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise le capteur d'écran actif."""
        super().__init__(coordinator, entry, "active_screen")

    @property
    def native_value(self) -> str | None:
        """Retourne le nom de l'écran ou de l'application active."""
        return self.coordinator.active_screen
