"""Banc d'essais contradictoires et de stress empirique pour sensor.py et binary_sensor.py.

Auteur: Challenger M2.1 (EMPIRICAL CHALLENGER)
Rôles: critic, specialist
Milestone: Phase 3 — Milestone 2

Ce banc d'essais teste de manière exhaustive, contradictoire et impitoyable :
1. Bornes extrêmes et typage de la télémétrie :
   - Batterie : -10, -5, 0, 50, 100, 150, 9999, floats, strings, None, NaN.
   - Wi-Fi signal (RSSI) : None, -100, -30, -150, 0, 5 (positif), "invalid", payload alternatif rssi vs wifi_signal.
   - Écran actif : chaînes standard, chaîne vide "", chaîne géante (10 000 caractères), Unicode / emojis, conversion de type.
2. Matrice de vérité Charging & Docked (4 combinaisons + types hétérogènes) :
   - (charging=True, docked=True) -> is_on=True, docked=True
   - (charging=False, docked=True) -> is_on=True, docked=True (docking implique statut charge/dock)
   - (charging=True, docked=False) -> is_on=True, docked=False
   - (charging=False, docked=False) -> is_on=False, docked=False
   - Types non booléens (int, None, dict vide).
3. Machine à états du cycle de veille et Invariant de disponibilité de la connectivité :
   - Échecs 1 et 2 (Veille tolérée) : maintien absolu du cache et disponibilité de toutes les entités (available=True).
   - Échec 3 (Perte de liaison) : bascule de battery, wifi_signal, screen, charging vers unavailable (available=False).
   - INVARIANT CRITIQUE : connectivity.available reste STRICTEMENT True et transitionne vers is_on=False (state='off').
   - Échecs 4-5 : persistance de l'état déconnecté sans crash.
   - Réveil événementiel instantané (async_wake_up / async_handle_wake_up) : restauration immédiate à 100%.
4. Concurrence extrême, anti-race conditions et dynamique des écouteurs Home Assistant :
   - Souscription et désouscription propre d'entités (async_added_to_hass / async_will_remove_from_hass).
   - Vérification de la purge des callbacks dans le coordinateur.
   - Tempête de 30 rafraîchissements concurrents sans désynchronisation.
   - Écriture d'état conforme dans hass.states (MockStateMachine).
5. Architecture d'amorçage (async_setup_entry) et conformité Quality Scale :
   - Résolution polymorphique du coordinateur (6 variantes d'encapsulation).
   - Robustesse face au coordinateur manquant (0 crash, 0 entité orpheline).
   - Vérification de PARALLEL_UPDATES == 0, _attr_has_entity_name == True, diagnostic category.
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
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.smart_remote_rebirth.binary_sensor import (
    PARALLEL_UPDATES as BINARY_SENSOR_PARALLEL_UPDATES,
    SevenhugsChargingBinarySensor,
    SevenhugsConnectivityBinarySensor,
    _get_coordinator as _get_binary_coordinator,
    async_setup_entry as async_setup_binary_entry,
)
from custom_components.smart_remote_rebirth.client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from custom_components.smart_remote_rebirth.const import (
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DEFAULT_NAME,
    DOMAIN,
    HARDWARE_VERSION,
    MANUFACTURER,
    MODEL_SR1A,
)
from custom_components.smart_remote_rebirth.coordinator import (
    DEFAULT_COORDINATOR_UPDATE_INTERVAL,
    SLEEP_CONSECUTIVE_FAILURES_THRESHOLD,
    SevenhugsDataUpdateCoordinator,
)
from custom_components.smart_remote_rebirth.sensor import (
    PARALLEL_UPDATES as SENSOR_PARALLEL_UPDATES,
    SevenhugsActiveScreenSensor,
    SevenhugsBatterySensor,
    SevenhugsWifiSignalSensor,
    _get_coordinator as _get_sensor_coordinator,
    async_setup_entry as async_setup_sensor_entry,
)


class MockAdversarialClient:
    """Client simulé permettant l'injection dynamique d'erreurs et de charges utiles adverses."""

    def __init__(self, initial_data: dict[str, Any] | None = None) -> None:
        self.response_data: dict[str, Any] = initial_data or {
            "battery": 85,
            "wifi_signal": -60,
            "charging": False,
            "docked": False,
            "active_screen": "Home",
            "serial_number": "SR1A_ADV_999",
            "mac_address": "AA:BB:CC:DD:EE:99",
            "firmware_version": "0.76.4",
            "name": "Smart Remote 999",
        }
        self.exception_to_raise: Exception | None = None
        self.request_count: int = 0

    async def async_request(self, method: str, path: str, json_data: dict | None = None) -> dict[str, Any]:
        self.request_count += 1
        if self.exception_to_raise is not None:
            raise self.exception_to_raise
        return dict(self.response_data)


def create_mock_config_entry(
    entry_id: str = "entry_adv_test",
    serial_number: str = "SR1A_ADV_999",
    remote_id: str = "999",
) -> ConfigEntry:
    """Crée une instance ConfigEntry factice pour les tests."""
    return ConfigEntry(
        entry_id=entry_id,
        domain=DOMAIN,
        title=f"Sevenhugs Smart Remote ({remote_id})",
        data={
            CONF_SERIAL_NUMBER: serial_number,
            CONF_REMOTE_ID: remote_id,
            "firmware_version": "0.76.4",
            "mac_address": "AA:BB:CC:DD:EE:99",
        },
        options={},
    )


class TestAdversarialSensorPlatforms(unittest.IsolatedAsyncioTestCase):
    """Banc de tests contradictoires et de stress empirique pour sensor et binary_sensor."""

    async def asyncSetUp(self) -> None:
        """Initialise l'environnement de test avant chaque cas."""
        self.hass = HomeAssistant()
        self.entry = create_mock_config_entry()
        self.client = MockAdversarialClient()
        self.coordinator = SevenhugsDataUpdateCoordinator(self.hass, self.client, self.entry)
        self.entry.runtime_data = self.coordinator

    # =========================================================================
    # 1. TÉLÉMÉTRIE : BORNES EXTRÊMES, CLAMPING & ROBUSTESSE DES TYPES
    # =========================================================================

    async def test_battery_boundary_values_and_clamping(self) -> None:
        """Vérifie le bornage strict (0-100) et la tolérance aux types invalides de la batterie."""
        battery_sensor = SevenhugsBatterySensor(self.coordinator, self.entry)
        battery_sensor.hass = self.hass
        battery_sensor.entity_id = "sensor.sevenhugs_battery"

        # Vérification des métadonnées de contrat HA
        self.assertEqual(battery_sensor.device_class, SensorDeviceClass.BATTERY)
        self.assertEqual(battery_sensor.native_unit_of_measurement, PERCENTAGE)
        self.assertEqual(battery_sensor.state_class, SensorStateClass.MEASUREMENT)
        self.assertEqual(battery_sensor.suggested_display_precision, 0)
        self.assertEqual(battery_sensor.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(battery_sensor.unique_id, "SR1A_ADV_999_battery")

        test_cases = [
            (-100, 0, "Sous-charge extrême négative (-100 -> 0%)"),
            (-5, 0, "Sous-charge négative (-5 -> 0%)"),
            (0, 0, "Borne minimale exacte (0 -> 0%)"),
            (1, 1, "Valeur basse nominale (1 -> 1%)"),
            (50, 50, "Valeur médiane nominale (50 -> 50%)"),
            (99, 99, "Valeur haute nominale (99 -> 99%)"),
            (100, 100, "Borne maximale exacte (100 -> 100%)"),
            (105, 100, "Surcharge modérée (105 -> 100%)"),
            (150, 100, "Surcharge élevée (150 -> 100%)"),
            (99999, 100, "Surcharge extrême (99999 -> 100%)"),
            ("42", 42, "Chaîne numérique valide ('42' -> 42%)"),
            ("-15", 0, "Chaîne négative ('-15' -> 0%)"),
            ("120", 100, "Chaîne avec dépassement ('120' -> 100%)"),
        ]

        for raw_val, expected, label in test_cases:
            with self.subTest(label=label, raw_val=raw_val):
                self.client.response_data["battery"] = raw_val
                await self.coordinator.async_refresh()
                self.assertEqual(
                    battery_sensor.native_value,
                    expected,
                    f"Échec {label}: attendu {expected}, obtenu {battery_sensor.native_value}",
                )

        # Cas adverses non-numériques : repli sur le cache précédent
        self.client.response_data["battery"] = 77
        await self.coordinator.async_refresh()
        self.assertEqual(battery_sensor.native_value, 77)

        invalid_inputs = ["non_numeric", "NaN", None, [80], {"bat": 50}]
        for invalid_val in invalid_inputs:
            with self.subTest(invalid_val=invalid_val):
                self.client.response_data["battery"] = invalid_val
                await self.coordinator.async_refresh()
                # Doit conserver le cache valide ou une valeur de repli sans lever d'exception
                self.assertIsNotNone(battery_sensor.native_value)
                self.assertTrue(0 <= battery_sensor.native_value <= 100)

    async def test_wifi_signal_boundary_and_invalid_values(self) -> None:
        """Vérifie le traitement du RSSI Wi-Fi (dBm), valeurs négatives, positives, None et invalid."""
        wifi_sensor = SevenhugsWifiSignalSensor(self.coordinator, self.entry)
        wifi_sensor.hass = self.hass
        wifi_sensor.entity_id = "sensor.sevenhugs_wifi_signal"

        self.assertEqual(wifi_sensor.device_class, SensorDeviceClass.SIGNAL_STRENGTH)
        self.assertEqual(wifi_sensor.native_unit_of_measurement, SIGNAL_STRENGTH_DECIBELS_MILLIWATT)
        self.assertEqual(wifi_sensor.state_class, SensorStateClass.MEASUREMENT)
        self.assertEqual(wifi_sensor.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(wifi_sensor.unique_id, "SR1A_ADV_999_wifi_signal")

        test_cases = [
            (-100, -100, "Signal minimal limite (-100 dBm)"),
            (-75, -75, "Signal moyen nominal (-75 dBm)"),
            (-30, -30, "Signal excellent nominal (-30 dBm)"),
            (-150, -150, "Signal extrêmement faible (-150 dBm)"),
            (0, 0, "Signal nul (0 dBm)"),
            (5, 5, "Signal positif inhabituel (5 dBm)"),
            ("-62", -62, "Chaîne numérique valide ('-62' -> -62 dBm)"),
            (None, None, "Signal absent / déconnecté (None)"),
        ]

        for raw_val, expected, label in test_cases:
            with self.subTest(label=label, raw_val=raw_val):
                self.client.response_data["wifi_signal"] = raw_val
                # Nettoie la clé rssi alternative pour tester strictement wifi_signal
                self.client.response_data.pop("rssi", None)
                await self.coordinator.async_refresh()
                self.assertEqual(
                    wifi_sensor.native_value,
                    expected,
                    f"Échec {label}: attendu {expected}, obtenu {wifi_sensor.native_value}",
                )

        # Clé alternative 'rssi' quand 'wifi_signal' est absente ou None
        self.client.response_data.pop("wifi_signal", None)
        self.client.response_data["rssi"] = -68
        await self.coordinator.async_refresh()
        self.assertEqual(wifi_sensor.native_value, -68)

        # Priorité de 'wifi_signal' sur 'rssi'
        self.client.response_data["wifi_signal"] = -52
        self.client.response_data["rssi"] = -88
        await self.coordinator.async_refresh()
        self.assertEqual(wifi_sensor.native_value, -52)

        # Cas chaînes invalides ou corrompues
        for corrupt in ["invalid_signal", "---", [1, 2], {}]:
            with self.subTest(corrupt=corrupt):
                self.client.response_data["wifi_signal"] = corrupt
                await self.coordinator.async_refresh()
                self.assertIsNone(wifi_sensor.native_value)

    async def test_active_screen_adversarial_values(self) -> None:
        """Vérifie le capteur d'écran actif face aux chaînes vides, Unicode géant et caractères spéciaux."""
        screen_sensor = SevenhugsActiveScreenSensor(self.coordinator, self.entry)
        screen_sensor.hass = self.hass
        screen_sensor.entity_id = "sensor.sevenhugs_active_screen"

        self.assertIsNone(screen_sensor.device_class)
        self.assertEqual(screen_sensor.icon, "mdi:cellphone")
        self.assertEqual(screen_sensor.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(screen_sensor.unique_id, "SR1A_ADV_999_active_screen")

        # 1. Écrans nominaux standard
        standard_screens = ["Home", "media_player", "devices", "scenes", "settings"]
        for screen in standard_screens:
            self.client.response_data["active_screen"] = screen
            await self.coordinator.async_refresh()
            self.assertEqual(screen_sensor.native_value, screen)

        # 2. Chaîne Unicode et émojis complexes
        unicode_screen = "Salon 🛋️ / Lumières 💡 / 日本語 / 🚀"
        self.client.response_data["active_screen"] = unicode_screen
        await self.coordinator.async_refresh()
        self.assertEqual(screen_sensor.native_value, unicode_screen)

        # 3. Chaîne géante (stress mémoire / débordement)
        huge_screen = "A" * 10000
        self.client.response_data["active_screen"] = huge_screen
        await self.coordinator.async_refresh()
        self.assertEqual(screen_sensor.native_value, huge_screen)

        # 4. Repli sur clé alternative 'current_screen'
        self.client.response_data.pop("active_screen", None)
        self.client.response_data["current_screen"] = "Radio_FM"
        await self.coordinator.async_refresh()
        self.assertEqual(screen_sensor.native_value, "Radio_FM")

        # 5. Conversion de types non-chaînes en str
        self.client.response_data["active_screen"] = 12345
        await self.coordinator.async_refresh()
        self.assertEqual(screen_sensor.native_value, "12345")

    # =========================================================================
    # 2. MATRICE DE VÉRITÉ CHARGING & DOCKED
    # =========================================================================

    async def test_charging_docked_full_truth_matrix(self) -> None:
        """Vérifie les 4 combinaisons de la table de vérité charging/docked et attributs."""
        charging_sensor = SevenhugsChargingBinarySensor(self.coordinator, self.entry)
        charging_sensor.hass = self.hass
        charging_sensor.entity_id = "binary_sensor.sevenhugs_charging"

        self.assertEqual(charging_sensor.device_class, BinarySensorDeviceClass.BATTERY_CHARGING)
        self.assertEqual(charging_sensor.entity_category, EntityCategory.DIAGNOSTIC)
        self.assertEqual(charging_sensor.unique_id, "SR1A_ADV_999_charging")

        truth_table = [
            (True, True, True, True, "En charge ET posé sur le socle"),
            (False, True, True, True, "Sur le socle sans cycle de charge actif"),
            (True, False, True, False, "En charge filaire hors socle"),
            (False, False, False, False, "Déchargé et autonome en main"),
        ]

        for raw_charging, raw_docked, exp_is_on, exp_docked_attr, desc in truth_table:
            with self.subTest(desc=desc):
                self.client.response_data["charging"] = raw_charging
                self.client.response_data["docked"] = raw_docked
                await self.coordinator.async_refresh()

                self.assertEqual(
                    charging_sensor.is_on,
                    exp_is_on,
                    f"Échec is_on pour '{desc}': attendu {exp_is_on}, obtenu {charging_sensor.is_on}",
                )
                attrs = charging_sensor.extra_state_attributes
                self.assertIn("docked", attrs)
                self.assertEqual(
                    attrs["docked"],
                    exp_docked_attr,
                    f"Échec attribut 'docked' pour '{desc}': attendu {exp_docked_attr}, obtenu {attrs['docked']}",
                )

        # Entrées polymorphes non-booléennes (int, strings, None)
        fuzzy_cases = [
            (1, 0, True, False),
            (0, 1, True, True),
            ("true", "false", True, True),  # non-empty string is truthy
            (None, None, False, False),
        ]
        for c, d, exp_on, exp_dock in fuzzy_cases:
            with self.subTest(c=c, d=d):
                self.client.response_data["charging"] = c
                self.client.response_data["docked"] = d
                await self.coordinator.async_refresh()
                self.assertEqual(charging_sensor.is_on, exp_on)
                self.assertEqual(charging_sensor.extra_state_attributes["docked"], exp_dock)

    # =========================================================================
    # 3. MACHINE D'ÉTATS DU CYCLE DE VEILLE ET INVARIANT DE CONNECTIVITÉ
    # =========================================================================

    async def test_deep_sleep_resilience_and_connectivity_invariant(self) -> None:
        """Vérifie l'invariant fondamental :
        Échec 1-2 : Veille tolérée -> cache préservé, toutes entités available=True.
        Échec 3 : Injoignable -> capteurs de télémétrie unavailable (available=False),
                  MAIS le capteur de connectivité reste disponible (available=True)
                  et bascule proprement à is_on=False (state='off').
        Réveil : Restauration instantanée sur événement.
        """
        battery = SevenhugsBatterySensor(self.coordinator, self.entry)
        wifi = SevenhugsWifiSignalSensor(self.coordinator, self.entry)
        screen = SevenhugsActiveScreenSensor(self.coordinator, self.entry)
        charging = SevenhugsChargingBinarySensor(self.coordinator, self.entry)
        connectivity = SevenhugsConnectivityBinarySensor(self.coordinator, self.entry)

        entities = [battery, wifi, screen, charging, connectivity]
        for ent in entities:
            ent.hass = self.hass
            await ent.async_added_to_hass()

        battery.entity_id = "sensor.sevenhugs_battery"
        wifi.entity_id = "sensor.sevenhugs_wifi_signal"
        screen.entity_id = "sensor.sevenhugs_active_screen"
        charging.entity_id = "binary_sensor.sevenhugs_charging"
        connectivity.entity_id = "binary_sensor.sevenhugs_connectivity"

        # --- ÉTAPE 0 : État nominal initial ---
        self.client.response_data = {
            "battery": 82,
            "wifi_signal": -58,
            "charging": True,
            "docked": True,
            "active_screen": "Home",
            "serial_number": "SR1A_ADV_999",
            "mac_address": "AA:BB:CC:DD:EE:99",
            "firmware_version": "0.76.4",
            "name": "Smart Remote 999",
        }
        await self.coordinator.async_refresh()

        self.assertTrue(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.consecutive_failures, 0)
        self.assertTrue(self.coordinator.is_connected)

        # Toutes les entités sont disponibles
        for ent in entities:
            self.assertTrue(ent.available, f"{ent.__class__.__name__} doit être disponible")

        self.assertEqual(battery.native_value, 82)
        self.assertEqual(wifi.native_value, -58)
        self.assertEqual(screen.native_value, "Home")
        self.assertTrue(charging.is_on)
        self.assertTrue(connectivity.is_on)
        self.assertEqual(connectivity.extra_state_attributes["consecutive_failures"], 0)

        # Vérification des écritures dans le state machine HA
        self.assertEqual(self.hass.states.get("sensor.sevenhugs_battery").state, "82")
        self.assertEqual(self.hass.states.get("sensor.sevenhugs_wifi_signal").state, "-58")
        self.assertEqual(self.hass.states.get("sensor.sevenhugs_active_screen").state, "Home")
        self.assertEqual(self.hass.states.get("binary_sensor.sevenhugs_charging").state, "on")
        self.assertEqual(self.hass.states.get("binary_sensor.sevenhugs_connectivity").state, "on")

        # --- ÉTAPE 1 : Échec 1 (Veille profonde tolérée) ---
        self.client.exception_to_raise = SevenhugsConnectionError("Télécommande en veille (socket down)")
        await self.coordinator.async_refresh()

        self.assertTrue(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.consecutive_failures, 1)
        self.assertTrue(self.coordinator.is_connected)

        # Maintien absolu du cache et disponibilité
        for ent in entities:
            self.assertTrue(ent.available, f"{ent.__class__.__name__} doit rester disponible à l'échec 1")

        self.assertEqual(battery.native_value, 82)
        self.assertEqual(wifi.native_value, -58)
        self.assertEqual(screen.native_value, "Home")
        self.assertTrue(charging.is_on)
        self.assertTrue(connectivity.is_on)
        self.assertEqual(connectivity.extra_state_attributes["consecutive_failures"], 1)

        # --- ÉTAPE 2 : Échec 2 (Veille prolongée tolérée) ---
        self.client.exception_to_raise = asyncio.TimeoutError()
        await self.coordinator.async_refresh()

        self.assertTrue(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.consecutive_failures, 2)
        self.assertTrue(self.coordinator.is_connected)

        for ent in entities:
            self.assertTrue(ent.available, f"{ent.__class__.__name__} doit rester disponible à l'échec 2")

        self.assertEqual(connectivity.extra_state_attributes["consecutive_failures"], 2)

        # --- ÉTAPE 3 : Échec 3 (SEUIL ATTEINT -> Perte de connectivité confirmée) ---
        self.client.exception_to_raise = ConnectionRefusedError("Host down")
        await self.coordinator.async_refresh()

        self.assertFalse(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.consecutive_failures, 3)
        self.assertFalse(self.coordinator.is_connected)

        # VÉRIFICATION DE L'INVARIANT :
        # Les capteurs de télémétrie sont indisponibles (unavailable)
        self.assertFalse(battery.available, "Battery doit être unavailable à l'échec 3")
        self.assertFalse(wifi.available, "Wifi doit être unavailable à l'échec 3")
        self.assertFalse(screen.available, "Screen doit être unavailable à l'échec 3")
        self.assertFalse(charging.available, "Charging doit être unavailable à l'échec 3")

        # LE CAPTEUR DE CONNECTIVITÉ DOIT RESTER DISPONIBLE !
        self.assertTrue(connectivity.available, "Connectivity DOIT rester available=True pour rapporter 'off' !")
        self.assertFalse(connectivity.is_on, "Connectivity is_on doit être False !")
        self.assertEqual(connectivity.extra_state_attributes["consecutive_failures"], 3)

        # Vérification dans hass.states
        self.assertEqual(self.hass.states.get("sensor.sevenhugs_battery").state, "unavailable")
        self.assertEqual(self.hass.states.get("sensor.sevenhugs_wifi_signal").state, "unavailable")
        self.assertEqual(self.hass.states.get("sensor.sevenhugs_active_screen").state, "unavailable")
        self.assertEqual(self.hass.states.get("binary_sensor.sevenhugs_charging").state, "unavailable")

        # Crucial : le binaire de connectivité est 'off', pas 'unavailable'
        self.assertEqual(self.hass.states.get("binary_sensor.sevenhugs_connectivity").state, "off")

        # --- ÉTAPE 4 : Échecs 4 et 5 (Persistance de la panne) ---
        for fail_count in [4, 5]:
            await self.coordinator.async_refresh()
            self.assertFalse(self.coordinator.last_update_success)
            self.assertEqual(self.coordinator.consecutive_failures, fail_count)
            self.assertFalse(battery.available)
            self.assertTrue(connectivity.available)
            self.assertFalse(connectivity.is_on)
            self.assertEqual(connectivity.extra_state_attributes["consecutive_failures"], fail_count)
            self.assertEqual(self.hass.states.get("binary_sensor.sevenhugs_connectivity").state, "off")

        # --- ÉTAPE 5 : Réveil événementiel et rétablissement instantané ---
        self.client.exception_to_raise = None
        self.client.response_data = {
            "battery": 95,
            "wifi_signal": -45,
            "charging": False,
            "docked": False,
            "active_screen": "Spotify",
            "serial_number": "SR1A_ADV_999",
            "mac_address": "AA:BB:CC:DD:EE:99",
            "firmware_version": "0.76.4",
            "name": "Smart Remote 999",
        }

        # Déclenchement par réveil événementiel
        await self.coordinator.async_wake_up(reason="mdns_wake_event")

        self.assertTrue(self.coordinator.last_update_success)
        self.assertEqual(self.coordinator.consecutive_failures, 0)
        self.assertTrue(self.coordinator.is_connected)

        # Toutes les entités sont redevenues immédiatement disponibles
        for ent in entities:
            self.assertTrue(ent.available, f"{ent.__class__.__name__} doit être rétabli disponible après réveil")

        self.assertEqual(battery.native_value, 95)
        self.assertEqual(wifi.native_value, -45)
        self.assertEqual(screen.native_value, "Spotify")
        self.assertFalse(charging.is_on)
        self.assertTrue(connectivity.is_on)
        self.assertEqual(connectivity.extra_state_attributes["consecutive_failures"], 0)

        self.assertEqual(self.hass.states.get("sensor.sevenhugs_battery").state, "95")
        self.assertEqual(self.hass.states.get("sensor.sevenhugs_wifi_signal").state, "-45")
        self.assertEqual(self.hass.states.get("sensor.sevenhugs_active_screen").state, "Spotify")
        self.assertEqual(self.hass.states.get("binary_sensor.sevenhugs_charging").state, "off")
        self.assertEqual(self.hass.states.get("binary_sensor.sevenhugs_connectivity").state, "on")

    # =========================================================================
    # 4. CONCURRENCE, ÉCOUTEURS & INTÉGRITÉ DU CYCLE DE VIE
    # =========================================================================

    async def test_entity_listener_lifecycle_and_cleanup(self) -> None:
        """Vérifie l'enregistrement des écouteurs et leur désouscription propre lors du retrait."""
        initial_listeners_count = len(self.coordinator._listeners)

        sensor = SevenhugsBatterySensor(self.coordinator, self.entry)
        sensor.hass = self.hass
        sensor.entity_id = "sensor.test_listener"

        await sensor.async_added_to_hass()
        self.assertEqual(len(self.coordinator._listeners), initial_listeners_count + 1)

        # Rafraîchissement : mise à jour écrite
        self.client.response_data["battery"] = 64
        await self.coordinator.async_refresh()
        self.assertEqual(self.hass.states.get("sensor.test_listener").state, "64")

        # Retrait de l'entité
        await sensor.async_will_remove_from_hass()
        self.assertEqual(len(self.coordinator._listeners), initial_listeners_count)

        # Un nouveau rafraîchissement ne doit plus impacter l'état
        self.client.response_data["battery"] = 12
        await self.coordinator.async_refresh()
        # L'état dans la machine à état reste figé à 64 car l'entité n'écoute plus
        self.assertEqual(self.hass.states.get("sensor.test_listener").state, "64")

    async def test_concurrent_refresh_stress_storm(self) -> None:
        """Soumet le coordinateur et les entités à 30 rafraîchissements concurrents asynchrones."""
        battery = SevenhugsBatterySensor(self.coordinator, self.entry)
        connectivity = SevenhugsConnectivityBinarySensor(self.coordinator, self.entry)

        battery.hass = self.hass
        connectivity.hass = self.hass
        battery.entity_id = "sensor.stress_bat"
        connectivity.entity_id = "binary_sensor.stress_conn"

        await battery.async_added_to_hass()
        await connectivity.async_added_to_hass()

        async def worker(index: int) -> None:
            # Varie les données à chaque worker
            self.client.response_data["battery"] = 50 + (index % 40)
            await self.coordinator.async_request_refresh()

        tasks = [asyncio.create_task(worker(i)) for i in range(30)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            self.assertFalse(isinstance(res, Exception), f"Exception inattendue sous concurrence: {res}")

        # Intégrité finale vérifiée
        self.assertTrue(self.coordinator.last_update_success)
        self.assertTrue(50 <= battery.native_value <= 90)
        self.assertTrue(connectivity.is_on)

    # =========================================================================
    # 5. INITIALISATION POLYMORPHE & ROBUSTESSE SETUP_ENTRY
    # =========================================================================

    async def test_async_setup_entry_polymorphic_coordinator_storage(self) -> None:
        """Vérifie que setup_entry trouve le coordinateur dans les 6 formats de stockage supportés."""
        added_entities_sensor: list[Any] = []
        added_entities_binary: list[Any] = []

        def add_sensor_cb(ents: list[Any]) -> None:
            added_entities_sensor.extend(ents)

        def add_binary_cb(ents: list[Any]) -> None:
            added_entities_binary.extend(ents)

        # Variantes de stockage :
        storage_variations = [
            ("runtime_data direct", self.coordinator, None),
            (
                "runtime_data object with .coordinator",
                type("SevenhugsDataObj", (), {"coordinator": self.coordinator})(),
                None,
            ),
            ("runtime_data dict", {"coordinator": self.coordinator}, None),
            ("hass.data direct", None, self.coordinator),
            (
                "hass.data object with .coordinator",
                None,
                type("SevenhugsDataObj", (), {"coordinator": self.coordinator})(),
            ),
            ("hass.data dict", None, {"coordinator": self.coordinator}),
        ]

        for desc, rd, hd in storage_variations:
            with self.subTest(desc=desc):
                added_entities_sensor.clear()
                added_entities_binary.clear()

                entry = create_mock_config_entry(entry_id=f"entry_{desc.replace(' ', '_')}")
                entry.runtime_data = rd

                self.hass.data[DOMAIN] = {}
                if hd is not None:
                    self.hass.data[DOMAIN][entry.entry_id] = hd

                await async_setup_sensor_entry(self.hass, entry, add_sensor_cb)
                await async_setup_binary_entry(self.hass, entry, add_binary_cb)

                self.assertEqual(len(added_entities_sensor), 3, f"Doit enregistrer 3 capteurs pour {desc}")
                self.assertEqual(len(added_entities_binary), 2, f"Doit enregistrer 2 binaires pour {desc}")

                types_sensor = {type(e) for e in added_entities_sensor}
                self.assertIn(SevenhugsBatterySensor, types_sensor)
                self.assertIn(SevenhugsWifiSignalSensor, types_sensor)
                self.assertIn(SevenhugsActiveScreenSensor, types_sensor)

                types_binary = {type(e) for e in added_entities_binary}
                self.assertIn(SevenhugsChargingBinarySensor, types_binary)
                self.assertIn(SevenhugsConnectivityBinarySensor, types_binary)

    async def test_async_setup_entry_missing_coordinator_resilience(self) -> None:
        """Vérifie qu'en cas d'absence complète de coordinateur, setup_entry ne crash pas."""
        entry = create_mock_config_entry(entry_id="entry_empty")
        entry.runtime_data = None
        self.hass.data.clear()

        added_entities: list[Any] = []

        def add_cb(ents: list[Any]) -> None:
            added_entities.extend(ents)

        # Ne doit pas lever d'exception et ne doit rien enregistrer
        await async_setup_sensor_entry(self.hass, entry, add_cb)
        await async_setup_binary_entry(self.hass, entry, add_cb)

        self.assertEqual(len(added_entities), 0)

    def test_quality_scale_parallel_updates_and_naming_rules(self) -> None:
        """Vérifie la conformité avec les règles d'architecture et standards Home Assistant."""
        self.assertEqual(
            SENSOR_PARALLEL_UPDATES,
            0,
            "sensor.py PARALLEL_UPDATES doit être égal à 0 (push coordinator)",
        )
        self.assertEqual(
            BINARY_SENSOR_PARALLEL_UPDATES,
            0,
            "binary_sensor.py PARALLEL_UPDATES doit être égal à 0 (push coordinator)",
        )

        battery = SevenhugsBatterySensor(self.coordinator, self.entry)
        connectivity = SevenhugsConnectivityBinarySensor(self.coordinator, self.entry)

        # Règle Bronze has_entity_name = True
        self.assertTrue(battery.has_entity_name)
        self.assertTrue(connectivity.has_entity_name)

        # Polling désactivé au niveau de l'entité (piloté par coordinateur)
        self.assertFalse(battery.should_poll)
        self.assertFalse(connectivity.should_poll)

        # Device registry DeviceInfo
        dev_info = battery.device_info
        self.assertIsNotNone(dev_info)
        self.assertIn((DOMAIN, "SR1A_ADV_999"), dev_info.identifiers)
        self.assertEqual(dev_info.manufacturer, MANUFACTURER)
        self.assertEqual(dev_info.model, MODEL_SR1A)
        self.assertEqual(dev_info.hw_version, HARDWARE_VERSION)
        self.assertEqual(dev_info.sw_version, "0.76.4")


if __name__ == "__main__":
    print("\n" + "=" * 78)
    print("  EXÉCUTION DU BANC D'ESSAIS CONTRADICTOIRES M2 (SENSOR & BINARY SENSOR)")
    print("=" * 78 + "\n")
    unittest.main(verbosity=2)
