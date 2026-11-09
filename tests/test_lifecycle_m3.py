"""Tests unitaires et d'intégration pour Milestone 3 (Cycle de vie, coordinateur et localisation).

Valide :
1. Initialisation de AsyncSmartRemoteClient et SevenhugsDataUpdateCoordinator dans async_setup_entry.
2. Premier rafraîchissement d'état initial (first_refresh) et stockage dual dans SevenhugsData.
3. Forwarding de la configuration aux plateformes déclarées dans PLATFORMS.
4. Nettoyage ordonné et déchargement des plateformes dans async_unload_entry avec idempotence.
5. Gestion des échecs de premier rafraîchissement avec nettoyage immédiat sans fuite de descripteur.
6. Constantes nominales (polling 60s, tolérance veille 3 échecs).
7. Parité stricte 1:1 et intégrité des fichiers de localisation (strings.json, en.json, fr.json).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, patch

# Assure le chargement autonome depuis la racine
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.ha_mock
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

import tests.conftest
from tests.conftest import MockSmartRemoteServer
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
from custom_components.smart_remote_rebirth.const import (
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_HOST,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_READ_KEY,
    CONF_SESSION_WRITE_KEY,
    DEFAULT_POLLING_INTERVAL,
    SLEEP_CONSECUTIVE_FAILURES_THRESHOLD,
)


class TestMilestone3LifecycleAndLocalization(unittest.IsolatedAsyncioTestCase):
    """Suite de tests pour le cycle de vie Milestone 3 et la localisation."""

    async def asyncSetUp(self) -> None:
        """Initialise un environnement de test isolé."""
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer()
        await self.server.start()

        self.valid_entry = ConfigEntry(
            entry_id="m3_test_entry_001",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: self.server.host,
                CONF_PORT: self.server.port,
                CONF_SERIAL_NUMBER: "a06fe048b48b260139",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-m3-test",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: self.server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )

    async def asyncTearDown(self) -> None:
        """Nettoie les serveurs et ressources."""
        await self.server.stop()

    async def test_nominal_constants(self) -> None:
        """Vérifie la conformité des constantes de polling et de résilience veille."""
        self.assertEqual(DEFAULT_POLLING_INTERVAL, 60)
        self.assertEqual(SLEEP_CONSECUTIVE_FAILURES_THRESHOLD, 3)
        self.assertEqual(len(PLATFORMS), 3)
        self.assertIn(Platform.SENSOR, PLATFORMS)
        self.assertIn(Platform.BINARY_SENSOR, PLATFORMS)
        self.assertIn(Platform.BUTTON, PLATFORMS)

    async def test_sevenhugs_data_bidirectional_and_proxy(self) -> None:
        """Valide le double stockage, la synchronisation bidirectionnelle et le proxy de SevenhugsData."""
        mock_client = AsyncMock(spec=AsyncSmartRemoteClient)
        mock_client.host = "192.168.1.50:80"
        mock_session = AsyncMock()
        mock_coordinator = AsyncMock()

        data = SevenhugsData(client=mock_client, session=mock_session, coordinator=mock_coordinator)

        # 1. Accès attribut et clé
        self.assertIs(data.client, mock_client)
        self.assertIs(data["client"], mock_client)
        self.assertIs(data.session, mock_session)
        self.assertIs(data["session"], mock_session)
        self.assertIs(data.coordinator, mock_coordinator)
        self.assertIs(data["coordinator"], mock_coordinator)

        # 2. Délégation proxy vers client
        self.assertEqual(data.host, "192.168.1.50:80")

        # 3. Rejet strict des attributs inconnus
        with self.assertRaises(AttributeError):
            _ = data.unknown_attribute_xyz

        # 4. Synchronisation mutation dictionnaire -> attribut
        new_coord = AsyncMock()
        data["coordinator"] = new_coord
        self.assertIs(data.coordinator, new_coord)

        # 5. Synchronisation mutation attribut -> dictionnaire
        another_coord = AsyncMock()
        data.coordinator = another_coord
        self.assertIs(data["coordinator"], another_coord)

    async def test_async_setup_entry_nominal_lifecycle(self) -> None:
        """Valide l'initialisation complète : coordinateur, first_refresh, stockage et forwarding."""
        setup_ok = await async_setup_entry(self.hass, self.valid_entry)
        self.assertTrue(setup_ok)

        # 1. Vérification stockage dans hass.data et entry.runtime_data
        self.assertIn(self.valid_entry.entry_id, self.hass.data[DOMAIN])
        runtime_data = self.hass.data[DOMAIN][self.valid_entry.entry_id]
        self.assertIs(self.valid_entry.runtime_data, runtime_data)
        self.assertIsInstance(runtime_data, SevenhugsData)

        # 2. Vérification coordinateur initialisé et premier rafraîchissement exécuté
        coord = runtime_data.coordinator
        self.assertIsInstance(coord, SevenhugsDataUpdateCoordinator)
        self.assertTrue(coord.last_update_success)
        self.assertIsNotNone(coord.data)
        self.assertEqual(coord.data["battery"], 95)
        self.assertEqual(coord.data["serial_number"], "a06fe048b48b260139")

        # 3. Vérification forwarding vers PLATFORMS
        self.assertIn(self.valid_entry.entry_id, self.hass.config_entries.forwarded_platforms)
        self.assertEqual(
            self.hass.config_entries.forwarded_platforms[self.valid_entry.entry_id],
            PLATFORMS,
        )

        # 4. Déchargement nominal
        unload_ok = await async_unload_entry(self.hass, self.valid_entry)
        self.assertTrue(unload_ok)
        self.assertNotIn(self.valid_entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(self.valid_entry.runtime_data)
        self.assertNotIn(DOMAIN, self.hass.data)
        self.assertIn(self.valid_entry.entry_id, self.hass.config_entries.unloaded_platforms)
        self.assertEqual(
            self.hass.config_entries.unloaded_platforms[self.valid_entry.entry_id],
            PLATFORMS,
        )

        # 5. Idempotence du déchargement
        self.assertTrue(await async_unload_entry(self.hass, self.valid_entry))

    async def test_async_setup_entry_first_refresh_failure_cleans_up(self) -> None:
        """Vérifie que si first_refresh lève une exception, les ressources sont nettoyées sans fuite."""
        bad_entry = ConfigEntry(
            entry_id="m3_bad_entry_fail",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (Dead Host)",
            data={
                CONF_HOST: "127.0.0.1",
                CONF_PORT: 65431,  # Port inaccessible
                CONF_SERIAL_NUMBER: "a06fe048b48b999999",
                CONF_REMOTE_ID: "9999",
                CONF_CONTROLLER_ID: "ctrl-uuid-bad",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: "03" * 32,
                CONF_SESSION_WRITE_KEY: "04" * 32,
                CONF_SESSION_READ_KEY: "05" * 32,
            },
        )

        with patch.object(
            SevenhugsDataUpdateCoordinator,
            "async_config_entry_first_refresh",
            side_effect=RuntimeError("Simulated network dead on startup"),
        ):
            with self.assertRaises(RuntimeError):
                await async_setup_entry(self.hass, bad_entry)

        # Validation du nettoyage immédiat
        self.assertNotIn(bad_entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(bad_entry.runtime_data)

    async def test_async_setup_entry_forward_entry_setups_failure_rolls_back(self) -> None:
        """Vérifie qu'un échec lors de async_forward_entry_setups déclenche un rollback complet."""
        entry = self.valid_entry
        with patch.object(
            self.hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("Platform sensor failed to initialize"),
        ):
            with self.assertRaises(RuntimeError):
                await async_setup_entry(self.hass, entry)

        # Vérification du rollback complet
        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(entry.runtime_data)

    async def test_async_setup_entry_first_refresh_unsuccessful_raises_not_ready(self) -> None:
        """Vérifie que last_update_success=False lève ConfigEntryNotReady et nettoie les ressources."""
        entry = self.valid_entry
        async def fake_first_refresh(coord_self: Any) -> None:
            coord_self.last_update_success = False
            coord_self.last_exception = Exception("Temporary backend glitch")

        with patch.object(SevenhugsDataUpdateCoordinator, "async_config_entry_first_refresh", autospec=True, side_effect=fake_first_refresh):
            with self.assertRaises(ConfigEntryNotReady):
                await async_setup_entry(self.hass, entry)

        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(entry.runtime_data)

    async def test_async_setup_entry_first_refresh_auth_failed_raises_auth_failed(self) -> None:
        """Vérifie qu'une erreur d'authentification propage ConfigEntryAuthFailed et nettoie."""
        entry = self.valid_entry
        async def fake_first_refresh_auth(coord_self: Any) -> None:
            coord_self.last_update_success = False
            coord_self.last_exception = ConfigEntryAuthFailed("Invalid remote credentials")

        with patch.object(SevenhugsDataUpdateCoordinator, "async_config_entry_first_refresh", autospec=True, side_effect=fake_first_refresh_auth):
            with self.assertRaises(ConfigEntryAuthFailed):
                await async_setup_entry(self.hass, entry)

        self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
        self.assertIsNone(entry.runtime_data)

    def test_localization_schema_and_parity(self) -> None:
        """Valide la stricte parité 1:1, les 8 clés d'entités et les placeholders des fichiers de traduction."""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        strings_path = os.path.join(base_dir, "custom_components", "smart_remote_rebirth", "strings.json")
        en_path = os.path.join(base_dir, "custom_components", "smart_remote_rebirth", "translations", "en.json")
        fr_path = os.path.join(base_dir, "custom_components", "smart_remote_rebirth", "translations", "fr.json")

        with open(strings_path, encoding="utf-8") as f:
            strings = json.load(f)
        with open(en_path, encoding="utf-8") as f:
            en = json.load(f)
        with open(fr_path, encoding="utf-8") as f:
            fr = json.load(f)

        # 1. strings.json et en.json doivent être strictement identiques
        self.assertEqual(strings, en, "strings.json et translations/en.json doivent être identiques")

        # 2. Récupération des clés feuilles
        def get_leaf_keys(d: dict, prefix: str = "") -> dict[str, str]:
            leaves = {}
            for k, v in d.items():
                p = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    leaves.update(get_leaf_keys(v, p))
                else:
                    leaves[p] = str(v)
            return leaves

        s_leaves = get_leaf_keys(strings)
        fr_leaves = get_leaf_keys(fr)

        # 3. Parité stricte 1:1 des clés feuilles (40 clés en Phase 3, 42-47 en Phase 4)
        self.assertEqual(
            set(s_leaves.keys()),
            set(fr_leaves.keys()),
            f"Différence de structure détectée: {set(s_leaves.keys()) ^ set(fr_leaves.keys())}",
        )
        self.assertIn(len(s_leaves), (40, 42, 43, 47))

        # 4. Présence de l'intégralité des 8 entités créées en Milestone 2
        expected_entities = [
            "entity.sensor.battery.name",
            "entity.sensor.wifi_signal.name",
            "entity.sensor.active_screen.name",
            "entity.binary_sensor.charging.name",
            "entity.binary_sensor.connectivity.name",
            "entity.button.force_sync.name",
            "entity.button.ping.name",
            "entity.button.backup_config.name",
        ]
        for key in expected_entities:
            self.assertIn(key, s_leaves)
            self.assertIn(key, fr_leaves)

        # 5. Cohérence des valeurs anglaises
        self.assertEqual(s_leaves["entity.sensor.battery.name"], "Battery")
        self.assertEqual(s_leaves["entity.sensor.wifi_signal.name"], "Wi-Fi signal")
        self.assertEqual(s_leaves["entity.sensor.active_screen.name"], "Active screen")
        self.assertEqual(s_leaves["entity.binary_sensor.charging.name"], "Charging")
        self.assertEqual(s_leaves["entity.binary_sensor.connectivity.name"], "Connectivity")
        self.assertEqual(s_leaves["entity.button.force_sync.name"], "Force sync")
        self.assertEqual(s_leaves["entity.button.ping.name"], "Ping")
        self.assertEqual(s_leaves["entity.button.backup_config.name"], "Backup configuration")

        # 6. Cohérence des valeurs françaises
        self.assertEqual(fr_leaves["entity.sensor.battery.name"], "Batterie")
        self.assertEqual(fr_leaves["entity.sensor.wifi_signal.name"], "Signal Wi-Fi")
        self.assertEqual(fr_leaves["entity.sensor.active_screen.name"], "Écran actif")
        self.assertEqual(fr_leaves["entity.binary_sensor.charging.name"], "En charge")
        self.assertEqual(fr_leaves["entity.binary_sensor.connectivity.name"], "Connectivité")
        self.assertEqual(fr_leaves["entity.button.force_sync.name"], "Forcer la synchronisation")
        self.assertEqual(fr_leaves["entity.button.ping.name"], "Ping")
        self.assertEqual(fr_leaves["entity.button.backup_config.name"], "Sauvegarder la configuration")

        # 7. Préservation des tokens dynamiques ({sn}, {host})
        token_regex = re.compile(r"\{([a-zA-Z0-9_]+)\}")
        for key in s_leaves:
            s_tokens = set(token_regex.findall(s_leaves[key]))
            fr_tokens = set(token_regex.findall(fr_leaves[key]))
            self.assertEqual(
                s_tokens,
                fr_tokens,
                f"Mismatch de tokens à la clé {key}: EN={s_tokens} vs FR={fr_tokens}",
            )


if __name__ == "__main__":
    unittest.main()
