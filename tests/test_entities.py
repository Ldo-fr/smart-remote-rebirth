"""Tests unitaires et asynchrones pour SevenhugsBaseEntity et les plateformes diagnostiques (Phase 3 - R2, R3, R4 & R5).

Couvre l'intégralité des spécifications des entités et plateformes :
1. SevenhugsBaseEntity (entity.py) :
   - Héritage de CoordinatorEntity[SevenhugsDataUpdateCoordinator].
   - Propriété _attr_has_entity_name = True (Bronze standard Home Assistant).
   - Rattachement Device Registry (DeviceInfo : identifiers, manufacturer, model, name, sw_version, hw_version, connections).
   - Unique ID format : {serial_number}_{entity_key}.
   - Invariant de disponibilité (available = coordinator.last_update_success and _attr_available).
   - Synchronisation dynamique avec les notifications du coordinateur et écriture dans hass.states.
2. Plateforme Sensor (sensor.py) :
   - PARALLEL_UPDATES = 0.
   - SevenhugsBatterySensor : SensorDeviceClass.BATTERY, PERCENTAGE, SensorStateClass.MEASUREMENT, EntityCategory.DIAGNOSTIC.
   - SevenhugsWifiSignalSensor : SensorDeviceClass.SIGNAL_STRENGTH, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory.DIAGNOSTIC.
   - SevenhugsActiveScreenSensor : EntityCategory.DIAGNOSTIC, icône mdi:cellphone, native_value.
   - async_setup_entry : résolution du coordinateur (runtime_data ou hass.data) et enregistrement des 3 capteurs.
3. Plateforme Binary Sensor (binary_sensor.py) :
   - PARALLEL_UPDATES = 0.
   - SevenhugsChargingBinarySensor : BinarySensorDeviceClass.BATTERY_CHARGING, EntityCategory.DIAGNOSTIC, attribut docked.
   - SevenhugsConnectivityBinarySensor : BinarySensorDeviceClass.CONNECTIVITY, EntityCategory.DIAGNOSTIC.
     * INVARIANT CRITIQUE : available = True constant même lors des pannes pour rapporter l'état 'off'.
     * is_on = coordinator.is_connected, extra_state_attributes avec consecutive_failures.
   - async_setup_entry : résolution du coordinateur et enregistrement des 2 capteurs binaires.
4. Plateforme Button (button.py) :
   - PARALLEL_UPDATES = 0.
   - SevenhugsForceSyncButton : EntityCategory.DIAGNOSTIC, icône mdi:sync, async_press appelant async_request_refresh.
   - SevenhugsPingButton : EntityCategory.DIAGNOSTIC, icône mdi:network-outline, async_press (/api/hello puis refresh).
   - SevenhugsBackupConfigButton : EntityCategory.DIAGNOSTIC, icône mdi:cloud-download, async_press (/api/config), extra_state_attributes.
   - Matrice exhaustive de traduction d'exceptions vers HomeAssistantError.
   - async_setup_entry : résolution du coordinateur et enregistrement des 3 boutons.
5. Cycle de vie et intégration multi-plateformes (__init__.py) :
   - Forwarding vers les plateformes déclarées dans PLATFORMS (sensor, binary_sensor, button).
   - async_setup_entry et async_unload_entry avec nettoyage propre et sans fuite de socket/session.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Ingestion de l'environnement de mocks et shims Home Assistant
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
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
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, UpdateFailed

import tests.conftest
import aiohttp

from custom_components.smart_remote_rebirth import (
    DOMAIN,
    PLATFORMS,
    AsyncSmartRemoteClient,
    SevenhugsData,
    SevenhugsDataUpdateCoordinator,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.smart_remote_rebirth.binary_sensor import (
    PARALLEL_UPDATES as BINARY_SENSOR_PARALLEL_UPDATES,
    SevenhugsChargingBinarySensor,
    SevenhugsConnectivityBinarySensor,
    async_setup_entry as binary_sensor_setup_entry,
)
from custom_components.smart_remote_rebirth.button import (
    PARALLEL_UPDATES as BUTTON_PARALLEL_UPDATES,
    SevenhugsBackupConfigButton,
    SevenhugsForceSyncButton,
    SevenhugsPingButton,
    async_setup_entry as button_setup_entry,
)
from custom_components.smart_remote_rebirth.client import (
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from custom_components.smart_remote_rebirth.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DEFAULT_NAME,
    HARDWARE_VERSION,
    MANUFACTURER,
    MODEL_SR1A,
)
from custom_components.smart_remote_rebirth.entity import SevenhugsBaseEntity
from custom_components.smart_remote_rebirth.sensor import (
    PARALLEL_UPDATES as SENSOR_PARALLEL_UPDATES,
    SevenhugsActiveScreenSensor,
    SevenhugsBatterySensor,
    SevenhugsWifiSignalSensor,
    async_setup_entry as sensor_setup_entry,
)


class MockSmartRemoteClientForEntities:
    """Mock du client pour tester les entités et plateformes."""

    def __init__(self) -> None:
        self.request_calls: list[tuple[str, str]] = []
        self.side_effects: dict[str, Any] = {}
        self.default_responses: dict[str, Any] = {
            "/api/settings": {
                "battery": 82,
                "wifi_signal": -64,
                "charging": False,
                "docked": False,
                "active_screen": "MediaRemote",
                "serial_number": "SR1A_ENTITIES_999",
                "mac_address": "12:34:56:78:9A:BC",
                "firmware_version": "v0.76.4",
                "name": "Smart Remote Test",
                "remotes": [
                    {
                        "serial_number": "SR1A_ENTITIES_999",
                        "id": "5726",
                        "firmware_version": "v0.76.4",
                        "mac_address": "12:34:56:78:9A:BC",
                    }
                ],
            },
            "/api/hello": {"status": "ok"},
            "/api/config": {
                "general": {"language": "fr"},
                "screens": ["Home", "MediaRemote", "Lighting"],
                "version": 1,
            },
        }

    async def async_request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.request_calls.append((method, path))
        if path in self.side_effects:
            effect = self.side_effects[path]
            if isinstance(effect, type) and issubclass(effect, Exception):
                raise effect("Injected exception on endpoint")
            elif isinstance(effect, Exception):
                raise effect
            elif callable(effect):
                res = effect()
                if asyncio.iscoroutine(res):
                    return await res
                return res
        return self.default_responses.get(path, {})

    async def close(self) -> None:
        pass


class TestSevenhugsBaseEntity(unittest.IsolatedAsyncioTestCase):
    """Vérifie le comportement de SevenhugsBaseEntity et son rattachement Device Registry."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.client = MockSmartRemoteClientForEntities()
        self.entry = ConfigEntry(
            entry_id="base_entity_entry_001",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "SR1A_ENTITIES_999",
                CONF_REMOTE_ID: "5726",
                "mac_address": "12:34:56:78:9A:BC",
                "firmware_version": "v0.76.4",
            },
            unique_id="SR1A_ENTITIES_999",
        )
        self.coord = SevenhugsDataUpdateCoordinator(self.hass, self.client, self.entry)
        await self.coord.async_refresh()

    async def test_base_entity_inheritance_and_flags(self) -> None:
        """Vérifie l'héritage de CoordinatorEntity et les drapeaux has_entity_name et should_poll."""
        entity = SevenhugsBaseEntity(self.coord, self.entry, "test_feature")
        self.assertIsInstance(entity, CoordinatorEntity)
        self.assertTrue(entity.has_entity_name)
        self.assertFalse(entity.should_poll)
        self.assertEqual(entity.unique_id, "SR1A_ENTITIES_999_test_feature")
        self.assertEqual(entity.translation_key, "test_feature")
        self.assertIs(entity.client, self.client)
        self.assertEqual(entity.telemetry_data["battery"], 82)

    async def test_device_info_structure(self) -> None:
        """Vérifie que _attr_device_info contient toutes les métadonnées exigées par Home Assistant."""
        entity = SevenhugsBaseEntity(self.coord, self.entry, "test_feature")
        dev_info = entity.device_info
        self.assertIsInstance(dev_info, DeviceInfo)

        self.assertEqual(dev_info.identifiers, {(DOMAIN, "SR1A_ENTITIES_999")})
        self.assertEqual(dev_info.manufacturer, MANUFACTURER)
        self.assertEqual(dev_info.manufacturer, "Sevenhugs")
        self.assertEqual(dev_info.model, MODEL_SR1A)
        self.assertEqual(dev_info.model, "SR1A")
        self.assertEqual(dev_info.name, f"{DEFAULT_NAME} (5726)")
        self.assertEqual(dev_info.name, "Smart Remote (5726)")
        self.assertEqual(dev_info.sw_version, "v0.76.4")
        self.assertEqual(dev_info.hw_version, HARDWARE_VERSION)
        self.assertEqual(dev_info.hw_version, "FCC ID: 2AEVC-SR1A")
        self.assertIn((CONNECTION_NETWORK_MAC, "12:34:56:78:9A:BC"), dev_info.connections)

    async def test_device_info_without_mac_address(self) -> None:
        """Vérifie que DeviceInfo reste valide sans adresse MAC."""
        entry_no_mac = ConfigEntry(
            entry_id="base_no_mac",
            domain=DOMAIN,
            title="Remote No Mac",
            data={CONF_SERIAL_NUMBER: "SN_NO_MAC", CONF_REMOTE_ID: "1111"},
            unique_id="SN_NO_MAC",
        )
        coord_no_mac = SevenhugsDataUpdateCoordinator(self.hass, self.client, entry_no_mac)
        # Forcer data sans mac_address
        coord_no_mac.data = {"serial_number": "SN_NO_MAC"}

        entity = SevenhugsBaseEntity(coord_no_mac, entry_no_mac, "feature_x")
        self.assertEqual(entity.device_info.connections, set())
        self.assertEqual(entity.device_info.identifiers, {(DOMAIN, "SN_NO_MAC")})

    async def test_base_entity_availability_logic(self) -> None:
        """Vérifie que la disponibilité de l'entité dépend de last_update_success du coordinateur."""
        entity = SevenhugsBaseEntity(self.coord, self.entry, "feature_y")

        # Nominal -> disponible
        self.assertTrue(entity.available)

        # Échec toléré de veille (1er et 2ème échec) -> last_update_success reste True -> available reste True
        self.client.side_effects["/api/settings"] = SevenhugsConnectionError("Sleep")
        await self.coord.async_refresh()
        self.assertEqual(self.coord.consecutive_failures, 1)
        self.assertTrue(entity.available)

        await self.coord.async_refresh()
        self.assertEqual(self.coord.consecutive_failures, 2)
        self.assertTrue(entity.available)

        # 3ème échec -> UpdateFailed -> last_update_success = False -> entity devient indisponible
        await self.coord.async_refresh()
        self.assertEqual(self.coord.consecutive_failures, 3)
        self.assertFalse(self.coord.last_update_success)
        self.assertFalse(entity.available)

        # Rétablissement de la connexion -> redevient disponible
        self.client.side_effects.clear()
        await self.coord.async_refresh()
        self.assertTrue(self.coord.last_update_success)
        self.assertTrue(entity.available)

        # Si _attr_available est positionné manuellement à False -> indisponible
        entity._attr_available = False
        self.assertFalse(entity.available)

    async def test_entity_coordinator_listener_and_state_machine_write(self) -> None:
        """Vérifie l'enregistrement de l'écouteur et l'écriture dans hass.states lors d'une mise à jour."""
        entity = SevenhugsBaseEntity(self.coord, self.entry, "battery")
        entity.hass = self.hass
        entity.entity_id = "sensor.sevenhugs_battery"

        await entity.async_added_to_hass()

        # Rafraîchissement des données -> l'entité doit écrire son état dans hass.states
        await self.coord.async_refresh()
        ha_state = self.hass.states.get("sensor.sevenhugs_battery")
        self.assertIsNotNone(ha_state)

        # Retrait de l'entité -> écouteur nettoyé
        await entity.async_will_remove_from_hass()
        # Un refresh ultérieur ne doit plus provoquer d'erreur
        await self.coord.async_refresh()


class TestSevenhugsSensorPlatform(unittest.IsolatedAsyncioTestCase):
    """Vérifie la plateforme de capteurs (Battery, WifiSignal, ActiveScreen) et son initialisation."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.client = MockSmartRemoteClientForEntities()
        self.entry = ConfigEntry(
            entry_id="sensor_entry_001",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "SR1A_SENSOR_123",
                CONF_REMOTE_ID: "5726",
            },
            unique_id="SR1A_SENSOR_123",
        )
        self.client.default_responses["/api/settings"]["serial_number"] = "SR1A_SENSOR_123"
        self.coord = SevenhugsDataUpdateCoordinator(self.hass, self.client, self.entry)
        await self.coord.async_refresh()

    def test_sensor_parallel_updates_constant(self) -> None:
        """Vérifie que PARALLEL_UPDATES vaut 0 pour éviter les pollings concurrents."""
        self.assertEqual(SENSOR_PARALLEL_UPDATES, 0)

    async def test_battery_sensor_specifications(self) -> None:
        """Vérifie les caractéristiques normatives du capteur de batterie."""
        sensor = SevenhugsBatterySensor(self.coord, self.entry)
        self.assertIsInstance(sensor, SensorEntity)
        self.assertEqual(sensor.device_class, SensorDeviceClass.BATTERY)
        self.assertEqual(sensor.native_unit_of_measurement, PERCENTAGE)
        self.assertEqual(sensor.state_class, SensorStateClass.MEASUREMENT)
        self.assertEqual(sensor.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(sensor.suggested_display_precision, 0)
        self.assertEqual(sensor.unique_id, "SR1A_SENSOR_123_battery")
        self.assertEqual(sensor.native_value, 82)
        self.assertEqual(sensor.state, 82)

    async def test_wifi_signal_sensor_specifications(self) -> None:
        """Vérifie les caractéristiques normatives du capteur de force du signal Wi-Fi."""
        sensor = SevenhugsWifiSignalSensor(self.coord, self.entry)
        self.assertIsInstance(sensor, SensorEntity)
        self.assertEqual(sensor.device_class, SensorDeviceClass.SIGNAL_STRENGTH)
        self.assertEqual(sensor.native_unit_of_measurement, SIGNAL_STRENGTH_DECIBELS_MILLIWATT)
        self.assertEqual(sensor.state_class, SensorStateClass.MEASUREMENT)
        self.assertEqual(sensor.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(sensor.unique_id, "SR1A_SENSOR_123_wifi_signal")
        self.assertEqual(sensor.native_value, -64)
        self.assertEqual(sensor.state, -64)

    async def test_active_screen_sensor_specifications(self) -> None:
        """Vérifie les caractéristiques normatives du capteur d'écran ou mode actif."""
        sensor = SevenhugsActiveScreenSensor(self.coord, self.entry)
        self.assertIsInstance(sensor, SensorEntity)
        self.assertIsNone(sensor.device_class)
        self.assertEqual(sensor.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(sensor.icon, "mdi:cellphone")
        self.assertEqual(sensor.unique_id, "SR1A_SENSOR_123_active_screen")
        self.assertEqual(sensor.native_value, "MediaRemote")
        self.assertEqual(sensor.state, "MediaRemote")

    async def test_sensor_async_setup_entry_via_runtime_data(self) -> None:
        """Vérifie l'enregistrement des 3 capteurs via entry.runtime_data."""
        self.entry.runtime_data = SevenhugsData(client=self.client, session=MagicMock(), coordinator=self.coord)
        added_entities: list[SensorEntity] = []

        def add_entities_callback(new_entities: list[SensorEntity]) -> None:
            added_entities.extend(new_entities)

        await sensor_setup_entry(self.hass, self.entry, add_entities_callback)

        self.assertEqual(len(added_entities), 3)
        classes = {type(e) for e in added_entities}
        self.assertEqual(
            classes,
            {SevenhugsBatterySensor, SevenhugsWifiSignalSensor, SevenhugsActiveScreenSensor},
        )

    async def test_sensor_async_setup_entry_via_hass_data_fallback(self) -> None:
        """Vérifie le repli sur hass.data[DOMAIN][entry_id] si runtime_data est absent."""
        self.entry.runtime_data = None
        self.hass.data[DOMAIN] = {self.entry.entry_id: {"coordinator": self.coord}}

        added_entities: list[SensorEntity] = []
        await sensor_setup_entry(self.hass, self.entry, added_entities.extend)
        self.assertEqual(len(added_entities), 3)

    async def test_sensor_async_setup_entry_missing_coordinator(self) -> None:
        """Vérifie la robustesse si le coordinateur est introuvable (aucune entité ajoutée)."""
        self.entry.runtime_data = None
        self.hass.data.clear()

        added_entities: list[SensorEntity] = []
        await sensor_setup_entry(self.hass, self.entry, added_entities.extend)
        self.assertEqual(len(added_entities), 0)


class TestSevenhugsBinarySensorPlatform(unittest.IsolatedAsyncioTestCase):
    """Vérifie la plateforme de capteurs binaires (Charging, Connectivity) et l'invariant de disponibilité."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.client = MockSmartRemoteClientForEntities()
        self.entry = ConfigEntry(
            entry_id="bsensor_entry_001",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "SR1A_BSENSOR_456",
                CONF_REMOTE_ID: "5726",
            },
            unique_id="SR1A_BSENSOR_456",
        )
        self.client.default_responses["/api/settings"]["serial_number"] = "SR1A_BSENSOR_456"
        self.coord = SevenhugsDataUpdateCoordinator(self.hass, self.client, self.entry)
        await self.coord.async_refresh()

    def test_binary_sensor_parallel_updates_constant(self) -> None:
        """Vérifie que PARALLEL_UPDATES vaut 0."""
        self.assertEqual(BINARY_SENSOR_PARALLEL_UPDATES, 0)

    async def test_charging_binary_sensor_specifications(self) -> None:
        """Vérifie le capteur de charge / socle."""
        sensor = SevenhugsChargingBinarySensor(self.coord, self.entry)
        self.assertIsInstance(sensor, BinarySensorEntity)
        self.assertEqual(sensor.device_class, BinarySensorDeviceClass.BATTERY_CHARGING)
        self.assertEqual(sensor.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(sensor.unique_id, "SR1A_BSENSOR_456_charging")
        self.assertFalse(sensor.is_on)
        self.assertEqual(sensor.state, "off")
        self.assertEqual(sensor.extra_state_attributes, {"docked": False})

        # Modification des données de télémétrie : mise en charge sur socle
        self.coord.data["charging"] = True
        self.coord.data["docked"] = True
        self.assertTrue(sensor.is_on)
        self.assertEqual(sensor.state, "on")
        self.assertEqual(sensor.extra_state_attributes, {"docked": True})

    async def test_connectivity_binary_sensor_critical_availability_invariant(self) -> None:
        """Vérifie l'invariant critique : connectivity reste disponible (available=True) pour rapporter 'off'."""
        sensor = SevenhugsConnectivityBinarySensor(self.coord, self.entry)
        self.assertIsInstance(sensor, BinarySensorEntity)
        self.assertEqual(sensor.device_class, BinarySensorDeviceClass.CONNECTIVITY)
        self.assertEqual(sensor.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(sensor.unique_id, "SR1A_BSENSOR_456_connectivity")

        # 1. État nominal connecté
        self.assertTrue(sensor.available)
        self.assertTrue(sensor.is_on)
        self.assertEqual(sensor.state, "on")
        self.assertEqual(sensor.extra_state_attributes, {"consecutive_failures": 0})

        # 2. Veille tolérée (échecs 1 et 2)
        self.client.side_effects["/api/settings"] = SevenhugsConnectionError("Sleep")
        await self.coord.async_refresh()
        self.assertTrue(sensor.available)
        self.assertTrue(sensor.is_on)
        self.assertEqual(sensor.state, "on")
        self.assertEqual(sensor.extra_state_attributes, {"consecutive_failures": 1})

        await self.coord.async_refresh()
        self.assertTrue(sensor.available)
        self.assertTrue(sensor.is_on)
        self.assertEqual(sensor.extra_state_attributes, {"consecutive_failures": 2})

        # 3. 3ème échec : Perte de liaison avérée
        await self.coord.async_refresh()
        self.assertFalse(self.coord.last_update_success)
        self.assertFalse(self.coord.is_connected)

        # INVARIANT FONDAMENTAL : Le capteur de connectivité reste STRICTEMENT disponible (available=True)
        # alors que tous les autres capteurs deviennent indisponibles !
        self.assertTrue(sensor.available, "Le capteur de connectivité DOIT rester disponible pendant une déconnexion")
        self.assertFalse(sensor.is_on)
        self.assertEqual(sensor.state, "off")
        self.assertEqual(sensor.extra_state_attributes, {"consecutive_failures": 3})

        # 4. Rétablissement
        self.client.side_effects.clear()
        await self.coord.async_refresh()
        self.assertTrue(sensor.available)
        self.assertTrue(sensor.is_on)
        self.assertEqual(sensor.state, "on")
        self.assertEqual(sensor.extra_state_attributes, {"consecutive_failures": 0})

    async def test_binary_sensor_async_setup_entry(self) -> None:
        """Vérifie l'enregistrement des 2 capteurs binaires."""
        self.entry.runtime_data = SevenhugsData(client=self.client, session=MagicMock(), coordinator=self.coord)
        added_entities: list[BinarySensorEntity] = []

        await binary_sensor_setup_entry(self.hass, self.entry, added_entities.extend)

        self.assertEqual(len(added_entities), 2)
        classes = {type(e) for e in added_entities}
        self.assertEqual(
            classes,
            {SevenhugsChargingBinarySensor, SevenhugsConnectivityBinarySensor},
        )


class TestSevenhugsButtonPlatform(unittest.IsolatedAsyncioTestCase):
    """Vérifie la plateforme de boutons d'action (ForceSync, Ping, BackupConfig) et leurs traductions d'erreurs."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.client = MockSmartRemoteClientForEntities()
        self.entry = ConfigEntry(
            entry_id="button_entry_001",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.100",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "SR1A_BUTTON_789",
                CONF_REMOTE_ID: "5726",
            },
            unique_id="SR1A_BUTTON_789",
        )
        self.client.default_responses["/api/settings"]["serial_number"] = "SR1A_BUTTON_789"
        self.coord = SevenhugsDataUpdateCoordinator(self.hass, self.client, self.entry)
        await self.coord.async_refresh()

    def test_button_parallel_updates_constant(self) -> None:
        """Vérifie que PARALLEL_UPDATES vaut 0."""
        self.assertEqual(BUTTON_PARALLEL_UPDATES, 0)

    async def test_force_sync_button_nominal_and_error(self) -> None:
        """Vérifie le bouton de synchronisation forcée."""
        button = SevenhugsForceSyncButton(self.coord, self.entry)
        self.assertIsInstance(button, ButtonEntity)
        self.assertEqual(button.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(button.icon, "mdi:sync")
        self.assertEqual(button.unique_id, "SR1A_BUTTON_789_force_sync")

        # Appel nominal
        with patch.object(self.coord, "async_request_refresh", AsyncMock()) as mock_refresh:
            await button.async_press()
            mock_refresh.assert_awaited_once()

        # Traduction d'erreur HomeAssistantError
        with patch.object(self.coord, "async_request_refresh", AsyncMock(side_effect=RuntimeError("Coord lock failed"))):
            with self.assertRaises(HomeAssistantError) as ctx:
                await button.async_press()
            self.assertIn("Échec de la synchronisation forcée", str(ctx.exception))

    async def test_ping_button_nominal_sequence(self) -> None:
        """Vérifie le séquencement nominal du bouton Ping : /api/hello puis rafraîchissement."""
        button = SevenhugsPingButton(self.coord, self.entry)
        self.assertIsInstance(button, ButtonEntity)
        self.assertEqual(button.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(button.icon, "mdi:network-outline")
        self.assertEqual(button.unique_id, "SR1A_BUTTON_789_ping")

        with patch.object(self.coord, "async_request_refresh", AsyncMock()) as mock_refresh:
            await button.async_press()
            self.assertIn(("GET", "/api/hello"), self.client.request_calls)
            mock_refresh.assert_awaited_once()

    async def test_ping_button_error_translations(self) -> None:
        """Vérifie la matrice de traduction d'erreurs pour le bouton Ping."""
        button = SevenhugsPingButton(self.coord, self.entry)

        # 1. SevenhugsAuthError
        self.client.side_effects["/api/hello"] = SevenhugsAuthError("Session invalid")
        with self.assertRaises(HomeAssistantError) as ctx1:
            await button.async_press()
        self.assertIn("Échec d'authentification lors du ping", str(ctx1.exception))

        # 2. Erreurs réseau / communication (_COMMUNICATION_ERRORS)
        for net_err in [
            SevenhugsConnectionError("Network disconnected"),
            TimeoutError("Socket timeout"),
            ConnectionResetError("RST packet"),
            OSError("Host down"),
        ]:
            self.client.side_effects["/api/hello"] = net_err
            with self.assertRaises(HomeAssistantError) as ctx2:
                await button.async_press()
            self.assertIn("Impossible de joindre la télécommande", str(ctx2.exception))

        # 3. Exception inattendue
        self.client.side_effects["/api/hello"] = ValueError("Unexpected corruption")
        with self.assertRaises(HomeAssistantError) as ctx3:
            await button.async_press()
        self.assertIn("Erreur inattendue lors du ping", str(ctx3.exception))

    async def test_backup_config_button_nominal_and_state_attributes(self) -> None:
        """Vérifie la sauvegarde de configuration et l'exposition des métadonnées dans extra_state_attributes."""
        button = SevenhugsBackupConfigButton(self.coord, self.entry)
        self.assertIsInstance(button, ButtonEntity)
        self.assertEqual(button.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(button.icon, "mdi:cloud-download")
        self.assertEqual(button.unique_id, "SR1A_BUTTON_789_backup_config")

        # Avant le premier appui : aucun attribut de sauvegarde
        self.assertEqual(button.extra_state_attributes, {})

        # Exécution de la sauvegarde
        await button.async_press()
        self.assertIn(("GET", "/api/config"), self.client.request_calls)

        attrs = button.extra_state_attributes
        self.assertIn("last_backup_sections", attrs)
        self.assertIn("last_backup_size", attrs)
        self.assertEqual(set(attrs["last_backup_sections"]), {"general", "screens", "version"})
        self.assertEqual(attrs["last_backup_size"], 3)

    async def test_backup_config_button_error_translations(self) -> None:
        """Vérifie la matrice de traduction d'erreurs pour le bouton Backup."""
        button = SevenhugsBackupConfigButton(self.coord, self.entry)

        # 1. SevenhugsAuthError
        self.client.side_effects["/api/config"] = SevenhugsAuthError("Token expired")
        with self.assertRaises(HomeAssistantError) as ctx1:
            await button.async_press()
        self.assertIn("Échec d'authentification lors de la sauvegarde", str(ctx1.exception))

        # 2. Erreurs réseau
        self.client.side_effects["/api/config"] = SevenhugsConnectionError("Connection timeout")
        with self.assertRaises(HomeAssistantError) as ctx2:
            await button.async_press()
        self.assertIn("Impossible de joindre la télécommande Sevenhugs pour sauvegarder", str(ctx2.exception))

        # 3. Erreur inattendue
        self.client.side_effects["/api/config"] = TypeError("Serialization bug")
        with self.assertRaises(HomeAssistantError) as ctx3:
            await button.async_press()
        self.assertIn("Erreur inattendue lors de la sauvegarde", str(ctx3.exception))

    async def test_button_async_setup_entry(self) -> None:
        """Vérifie l'enregistrement des 3 boutons de diagnostic."""
        self.entry.runtime_data = SevenhugsData(client=self.client, session=MagicMock(), coordinator=self.coord)
        added_entities: list[ButtonEntity] = []

        await button_setup_entry(self.hass, self.entry, added_entities.extend)

        self.assertEqual(len(added_entities), 3)
        classes = {type(e) for e in added_entities}
        self.assertEqual(
            classes,
            {SevenhugsForceSyncButton, SevenhugsPingButton, SevenhugsBackupConfigButton},
        )


class TestComponentLifecycleAndPlatformForwarding(unittest.IsolatedAsyncioTestCase):
    """Vérifie le cycle de vie complet du composant et le forwarding vers sensor, binary_sensor et button."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = tests.conftest.MockSmartRemoteServer()
        await self.server.start()

        self.entry = ConfigEntry(
            entry_id="lifecycle_entry_full_001",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: self.server.host,
                CONF_PORT: self.server.port,
                CONF_SERIAL_NUMBER: "a06fe048b48b260139",
                CONF_REMOTE_ID: "5726",
                "controller_id": "test_ctrl_id",
                "client_private_key": "01" * 32,
                "client_public_key": "02" * 32,
                "remote_public_key": self.server.remote_pk.hex(),
                "session_write_key": "03" * 32,
                "session_read_key": "04" * 32,
            },
            unique_id="a06fe048b48b260139",
        )

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_async_setup_and_unload_entry_multiplatform(self) -> None:
        """Vérifie l'initialisation complète, le forwarding multiplateformes et le déchargement propre."""
        setup_ok = await async_setup_entry(self.hass, self.entry)
        self.assertTrue(setup_ok)

        # Vérification du stockage des données d'exécution dans hass.data et runtime_data
        self.assertIn(DOMAIN, self.hass.data)
        self.assertIn(self.entry.entry_id, self.hass.data[DOMAIN])
        runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
        self.assertIsInstance(runtime_data, SevenhugsData)
        self.assertIsNotNone(runtime_data.coordinator)
        self.assertIsNotNone(runtime_data.client)
        self.assertIsNotNone(runtime_data.session)

        # Vérification du forwarding vers PLATFORMS (sensor, binary_sensor, button)
        forwarded = self.hass.config_entries.forwarded_platforms.get(self.entry.entry_id, [])
        self.assertEqual(len(forwarded), 3)
        self.assertIn(Platform.SENSOR, forwarded)
        self.assertIn(Platform.BINARY_SENSOR, forwarded)
        self.assertIn(Platform.BUTTON, forwarded)

        # Déchargement propre
        unload_ok = await async_unload_entry(self.hass, self.entry)
        self.assertTrue(unload_ok)

        # Plateformes déchargées
        unloaded = self.hass.config_entries.unloaded_platforms.get(self.entry.entry_id, [])
        self.assertEqual(len(unloaded), 3)
        self.assertIn(Platform.SENSOR, unloaded)
        self.assertIn(Platform.BINARY_SENSOR, unloaded)
        self.assertIn(Platform.BUTTON, unloaded)

        # Données nettoyées
        self.assertNotIn(self.entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(getattr(self.entry, "runtime_data", None))


if __name__ == "__main__":
    unittest.main()
