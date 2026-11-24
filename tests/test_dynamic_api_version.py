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


class TestFactoryResetDiagnosticTool(unittest.TestCase):
    """Vérifie les helpers du script tools/diagnose_factory_reset.py (payloads APK & parser MQTT 3.1.1)."""

    def test_apk_config_payload_builders(self) -> None:
        from tools.diagnose_factory_reset import (
            APK_FALLBACK_CONFIG_KEY_HEX,
            build_apk_create_config_payload,
            build_apk_restore_config_payload,
        )

        create_payload = build_apk_create_config_payload(home_name="Lab")
        self.assertEqual(create_payload["metadata"]["name"], "Lab")
        self.assertEqual(create_payload["smartphone"]["public_key"], "NO-PUBLIC-KEY")
        self.assertIn("first_name", create_payload["user"])
        self.assertEqual(create_payload["settings"]["time_convention"], "24-hour")

        restore_payload = build_apk_restore_config_payload()
        self.assertEqual(len(bytes.fromhex(restore_payload["config_key"])), 64)

        dev_restore = build_apk_restore_config_payload(config_key_hex=APK_FALLBACK_CONFIG_KEY_HEX)
        self.assertEqual(len(bytes.fromhex(dev_restore["config_key"])), 64)

    def test_mqtt_packet_parser_connect_subscribe_publish(self) -> None:
        from tools.diagnose_factory_reset import parse_mqtt_packet

        # CONNECT ("MQTT", level=4, flags=0x02, keepalive=60, client_id="sr-5726")
        connect_payload = b"\x00\x04MQTT\x04\x02\x00\x3c\x00\x07sr-5726"
        summary, resp = parse_mqtt_packet(1, 0, connect_payload)
        self.assertIn("CONNECT", summary)
        self.assertIn("sr-5726", summary)
        self.assertEqual(resp, b"\x20\x02\x00\x00")  # CONNACK OK

        # SUBSCRIBE (packet_id=1, topic="config/5726", qos=1)
        sub_payload = b"\x00\x01\x00\x0bconfig/5726\x01"
        summary, resp = parse_mqtt_packet(8, 2, sub_payload)
        self.assertIn("config/5726", summary)
        self.assertEqual(resp, b"\x90\x03\x00\x01\x00")  # SUBACK

        # PUBLISH QoS 1 (topic="config/create", packet_id=7, body='{"ok":true}')
        pub_payload = b"\x00\x0dconfig/create\x00\x07{\"ok\":true}"
        summary, resp = parse_mqtt_packet(3, 0x02, pub_payload)
        self.assertIn("config/create", summary)
        self.assertIn('{"ok":true}', summary)
        self.assertEqual(resp, b"\x40\x02\x00\x07")  # PUBACK


if __name__ == "__main__":
    unittest.main()
