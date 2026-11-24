"""Tests unitaires de la négociation dynamique X-Version / X-Min-Version (Issue #17 / API v21)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.conftest  # noqa: F401
from tests.conftest import MockSmartRemoteServer

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.smart_remote_rebirth.client import AsyncSmartRemoteClient
from custom_components.smart_remote_rebirth.config_flow import SmartRemoteConfigFlow
from custom_components.smart_remote_rebirth.const import (
    CONF_API_VERSION,
    CONF_HOST,
    CONF_MIN_API_VERSION,
    CONF_PORT,
)


class TestDynamicApiVersionNegotiation(unittest.IsolatedAsyncioTestCase):
    """Vérifie l'adaptation automatique aux télécommandes sur ancien firmware (ex. API v21 / min 17)."""

    async def asyncSetUp(self) -> None:
        self.server = MockSmartRemoteServer(pair_setup_delay=0.01)
        self.server.api_version = "21"
        self.server.min_api_version = "17"
        self.server.enforce_api_version = True
        self.server.hello_not_found = True
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_client_pair_setup_and_verify_auto_negotiates_v21_on_403(self) -> None:
        """Un client initialisé par défaut en 31/24 doit basculer automatiquement en 21/17 sur HTTP 403."""
        session = aiohttp.ClientSession()
        try:
            client = AsyncSmartRemoteClient(
                host=f"{self.server.host}:{self.server.port}",
                session=session,
                keys={},
            )
            self.assertEqual(client.api_version, "31")
            self.assertEqual(client.min_api_version, "24")

            # Pair-Setup M1 reçoit 403 avec X-Version: 21 / X-Min-Version: 17 puis réessaie automatiquement
            salt, b_bytes = await client.async_pair_setup_m1()
            self.assertEqual(len(salt), 16)
            self.assertEqual(len(b_bytes), 384)
            self.assertEqual(client.api_version, "21")
            self.assertEqual(client.min_api_version, "17")

            # Pair-Setup M3-M6 + Pair-Verify + GET /api/settings réussissent avec 21/17
            remote_id, _ = await client.async_pair_setup_finish("123-45-678", salt, b_bytes)
            self.assertEqual(remote_id, "5726")

            verified = await client.async_pair_verify()
            self.assertTrue(verified)

            settings = await client.async_request("GET", "/api/settings")
            self.assertIn("carousel_order", settings)
        finally:
            await session.close()

    async def test_client_pair_verify_auto_negotiates_v21_from_cold_start(self) -> None:
        """Un client déjà appairé sans api_version en cache (31/24 par défaut) négocie 21/17 dès Pair-Verify."""
        session = aiohttp.ClientSession()
        try:
            # 1. Appairage initial pour obtenir des clés valides
            setup_client = AsyncSmartRemoteClient(
                host=f"{self.server.host}:{self.server.port}",
                session=session,
                keys={},
            )
            salt, b_bytes = await setup_client.async_pair_setup_m1()
            await setup_client.async_pair_setup_finish("123-45-678", salt, b_bytes)
            saved_keys = dict(setup_client._keys)
            # Simule une ancienne ConfigEntry ne stockant pas encore api_version / min_api_version
            saved_keys.pop(CONF_API_VERSION, None)
            saved_keys.pop(CONF_MIN_API_VERSION, None)

            # 2. Nouveau client démarrant à froid en 31/24
            cold_client = AsyncSmartRemoteClient(
                host=f"{self.server.host}:{self.server.port}",
                session=session,
                keys=saved_keys,
            )
            self.assertEqual(cold_client.api_version, "31")
            self.assertEqual(cold_client.min_api_version, "24")

            settings = await cold_client.async_request("GET", "/api/settings")
            self.assertIn("carousel_order", settings)
            self.assertEqual(cold_client.api_version, "21")
            self.assertEqual(cold_client.min_api_version, "17")
        finally:
            await session.close()

    async def test_config_flow_detects_v21_during_probe_and_persists_entry_data(self) -> None:
        """Le ConfigFlow détecte X-Version: 21 / X-Min-Version: 17 sur /api/hello (404) et les persiste."""
        hass = HomeAssistant()
        flow = SmartRemoteConfigFlow()
        flow.hass = hass

        res_user = await flow.async_step_user({CONF_HOST: self.server.host, CONF_PORT: self.server.port})
        self.assertEqual(res_user["type"], FlowResultType.FORM)
        self.assertEqual(res_user["step_id"], "pair_init")
        self.assertEqual(flow.api_version, "21")
        self.assertEqual(flow.min_api_version, "17")

        res_init = await flow.async_step_pair_init({})
        self.assertEqual(res_init["type"], FlowResultType.FORM)
        self.assertEqual(res_init["step_id"], "pair_pin")

        res_pin = await flow.async_step_pair_pin({"pin": "123-45-678"})
        self.assertEqual(res_pin["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(res_pin["data"][CONF_API_VERSION], "21")
        self.assertEqual(res_pin["data"][CONF_MIN_API_VERSION], "17")


if __name__ == "__main__":
    unittest.main()
