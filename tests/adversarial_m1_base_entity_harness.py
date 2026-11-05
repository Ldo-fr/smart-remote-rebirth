"""Banc d'essais contradictoires et de stress empirique pour le Jalier M1.2.

Auteur: Challenger M1.2 (teamwork_challenger)
Rôles: critic, specialist

Ce banc d'essais teste de manière exhaustive et contradictoire :
1. Propriétés de SevenhugsBaseEntity et contrats Home Assistant Bronze (ADR-0007).
2. Rattachement au Device Registry (DeviceInfo) sous conditions normales et adverses
   (MAC valide, MAC absente, remote_id vide, serial vide, valeurs None).
3. Suivi dynamique de disponibilité (disponibilité coordonnateur / seuil de 3 échecs
   en Deep Sleep, réveil événementiel, démarrage à froid, erreurs inattendues, auth failure).
4. Résilience face aux données partielles, corrompues ou nulles.
5. Shims et mocks Home Assistant (Platform, EntityCategory, DeviceInfo, Entity,
   CoordinatorEntity[Any], DataUpdateCoordinator[Any], MockStateMachine).
6. Héritage multiple (SevenhugsBaseEntity combiné à SensorEntity, BinarySensorEntity,
   ButtonEntity), MRO, cycle de vie hass et écriture d'état async_write_ha_state.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any

# Configuration du PYTHONPATH
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    Platform,
    UnitOfSignalStrength,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)

from custom_components.sevenhugs_smart_remote.client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from custom_components.sevenhugs_smart_remote.const import (
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DEFAULT_NAME,
    DOMAIN,
    HARDWARE_VERSION,
    MANUFACTURER,
    MODEL_SR1A,
)
from custom_components.sevenhugs_smart_remote.coordinator import (
    DEFAULT_COORDINATOR_UPDATE_INTERVAL,
    SLEEP_CONSECUTIVE_FAILURES_THRESHOLD,
    SevenhugsDataUpdateCoordinator,
)
from custom_components.sevenhugs_smart_remote.entity import SevenhugsBaseEntity


class MockClient:
    """Client simulé pour tester le comportement du coordinateur."""

    def __init__(self, response_data: dict[str, Any] | None = None) -> None:
        self.response_data = response_data if response_data is not None else {
            "battery": 88,
            "wifi_signal": -60,
            "charging": False,
            "docked": False,
            "active_screen": "Media",
            "firmware_version": "0.76.4",
            "name": "Living Room Remote",
        }
        self.exception_to_raise: Exception | None = None
        self.requests_log: list[tuple[str, str, Any]] = []

    async def async_request(self, method: str, path: str, json_data: Any = None) -> Any:
        self.requests_log.append((method, path, json_data))
        if self.exception_to_raise:
            raise self.exception_to_raise
        return self.response_data


class AdversarialBaseEntityHarness:
    """Harnais contradictoire d'exécution empirique pour M1.2."""

    def __init__(self) -> None:
        self.passed: int = 0
        self.failed: int = 0
        self.warnings: list[str] = []

    def assert_true(self, condition: bool, msg: str) -> None:
        if condition:
            self.passed += 1
            print(f"  [PASS] {msg}")
        else:
            self.failed += 1
            print(f"  [FAIL] {msg}")
            raise AssertionError(msg)

    def assert_equal(self, actual: Any, expected: Any, msg: str) -> None:
        if actual == expected:
            self.passed += 1
            print(f"  [PASS] {msg}")
        else:
            self.failed += 1
            err = f"{msg} (attendu: {expected!r}, obtenu: {actual!r})"
            print(f"  [FAIL] {err}")
            raise AssertionError(err)

    # =========================================================================
    # Axe 1 : Propriétés et contrats de SevenhugsBaseEntity
    # =========================================================================

    async def test_axis1_base_entity_contracts(self) -> None:
        print("\n=== AXE 1 : Propriétés et Contrats de SevenhugsBaseEntity ===")
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="test_entry_01",
            domain=DOMAIN,
            title="My Remote",
            data={
                CONF_SERIAL_NUMBER: "SR1A11223344",
                CONF_REMOTE_ID: "4321",
                "mac_address": "00:11:22:33:44:55",
            },
        )
        client = MockClient({
            "battery": 88,
            "wifi_signal": -60,
            "charging": False,
            "docked": False,
            "active_screen": "Media",
            "serial_number": "SR1A11223344",
            "mac_address": "00:11:22:33:44:55",
            "firmware_version": "0.76.4",
            "name": "Living Room Remote",
        })
        coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
        await coord.async_refresh()

        desc = EntityDescription(key="battery", name="Battery Level")
        entity = SevenhugsBaseEntity(coord, entry, "battery", description=desc)

        # 1. Contrats Home Assistant Bronze ADR-0007
        self.assert_true(entity.has_entity_name is True, "has_entity_name est True (ADR-0007)")
        self.assert_true(entity.should_poll is False, "should_poll est False (pure push via coordinateur)")
        self.assert_equal(entity.unique_id, "SR1A11223344_battery", "unique_id est f'{serial}_{entity_key}'")
        self.assert_equal(entity.translation_key, "battery", "translation_key correspond à entity_key")
        self.assert_equal(entity.entity_description, desc, "entity_description correctement attachée")

        # 2. Délégation client et télémétrie
        self.assert_true(entity.client is client, "entity.client délègue vers coord.client")
        self.assert_equal(entity.telemetry_data.get("battery"), 88, "telemetry_data expose les données du coordinateur")

        # 3. Entité sans description optionnelle
        entity_no_desc = SevenhugsBaseEntity(coord, entry, "active_screen")
        self.assert_true(not hasattr(entity_no_desc, "entity_description"), "entity_description absente si non fournie")
        self.assert_equal(entity_no_desc.unique_id, "SR1A11223344_active_screen", "unique_id correct sans description")

    # =========================================================================
    # Axe 2 : Rattachement au Device Registry (DeviceInfo) et cas limites
    # =========================================================================

    async def test_axis2_device_registry_linkage(self) -> None:
        print("\n=== AXE 2 : Rattachement Device Registry & Cas Limites ===")
        hass = HomeAssistant()

        # Scénario 2.1 : MAC valide présente
        entry_mac = ConfigEntry(
            entry_id="entry_mac",
            domain=DOMAIN,
            title="Remote MAC",
            data={CONF_SERIAL_NUMBER: "SR1A_MAC", CONF_REMOTE_ID: "1234", "mac_address": "00:AA:BB:CC:DD:EE"},
        )
        coord_mac = SevenhugsDataUpdateCoordinator(hass, MockClient(), entry_mac)
        await coord_mac.async_refresh()
        entity_mac = SevenhugsBaseEntity(coord_mac, entry_mac, "test")
        dev_info = entity_mac.device_info
        self.assert_equal(dev_info.identifiers, {(DOMAIN, "SR1A_MAC")}, "DeviceInfo.identifiers contient (DOMAIN, serial)")
        self.assert_true((CONNECTION_NETWORK_MAC, "00:AA:BB:CC:DD:EE") in dev_info.connections, "DeviceInfo.connections contient la liaison MAC")
        self.assert_equal(dev_info.manufacturer, MANUFACTURER, "Fabricant 'Sevenhugs'")
        self.assert_equal(dev_info.model, MODEL_SR1A, "Modèle 'Smart Remote 1A (SR1A)'")
        self.assert_equal(dev_info.name, f"{DEFAULT_NAME} (1234)", "Nom inclut remote_id")
        self.assert_equal(dev_info.hw_version, HARDWARE_VERSION, "hw_version 'FCC ID: 2AEVC-SR1A'")

        # Scénario 2.2 : MAC absente (None)
        entry_no_mac = ConfigEntry(
            entry_id="entry_no_mac",
            domain=DOMAIN,
            title="Remote No MAC",
            data={CONF_SERIAL_NUMBER: "SR1A_NOMAC", CONF_REMOTE_ID: "5678"},
        )
        coord_no_mac = SevenhugsDataUpdateCoordinator(hass, MockClient({"serial_number": "SR1A_NOMAC"}), entry_no_mac)
        await coord_no_mac.async_refresh()
        entity_no_mac = SevenhugsBaseEntity(coord_no_mac, entry_no_mac, "test")
        self.assert_equal(entity_no_mac.device_info.connections, set(), "DeviceInfo.connections est vide quand mac_address est absente")

        # Scénario 2.3 : Serial absent ou vide
        entry_no_serial = ConfigEntry(
            entry_id="fallback_entry_id_999",
            domain=DOMAIN,
            title="Fallback",
            data={},
        )
        # Cas 2.3a : Coordinateur sans attribut serial_number -> repli SevenhugsBaseEntity sur entry.entry_id
        class CoordNoSerialAttr:
            last_update_success = True
            client = MockClient()
            data = None
        entity_pure_fallback = SevenhugsBaseEntity(CoordNoSerialAttr(), entry_no_serial, "test")
        self.assert_equal(
            entity_pure_fallback.unique_id,
            "fallback_entry_id_999_test",
            "SevenhugsBaseEntity.unique_id se replie sur entry.entry_id quand coordinator.serial_number est None",
        )
        self.assert_equal(
            entity_pure_fallback.device_info.identifiers,
            {(DOMAIN, "fallback_entry_id_999")},
            "DeviceInfo.identifiers utilise entry_id en repli quand coordinator.serial_number est None",
        )

        # Cas 2.3b : SevenhugsDataUpdateCoordinator sans serial dans data -> coord.serial_number renvoie 'unknown'
        coord_no_serial = SevenhugsDataUpdateCoordinator(hass, MockClient({}), entry_no_serial)
        entity_coord_unknown = SevenhugsBaseEntity(coord_no_serial, entry_no_serial, "test")
        self.assert_equal(
            entity_coord_unknown.unique_id,
            "unknown_test",
            "SevenhugsDataUpdateCoordinator sans serial produit 'unknown_test' via coord.serial_number",
        )
        self.warnings.append(
            "Discrépance de repli serial_number : SevenhugsDataUpdateCoordinator.serial_number se replie sur 'unknown' "
            "au lieu de entry.entry_id, masquant le repli de secours entry.entry_id codé dans SevenhugsBaseEntity."
        )

        # Scénario 2.4 : remote_id vide ou absent -> repli sur serial_number
        self.assert_equal(
            entity_pure_fallback.device_info.name,
            f"{DEFAULT_NAME} (fallback_entry_id_999)",
            "DeviceInfo.name se replie sur serial_number en l'absence de remote_id",
        )

        # Scénario 2.5 : Accès dictionnaire et get sur DeviceInfo
        self.assert_equal(dev_info["manufacturer"], MANUFACTURER, "DeviceInfo supporte l'accès par index d[key]")
        self.assert_equal(dev_info.get("model"), MODEL_SR1A, "DeviceInfo supporte la méthode get()")
        self.assert_equal(dev_info.get("unknown_key", "default_val"), "default_val", "DeviceInfo.get retourne default si clé inconnue")
        self.assert_true("manufacturer" in dev_info, "Opérateur 'in' supporté sur DeviceInfo")

    # =========================================================================
    # Axe 3 : Suivi dynamique de disponibilité & Machine d'états
    # =========================================================================

    async def test_axis3_dynamic_availability_tracking(self) -> None:
        print("\n=== AXE 3 : Suivi Dynamique de Disponibilité & Résilience Veille ===")
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="avail_entry",
            domain=DOMAIN,
            title="Avail Remote",
            data={CONF_SERIAL_NUMBER: "SR1A_AVAIL", CONF_REMOTE_ID: "1111"},
        )
        client = MockClient()
        coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
        entity = SevenhugsBaseEntity(coord, entry, "battery")
        entity.hass = hass
        entity.entity_id = "sensor.sevenhugs_battery"
        await entity.async_added_to_hass()

        # 1. Nominal initial
        await coord.async_refresh()
        self.assert_true(coord.last_update_success is True, "Coordinateur nominal : last_update_success = True")
        self.assert_true(entity.available is True, "Entité disponible au nominal")
        self.assert_equal(coord.consecutive_failures, 0, "0 échec consécutif")

        # 2. Premier échec réseau (Deep Sleep début)
        client.exception_to_raise = SevenhugsConnectionError("TCP Closed: Sleep")
        await coord.async_refresh()
        self.assert_equal(coord.consecutive_failures, 1, "Échec consécutif = 1")
        self.assert_true(coord.last_update_success is True, "Tolérance Deep Sleep (1/3) : last_update_success reste True")
        self.assert_true(entity.available is True, "Entité reste available à l'échec 1")
        self.assert_equal(coord.battery, 88, "Données en cache préservées à l'échec 1")

        # 3. Deuxième échec réseau (Deep Sleep suite)
        client.exception_to_raise = TimeoutError("Timeout matériel")
        await coord.async_refresh()
        self.assert_equal(coord.consecutive_failures, 2, "Échec consécutif = 2")
        self.assert_true(coord.last_update_success is True, "Tolérance Deep Sleep (2/3) : last_update_success reste True")
        self.assert_true(entity.available is True, "Entité reste available à l'échec 2")

        # 4. Troisième échec réseau (Seuil SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 3 atteint)
        client.exception_to_raise = ConnectionResetError("Remote unreachable")
        await coord.async_refresh()
        self.assert_equal(coord.consecutive_failures, 3, "Échec consécutif = 3")
        self.assert_true(coord.last_update_success is False, "Seuil atteint : last_update_success bascule à False")
        self.assert_true(entity.available is False, "Entité bascule à available = False")
        # Vérification propagation hass.states
        state = hass.states.get("sensor.sevenhugs_battery")
        self.assert_equal(state.state, "unavailable", "hass.states reflète 'unavailable' sur seuil d'échecs")

        # 5. Quatrième échec consécutif (persistance indisponibilité)
        await coord.async_refresh()
        self.assert_equal(coord.consecutive_failures, 4, "Échec consécutif = 4")
        self.assert_true(entity.available is False, "Entité reste unavailable après >3 échecs")

        # 6. Réveil événementiel (async_wake_up)
        client.exception_to_raise = None
        client.response_data["battery"] = 92
        await coord.async_wake_up(reason="mDNS")
        self.assert_equal(coord.consecutive_failures, 0, "async_wake_up réinitialise failures à 0")
        self.assert_true(coord.last_update_success is True, "last_update_success restauré à True")
        self.assert_true(entity.available is True, "Entité redevient available = True")
        self.assert_equal(coord.battery, 92, "Télémétrie mise à jour avec nouvelle valeur 92%")

        # 7. Erreur inattendue hors réseau (ex: ValueError) -> indisponibilité immédiate
        client.exception_to_raise = RuntimeError("Unexpected internal crash")
        await coord.async_refresh()
        self.assert_true(coord.last_update_success is False, "Erreur inattendue entraîne UpdateFailed immédiat")
        self.assert_true(entity.available is False, "Entité indisponible immédiatement sur erreur inattendue")

        # 8. Erreur d'authentification (SevenhugsAuthError) -> ConfigEntryAuthFailed
        client.exception_to_raise = SevenhugsAuthError("Clé révoquée")
        await coord.async_refresh()
        self.assert_true(coord.last_update_success is False, "AuthError entraîne échec coordinator")
        self.assert_true(isinstance(coord.last_exception, ConfigEntryAuthFailed), "last_exception est ConfigEntryAuthFailed")
        self.assert_true(entity.available is False, "Entité indisponible sur ConfigEntryAuthFailed")

        # 9. Démarrage à froid (Cold Start) sans cache préalable
        entry_cold = ConfigEntry(entry_id="cold", domain=DOMAIN, title="Cold", data={})
        client_cold = MockClient()
        client_cold.exception_to_raise = SevenhugsConnectionError("Offline")
        coord_cold = SevenhugsDataUpdateCoordinator(hass, client_cold, entry_cold)
        await coord_cold.async_refresh()
        self.assert_true(coord_cold.last_update_success is False, "Démarrage à froid sans cache échoue immédiatement")
        entity_cold = SevenhugsBaseEntity(coord_cold, entry_cold, "battery")
        self.assert_true(entity_cold.available is False, "Entité cold start indisponible")

    # =========================================================================
    # Axe 4 : Comportement face aux données partielles, nulles et corrompues
    # =========================================================================

    async def test_axis4_partial_corrupted_data(self) -> None:
        print("\n=== AXE 4 : Résilience Données Partielles & Corrompues ===")
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="partial_entry",
            domain=DOMAIN,
            title="Partial Remote",
            data={CONF_SERIAL_NUMBER: "SR1A_PARTIAL", CONF_REMOTE_ID: "5726"},
        )

        # Scénario 4.1 : Réponse vide / non-dictionnaire
        client = MockClient("chaine non dictionnaire")
        coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
        await coord.async_refresh()
        entity = SevenhugsBaseEntity(coord, entry, "battery")

        self.assert_equal(entity.telemetry_data.get("battery"), 100, "Repli batterie par défaut 100% sur réponse corrompue")
        self.assert_equal(coord.wifi_signal, None, "wifi_signal est None sur données vides")
        self.assert_equal(coord.charging, False, "charging est False par défaut")
        self.assert_equal(coord.active_screen, "Home", "active_screen repli 'Home'")
        self.assert_equal(coord.mac_address, None, "mac_address None")
        self.assert_equal(coord.firmware_version, "v0.76.4", "firmware_version repli 'v0.76.4'")

        # Scénario 4.2 : Télémétrie partielle (seul wifi_signal présent en float)
        client.response_data = {"wifi_signal": -72.6}
        await coord.async_refresh()
        self.assert_equal(coord.wifi_signal, -72, "wifi_signal float converti en int (-72)")
        self.assert_equal(coord.battery, 100, "battery maintenue depuis le cache")

        # Scénario 4.3 : Batterie hors bornes (valeur > 100 et < 0)
        client.response_data = {"battery": 150}
        await coord.async_refresh()
        self.assert_equal(coord.battery, 100, "Batterie 150 plafonnée à 100%")

        client.response_data = {"battery": -25}
        await coord.async_refresh()
        self.assert_equal(coord.battery, 0, "Batterie -25 planchée à 0%")

        # Scénario 4.4 : Type incompatible pour batterie
        client.response_data = {"battery": "invalide"}
        await coord.async_refresh()
        self.assert_equal(coord.battery, 0, "Batterie invalide conserve valeur précédente en cache")

        # Scénario 4.5 : Alternative 'rssi' au lieu de 'wifi_signal'
        client.response_data = {"rssi": -63}
        await coord.async_refresh()
        self.assert_equal(coord.wifi_signal, -63, "Champ alternatif 'rssi' détecté et exploité")

        # Scénario 4.6 : Alternative 'docked' au lieu de 'charging'
        client.response_data = {"docked": True}
        await coord.async_refresh()
        self.assert_true(coord.charging is True, "Champ alternatif 'docked' reflété sur charging")

        # Scénario 4.7 : coordinator.data est None
        coord.data = None
        self.assert_equal(entity.telemetry_data, {}, "telemetry_data retourne dict vide quand coord.data est None")

    # =========================================================================
    # Axe 5 : Vérification exhaustive des Shims Home Assistant
    # =========================================================================

    async def test_axis5_mock_shims_verification(self) -> None:
        print("\n=== AXE 5 : Vérification Exhaustive des Shims Mock HA ===")

        # 1. Platform Enum
        self.assert_equal(Platform.SENSOR, "sensor", "Platform.SENSOR == 'sensor'")
        self.assert_equal(Platform.BINARY_SENSOR, "binary_sensor", "Platform.BINARY_SENSOR == 'binary_sensor'")
        self.assert_equal(Platform.BUTTON, "button", "Platform.BUTTON == 'button'")
        self.assert_equal(Platform.SWITCH, "switch", "Platform.SWITCH == 'switch'")
        self.assert_equal(Platform.LIGHT, "light", "Platform.LIGHT == 'light'")
        self.assert_equal(Platform.MEDIA_PLAYER, "media_player", "Platform.MEDIA_PLAYER == 'media_player'")
        self.assert_equal(Platform.SCENE, "scene", "Platform.SCENE == 'scene'")

        # 2. EntityCategory Enum
        self.assert_equal(EntityCategory.CONFIG, "config", "EntityCategory.CONFIG == 'config'")
        self.assert_equal(EntityCategory.DIAGNOSTIC, "diagnostic", "EntityCategory.DIAGNOSTIC == 'diagnostic'")

        # 3. DeviceClasses
        self.assert_equal(SensorDeviceClass.BATTERY, "battery", "SensorDeviceClass.BATTERY == 'battery'")
        self.assert_equal(SensorDeviceClass.SIGNAL_STRENGTH, "signal_strength", "SensorDeviceClass.SIGNAL_STRENGTH == 'signal_strength'")
        self.assert_equal(BinarySensorDeviceClass.BATTERY_CHARGING, "battery_charging", "BinarySensorDeviceClass.BATTERY_CHARGING == 'battery_charging'")
        self.assert_equal(BinarySensorDeviceClass.CONNECTIVITY, "connectivity", "BinarySensorDeviceClass.CONNECTIVITY == 'connectivity'")
        self.assert_equal(ButtonDeviceClass.RESTART, "restart", "ButtonDeviceClass.RESTART == 'restart'")
        self.assert_equal(SensorStateClass.MEASUREMENT, "measurement", "SensorStateClass.MEASUREMENT == 'measurement'")

        # 4. Generic Subscripting sur CoordinatorEntity et DataUpdateCoordinator
        self.assert_true(CoordinatorEntity[Any] is CoordinatorEntity, "CoordinatorEntity[Any] supporté")
        self.assert_true(
            CoordinatorEntity[SevenhugsDataUpdateCoordinator] is CoordinatorEntity,
            "CoordinatorEntity[SevenhugsDataUpdateCoordinator] supporté",
        )
        self.assert_true(DataUpdateCoordinator[dict[str, Any]] is DataUpdateCoordinator, "DataUpdateCoordinator[dict[str, Any]] supporté")

        # 5. Entity lifecycle hooks & async_on_remove
        ent = Entity()
        callbacks_executed = []
        ent.async_on_remove(lambda: callbacks_executed.append("cb1"))
        ent.async_on_remove(lambda: callbacks_executed.append("cb2"))
        await ent.async_will_remove_from_hass()
        self.assert_equal(callbacks_executed, ["cb1", "cb2"], "async_will_remove_from_hass exécute les callbacks async_on_remove")

        # 6. MockStateMachine
        hass = HomeAssistant()
        hass.states.set("sensor.test_shim", "42", {"unit": "%"})
        self.assert_true("sensor.test_shim" in hass.states, "'sensor.test_shim' présent dans hass.states")
        st = hass.states.get("sensor.test_shim")
        self.assert_true(st is not None, "hass.states.get retourne une instance State")
        self.assert_equal(st.state, "42", "State.state == '42'")
        self.assert_equal(st.attributes.get("unit"), "%", "State.attributes préservés")
        self.assert_true(len(hass.states.all()) >= 1, "hass.states.all() liste les états")

    # =========================================================================
    # Axe 6 : Héritage Multiple et Intégration Plates-Formes (Sensor, Binary, Button)
    # =========================================================================

    async def test_axis6_multiple_inheritance_and_platforms(self) -> None:
        print("\n=== AXE 6 : Héritage Multiple & Multi-Plateformes ===")
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="multi_entry",
            domain=DOMAIN,
            title="Multi Remote",
            data={CONF_SERIAL_NUMBER: "SR1A_MULTI", CONF_REMOTE_ID: "8888", "mac_address": "11:22:33:44:55:66"},
        )
        client = MockClient({
            "battery": 77,
            "wifi_signal": -68,
            "charging": True,
            "docked": True,
            "active_screen": "Scenes",
            "serial_number": "SR1A_MULTI",
            "mac_address": "11:22:33:44:55:66",
            "firmware_version": "0.76.4",
        })
        coord = SevenhugsDataUpdateCoordinator(hass, client, entry)
        await coord.async_refresh()

        # --- Définition des classes héritant de SevenhugsBaseEntity et de la plate-forme ---
        class SevenhugsBatterySensor(SevenhugsBaseEntity, SensorEntity):
            _attr_device_class = SensorDeviceClass.BATTERY
            _attr_state_class = SensorStateClass.MEASUREMENT
            _attr_native_unit_of_measurement = PERCENTAGE
            _attr_entity_category = EntityCategory.DIAGNOSTIC

            def __init__(self, coordinator: SevenhugsDataUpdateCoordinator, entry: ConfigEntry) -> None:
                super().__init__(coordinator, entry, "battery")

            @property
            def native_value(self) -> int | None:
                return self.coordinator.battery

        class SevenhugsChargingBinarySensor(SevenhugsBaseEntity, BinarySensorEntity):
            _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
            _attr_entity_category = EntityCategory.DIAGNOSTIC

            def __init__(self, coordinator: SevenhugsDataUpdateCoordinator, entry: ConfigEntry) -> None:
                super().__init__(coordinator, entry, "charging")

            @property
            def is_on(self) -> bool:
                return self.coordinator.charging

        class SevenhugsPingButton(SevenhugsBaseEntity, ButtonEntity):
            _attr_device_class = ButtonDeviceClass.RESTART
            _attr_entity_category = EntityCategory.DIAGNOSTIC

            def __init__(self, coordinator: SevenhugsDataUpdateCoordinator, entry: ConfigEntry) -> None:
                super().__init__(coordinator, entry, "ping")
                self.ping_executed = False

            async def async_press(self) -> None:
                await self.client.async_request("GET", "/api/hello")
                await self.coordinator.async_request_refresh()
                self.ping_executed = True

        # Instanciation et initialisation dans Home Assistant
        battery_sensor = SevenhugsBatterySensor(coord, entry)
        battery_sensor.hass = hass
        battery_sensor.entity_id = "sensor.sevenhugs_battery"
        await battery_sensor.async_added_to_hass()

        charging_sensor = SevenhugsChargingBinarySensor(coord, entry)
        charging_sensor.hass = hass
        charging_sensor.entity_id = "binary_sensor.sevenhugs_charging"
        await charging_sensor.async_added_to_hass()

        ping_button = SevenhugsPingButton(coord, entry)
        ping_button.hass = hass
        ping_button.entity_id = "button.sevenhugs_ping"
        await ping_button.async_added_to_hass()

        # Écriture initiale des états
        battery_sensor.async_write_ha_state()
        charging_sensor.async_write_ha_state()
        ping_button.async_write_ha_state()

        # 1. Vérification MRO et identifiants
        self.assert_equal(battery_sensor.unique_id, "SR1A_MULTI_battery", "unique_id correct sur SensorEntity")
        self.assert_equal(charging_sensor.unique_id, "SR1A_MULTI_charging", "unique_id correct sur BinarySensorEntity")
        self.assert_equal(ping_button.unique_id, "SR1A_MULTI_ping", "unique_id correct sur ButtonEntity")

        # 2. Vérification états initiaux dans hass.states
        st_bat = hass.states.get("sensor.sevenhugs_battery")
        self.assert_equal(st_bat.state, "77", "Sensor state = 77 (valeur native batterie)")
        self.assert_equal(st_bat.attributes.get("unit_of_measurement"), "%", "Unité '%' dans les attributs")

        st_chg = hass.states.get("binary_sensor.sevenhugs_charging")
        self.assert_equal(st_chg.state, "on", "BinarySensor state = 'on' quand charging=True")

        st_btn = hass.states.get("button.sevenhugs_ping")
        self.assert_true(st_btn is not None, "Button enregistré dans hass.states")

        # 3. Mise à jour coordonnateur simultanée pour toutes les entités
        client.response_data["battery"] = 64
        client.response_data["charging"] = False
        client.response_data["docked"] = False
        await coord.async_refresh()

        self.assert_equal(hass.states.get("sensor.sevenhugs_battery").state, "64", "Mise à jour auditeur SensorEntity = 64")
        self.assert_equal(hass.states.get("binary_sensor.sevenhugs_charging").state, "off", "Mise à jour auditeur BinarySensor = off")

        # 4. Action Button async_press
        await ping_button.async_press()
        self.assert_true(ping_button.ping_executed, "Button.async_press exécuté avec succès")
        self.assert_equal(client.requests_log[-2][:2], ("GET", "/api/hello"), "Button a bien émis GET /api/hello")
        self.assert_equal(client.requests_log[-1][:2], ("GET", "/api/settings"), "Button a rafraîchi le coordonnateur via GET /api/settings")

        # 5. Retrait propre d'une entité et isolation des mises à jour
        await battery_sensor.async_will_remove_from_hass()
        client.response_data["battery"] = 25
        client.response_data["charging"] = True
        await coord.async_refresh()
        self.assert_equal(
            hass.states.get("sensor.sevenhugs_battery").state,
            "64",
            "Entité retirée ne reçoit plus les mises à jour (reste figée à 64)",
        )
        # Mais le binary sensor continue de recevoir les mises à jour
        self.assert_equal(
            hass.states.get("binary_sensor.sevenhugs_charging").state,
            "on",
            "Binary sensor actif continue de recevoir les mises à jour",
        )

        # 6. Vérification du comportement Python 3.14 sur Enum str(device_class)
        # Note critique : En Python 3.11+, (str, Enum) renvoie 'SensorDeviceClass.BATTERY' sous str()
        dev_class_str = str(SensorDeviceClass.BATTERY)
        if dev_class_str != "battery":
            self.warnings.append(
                f"Avertissement d'interopérabilité Python 3.11+ : str(SensorDeviceClass.BATTERY) renvoie '{dev_class_str}' au lieu de 'battery'. "
                "L'utilisation de enum.StrEnum ou de la propriété .value est recommandée dans ha_mock.py pour les attributs d'état."
            )

    async def run_all(self) -> bool:
        print("\n============================================================")
        print("LANCEMENT DU HARNAIS CONTRADICTOIRE M1.2 (SevenhugsBaseEntity & Shims)")
        print("============================================================")
        try:
            await self.test_axis1_base_entity_contracts()
            await self.test_axis2_device_registry_linkage()
            await self.test_axis3_dynamic_availability_tracking()
            await self.test_axis4_partial_corrupted_data()
            await self.test_axis5_mock_shims_verification()
            await self.test_axis6_multiple_inheritance_and_platforms()
        except AssertionError as err:
            print(f"\n[ÉCHEC DÉTECTÉ] {err}")
            return False

        print("\n============================================================")
        print(f"RÉSULTATS : {self.passed} tests passés, {self.failed} échecs.")
        if self.warnings:
            print(f"Avertissements / Découvertes empiriques ({len(self.warnings)}) :")
            for w in self.warnings:
                print(f"  - {w}")
        print("============================================================")
        return self.failed == 0


if __name__ == "__main__":
    harness = AdversarialBaseEntityHarness()
    success = asyncio.run(harness.run_all())
    sys.exit(0 if success else 1)
