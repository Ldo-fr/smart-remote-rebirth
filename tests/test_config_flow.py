"""Suite de tests unitaires et asynchrones pour le Config Flow de Sevenhugs Smart Remote (Phase 2).

Couvre l'intégralité des 23 scénarios :
1. test_zeroconf_discovery_success
2. test_zeroconf_bytes_properties
3. test_zeroconf_ip_update_already_configured
4. test_user_step_initial_form
5. test_user_step_valid_host
6. test_user_step_cannot_connect
7. test_pair_init_latency_non_blocking
8. test_pair_init_remote_busy_500
9. test_pair_init_timeout_connect
10. test_pair_pin_valid_formatted
11. test_pair_pin_valid_unformatted
12. test_pair_pin_invalid_format_short
13. test_pair_pin_invalid_format_letters
14. test_pair_pin_auth_failed_bad_pin
15. test_pair_pin_pairing_failed_network
16. test_device_registry_entry_created
17. test_already_configured_manual_entry
18. test_reconfigure_flow_success
19. test_reconfigure_flow_cannot_connect
20. test_reconfigure_flow_mismatched_device
21. test_reauth_flow_success
22. test_options_flow_init_and_save
23. test_lifecycle_setup_and_unload
24. test_options_flow_form_rendering_with_selectors
25. test_options_flow_save_with_entities_and_trigger_resync
26. test_options_flow_backward_compatibility_legacy_keys
27. test_webhook_lifecycle_in_setup_and_unload
28. test_background_sync_scheduling_on_setup_and_options

Compatible duale : pytest (avec pytest-asyncio) et python3 -m unittest discover tests -v.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any
from unittest.mock import patch

# Assure le chargement de conftest et des shims Home Assistant
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.conftest
from tests.conftest import MockSmartRemoteServer

import pytest
from homeassistant.components.zeroconf import ZeroconfServiceInfo
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.helpers import device_registry as dr, selector

from custom_components.sevenhugs_smart_remote import async_setup_entry, async_unload_entry
from custom_components.sevenhugs_smart_remote.config_flow import (
    SevenhugsConfigFlow,
    SevenhugsOptionsFlowHandler,
)
from custom_components.sevenhugs_smart_remote.client import SevenhugsConnectionError
from custom_components.sevenhugs_smart_remote.const import (
    CONF_AUTO_SYNC_WEBHOOKS,
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_ENTITIES,
    CONF_HOST,
    CONF_POLLING_INTERVAL,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_READ_KEY,
    CONF_SESSION_WRITE_KEY,
    CONF_TIMEOUT,
    CONF_TRIGGER_RESYNC,
    DEFAULT_AUTO_SYNC_WEBHOOKS,
    DEFAULT_ENTITIES,
    DEFAULT_EXPOSED_DOMAINS,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DEFAULT_TRIGGER_RESYNC,
    DOMAIN,
    ERROR_ALREADY_CONFIGURED,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_PIN,
    ERROR_PAIRING_FAILED,
    ERROR_REMOTE_BUSY,
    ERROR_TIMEOUT_CONNECT,
    HARDWARE_VERSION,
    MODEL_SR1A,
    MANUFACTURER,
)


@pytest.mark.asyncio
async def test_zeroconf_discovery_success() -> None:
    """Scénario 1 : Découverte automatique Zeroconf nominale avec confirmation."""
    hass = HomeAssistant()
    flow = SevenhugsConfigFlow()
    flow.hass = hass

    discovery = ZeroconfServiceInfo(
        host="127.0.0.1",
        port=80,
        hostname="sr-5726.local.",
        type="_smart_remote._tcp.local.",
        name="Smart Remote 5726",
        properties={"id": "5726", "sn": "a06fe048b48b", "fw": "0.76.4", "sf": "1"},
    )

    result = await flow.async_step_zeroconf(discovery)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"]["sn"] == "a06fe048b48b"
    assert result["description_placeholders"]["host"] == "127.0.0.1"

    confirm_result = await flow.async_step_zeroconf_confirm({})
    assert confirm_result["type"] == FlowResultType.FORM
    assert confirm_result["step_id"] == "pair_init"


@pytest.mark.asyncio
async def test_zeroconf_bytes_properties() -> None:
    """Scénario 2 : Découverte Zeroconf avec propriétés TXT encodées en bytes."""
    hass = HomeAssistant()
    flow = SevenhugsConfigFlow()
    flow.hass = hass

    discovery = ZeroconfServiceInfo(
        host="192.168.1.100",
        port=80,
        hostname="sr-bytes.local.",
        type="_smart_remote._tcp.local.",
        name="Smart Remote Bytes",
        properties={b"id": b"5726", b"sn": b"sn_in_bytes_123", b"fw": b"0.76.4", b"sf": b"1"},
    )

    result = await flow.async_step_zeroconf(discovery)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert flow.serial_number == "sn_in_bytes_123"
    assert flow.remote_id == "5726"


@pytest.mark.asyncio
async def test_zeroconf_ip_update_already_configured() -> None:
    """Scénario 3 : Zeroconf détecte une télécommande déjà configurée et met à jour son IP."""
    hass = HomeAssistant()
    entry = ConfigEntry(
        entry_id="existing_7hugs_entry",
        domain=DOMAIN,
        title="Sevenhugs Smart Remote (5726)",
        data={CONF_HOST: "192.168.1.50", CONF_PORT: 80, CONF_SERIAL_NUMBER: "a06fe048b48b"},
        unique_id="a06fe048b48b",
    )
    hass.config_entries.async_add(entry)

    flow = SevenhugsConfigFlow()
    flow.hass = hass

    discovery = ZeroconfServiceInfo(
        host="192.168.1.60",
        port=80,
        hostname="sr-5726.local.",
        type="_smart_remote._tcp.local.",
        name="Smart Remote 5726",
        properties={"id": "5726", "sn": "a06fe048b48b", "fw": "0.76.4"},
    )

    # Règle discovery-update-connection : interruption avec already_configured et mise à jour de l'IP
    with pytest.raises(AbortFlow) as exc_info:
        await flow.async_step_zeroconf(discovery)

    assert exc_info.value.reason == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.1.60"


@pytest.mark.asyncio
async def test_user_step_initial_form() -> None:
    """Scénario 4 : Formulaire initial de saisie manuelle de l'hôte."""
    hass = HomeAssistant()
    flow = SevenhugsConfigFlow()
    flow.hass = hass

    result = await flow.async_step_user(None)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert CONF_HOST in result["data_schema"].schema


@pytest.mark.asyncio
async def test_user_step_valid_host() -> None:
    """Scénario 5 : Saisie d'un hôte joignable sur le port HTTP 80."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass

        result = await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "pair_init"
        assert flow.host == server.host
        assert flow.port == server.port
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_user_step_cannot_connect() -> None:
    """Scénario 6 : Saisie d'un hôte injoignable retournant cannot_connect."""
    hass = HomeAssistant()
    flow = SevenhugsConfigFlow()
    flow.hass = hass

    # Adresse IP non routable TEST-NET
    result = await flow.async_step_user({CONF_HOST: "192.0.2.1", CONF_PORT: 80})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": ERROR_CANNOT_CONNECT}


@pytest.mark.asyncio
async def test_pair_init_latency_non_blocking() -> None:
    """Scénario 7 : Latence matérielle M1 non-bloquante pour la boucle d'événements."""
    server = MockSmartRemoteServer(pair_setup_delay=0.15)
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass
        flow.host = server.host
        flow.port = server.port

        ticks = 0
        async def ticker() -> None:
            nonlocal ticks
            try:
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1
            except asyncio.CancelledError:
                pass

        tick_task = asyncio.create_task(ticker())
        result = await flow.async_step_pair_init({})
        tick_task.cancel()
        await asyncio.sleep(0)

        assert ticks >= 5, f"La boucle a été bloquée pendant pair_init (ticks={ticks})"
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "pair_pin"
        assert len(flow.salt) == 16
        assert len(flow.b_bytes) == 384
        if flow._pair_client:
            await flow._pair_client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pair_init_remote_busy_500() -> None:
    """Scénario 8 : Gestion de l'erreur remote_busy lors d'un code HTTP 500 sur M1."""
    server = MockSmartRemoteServer()
    server.fail_pair_setup_m1 = "500"
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass
        flow.host = server.host
        flow.port = server.port

        result = await flow.async_step_pair_init({})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "pair_init"
        assert result["errors"] == {"base": ERROR_REMOTE_BUSY}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pair_init_timeout_connect() -> None:
    """Scénario 9 : Gestion de timeout_connect lors de l'expiration du délai de calcul M1."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass
        flow.host = server.host
        flow.port = server.port

        # Simuler une exception de timeout sur pair_setup_m1
        with patch.object(
            SevenhugsConfigFlow,
            "async_step_pair_init",
            wraps=flow.async_step_pair_init,
        ):
            with patch(
                "custom_components.sevenhugs_smart_remote.client.AsyncSmartRemoteClient.async_pair_setup_m1",
                side_effect=SevenhugsConnectionError("Timeout connecting to remote after 60s"),
            ):
                result = await flow.async_step_pair_init({})
                assert result["type"] == FlowResultType.FORM
                assert result["step_id"] == "pair_init"
                assert result["errors"] == {"base": ERROR_TIMEOUT_CONNECT}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pair_pin_valid_formatted() -> None:
    """Scénario 10 : Saisie réussie d'un code PIN avec tirets (format standard XXX-XX-XXX)."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass

        await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
        await flow.async_step_pair_init({})

        result = await flow.async_step_pair_pin({"pin": "123-45-678"})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == "Sevenhugs Smart Remote (5726)"
        assert result["data"][CONF_SERIAL_NUMBER] == "a06fe048b48b260139"
        assert result["data"][CONF_REMOTE_ID] == "5726"
        assert CONF_SESSION_WRITE_KEY in result["data"]
        assert CONF_SESSION_READ_KEY in result["data"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pair_pin_valid_unformatted() -> None:
    """Scénario 11 : Saisie réussie d'un code PIN sans tirets (8 chiffres bruts)."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass

        await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
        await flow.async_step_pair_init({})

        result = await flow.async_step_pair_pin({"pin": "12345678"})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["title"] == "Sevenhugs Smart Remote (5726)"
        assert result["data"][CONF_SERIAL_NUMBER] == "a06fe048b48b260139"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pair_pin_invalid_format_short() -> None:
    """Scénario 12 : Rejet immédiat d'un code PIN trop court sans appel réseau."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass

        await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
        await flow.async_step_pair_init({})

        result = await flow.async_step_pair_pin({"pin": "12345"})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "pair_pin"
        assert result["errors"] == {"base": ERROR_INVALID_PIN}
        if flow._pair_client:
            await flow._pair_client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pair_pin_invalid_format_letters() -> None:
    """Scénario 13 : Rejet immédiat d'un code PIN contenant des caractères alphabétiques."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass

        await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
        await flow.async_step_pair_init({})

        result = await flow.async_step_pair_pin({"pin": "ABC-DE-FGH"})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "pair_pin"
        assert result["errors"] == {"base": ERROR_INVALID_PIN}
        if flow._pair_client:
            await flow._pair_client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pair_pin_auth_failed_bad_pin() -> None:
    """Scénario 14 : Rejet d'authentification SRP en cas de mauvais code PIN."""
    server = MockSmartRemoteServer()
    server.fail_srp_m3 = True
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass

        await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
        await flow.async_step_pair_init({})

        result = await flow.async_step_pair_pin({"pin": "999-99-999"})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "pair_pin"
        assert result["errors"] == {"base": ERROR_INVALID_PIN}
        if flow._pair_client:
            await flow._pair_client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_pair_pin_pairing_failed_network() -> None:
    """Scénario 15 : Déconnexion réseau en cours d'échange M5/M6."""
    server = MockSmartRemoteServer()
    server.fail_m5_disconnect = True
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass

        await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
        await flow.async_step_pair_init({})

        result = await flow.async_step_pair_pin({"pin": "123-45-678"})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "pair_pin"
        assert result["errors"] == {"base": ERROR_PAIRING_FAILED}
        if flow._pair_client:
            await flow._pair_client.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_device_registry_entry_created() -> None:
    """Scénario 16 : Inscription complète dans le Device Registry avec fabricant et modèle."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        flow = SevenhugsConfigFlow()
        flow.hass = hass

        await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
        await flow.async_step_pair_init({})
        await flow.async_step_pair_pin({"pin": "123-45-678"})

        device = dr.async_get(hass).async_get_device({(DOMAIN, "a06fe048b48b260139")})
        assert device is not None
        assert device.manufacturer == MANUFACTURER
        assert device.model == MODEL_SR1A
        assert device.hw_version == HARDWARE_VERSION
        assert (dr.CONNECTION_NETWORK_MAC, "38:D7:CA:00:16:5E") in device.connections
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_already_configured_manual_entry() -> None:
    """Scénario 17 : Rejet d'un appareil déjà existant lors d'un appairage manuel."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        existing_entry = ConfigEntry(
            entry_id="existing_7hugs",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={CONF_HOST: server.host, CONF_PORT: server.port, CONF_SERIAL_NUMBER: "a06fe048b48b260139"},
            unique_id="a06fe048b48b260139",
        )
        hass.config_entries.async_add(existing_entry)

        flow = SevenhugsConfigFlow()
        flow.hass = hass

        await flow.async_step_user({CONF_HOST: server.host, CONF_PORT: server.port})
        await flow.async_step_pair_init({})

        with pytest.raises(AbortFlow) as exc_info:
            await flow.async_step_pair_pin({"pin": "123-45-678"})

        assert exc_info.value.reason == "already_configured"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_reconfigure_flow_success() -> None:
    """Scénario 18 : Reconfiguration de l'hôte avec succès sans ré-appairage cryptographique."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="entry_to_reconfig",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.99",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "a06fe048b48b260139",
                CONF_REMOTE_ID: "5726",
            },
            unique_id="a06fe048b48b260139",
        )
        hass.config_entries.async_add(entry)

        flow = SevenhugsConfigFlow()
        flow.hass = hass
        flow.context = {"entry_id": "entry_to_reconfig"}

        form = await flow.async_step_reconfigure(None)
        assert form["type"] == FlowResultType.FORM
        assert form["step_id"] == "reconfigure"

        result = await flow.async_step_reconfigure({CONF_HOST: server.host, CONF_PORT: server.port})
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_HOST] == server.host
        assert entry.data[CONF_PORT] == server.port
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_reconfigure_flow_cannot_connect() -> None:
    """Scénario 19 : Échec de reconfiguration en cas d'hôte injoignable."""
    hass = HomeAssistant()
    entry = ConfigEntry(
        entry_id="entry_reconfig_fail",
        domain=DOMAIN,
        title="Sevenhugs Smart Remote (5726)",
        data={CONF_HOST: "192.168.1.99", CONF_PORT: 80, CONF_SERIAL_NUMBER: "a06fe048b48b260139"},
    )
    hass.config_entries.async_add(entry)

    flow = SevenhugsConfigFlow()
    flow.hass = hass
    flow.context = {"entry_id": "entry_reconfig_fail"}

    result = await flow.async_step_reconfigure({CONF_HOST: "192.0.2.1", CONF_PORT: 80})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": ERROR_CANNOT_CONNECT}


@pytest.mark.asyncio
async def test_reconfigure_flow_mismatched_device() -> None:
    """Scénario 20 : Rejet de reconfiguration si l'appareil a un numéro de série différent."""
    server = MockSmartRemoteServer()
    server.serial_number = "DIFFERENT_SERIAL_999"
    await server.start()
    try:
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="entry_reconfig_mismatch",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={
                CONF_HOST: "192.168.1.99",
                CONF_PORT: 80,
                CONF_SERIAL_NUMBER: "a06fe048b48b260139",
                CONF_REMOTE_ID: "5726",
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
            },
        )
        hass.config_entries.async_add(entry)

        flow = SevenhugsConfigFlow()
        flow.hass = hass
        flow.context = {"entry_id": "entry_reconfig_mismatch"}

        # Patch _async_verify_device_match pour retourner False (différent numéro de série)
        with patch.object(SevenhugsConfigFlow, "_async_verify_device_match", return_value=False):
            result = await flow.async_step_reconfigure({CONF_HOST: server.host, CONF_PORT: server.port})
            assert result["type"] == FlowResultType.FORM
            assert result["step_id"] == "reconfigure"
            assert result["errors"] == {"base": ERROR_CANNOT_CONNECT}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_reauth_flow_success() -> None:
    """Scénario 21 : Réauthentification avec mise à jour des clés sur ConfigEntry existante."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="entry_reauth_success",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: "a06fe048b48b260139",
                CONF_REMOTE_ID: "5726",
                CONF_CLIENT_PRIVATE_KEY: "ancient_expired_private_key",
            },
            unique_id="a06fe048b48b260139",
        )
        hass.config_entries.async_add(entry)

        flow = SevenhugsConfigFlow()
        flow.hass = hass
        flow.context = {"entry_id": "entry_reauth_success"}

        r1 = await flow.async_step_reauth(entry.data)
        assert r1["type"] == FlowResultType.FORM
        assert r1["step_id"] == "reauth_confirm"

        r2 = await flow.async_step_reauth_confirm({})
        assert r2["type"] == FlowResultType.FORM
        assert r2["step_id"] == "pair_pin"

        r3 = await flow.async_step_pair_pin({"pin": "123-45-678"})
        assert r3["type"] == FlowResultType.ABORT
        assert r3["reason"] == "reauth_successful"
        # Les clés doivent avoir été mises à jour sans recréer d'entrée orpheline
        assert entry.data[CONF_CLIENT_PRIVATE_KEY] != "ancient_expired_private_key"
        assert entry.entry_id == "entry_reauth_success"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_options_flow_init_and_save() -> None:
    """Scénario 22 : Modification et sauvegarde des options avancées."""
    entry = ConfigEntry(
        entry_id="opt_entry_1",
        domain=DOMAIN,
        title="Sevenhugs Smart Remote (5726)",
        data={},
        options={
            CONF_POLLING_INTERVAL: 30,
            CONF_AUTO_SYNC_WEBHOOKS: True,
            CONF_TIMEOUT: 10,
        },
    )
    handler = SevenhugsOptionsFlowHandler(entry)

    form = await handler.async_step_init(None)
    assert form["type"] == FlowResultType.FORM
    assert form["step_id"] == "init"

    result = await handler.async_step_init({
        CONF_POLLING_INTERVAL: 45,
        CONF_AUTO_SYNC_WEBHOOKS: False,
        CONF_TIMEOUT: 15,
    })
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POLLING_INTERVAL] == 45
    assert result["data"][CONF_AUTO_SYNC_WEBHOOKS] is False
    assert result["data"][CONF_TIMEOUT] == 15


@pytest.mark.asyncio
async def test_lifecycle_setup_and_unload() -> None:
    """Scénario 23 : Initialisation et déchargement complets du cycle de vie (__init__.py)."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="lifecycle_entry_1",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: "a06fe048b48b260139",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-1234",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )

        setup_ok = await async_setup_entry(hass, entry)
        assert setup_ok is True
        assert entry.entry_id in hass.data[DOMAIN]
        runtime_data = hass.data[DOMAIN][entry.entry_id]
        assert runtime_data["client"] is not None
        assert entry.runtime_data["client"] is runtime_data["client"]

        unload_ok = await async_unload_entry(hass, entry)
        assert unload_ok is True
        assert entry.entry_id not in hass.data.get(DOMAIN, {})
        assert entry.runtime_data is None
    finally:
        await server.stop()



# ============================================================================
# TESTS PHASE 4 MILESTONE 4 : OPTIONS ENRICHIES, WEBHOOK LIFECYCLE & SYNC
# ============================================================================

@pytest.mark.asyncio
async def test_options_flow_form_rendering_with_selectors() -> None:
    """Scénario 24 : Rendu du formulaire d'options avec EntitySelector et case trigger_resync."""
    entry = ConfigEntry(
        entry_id="opt_render_entry",
        domain=DOMAIN,
        title="Sevenhugs Smart Remote (5726)",
        data={},
        options={
            CONF_ENTITIES: ["light.living_room_ceiling", "switch.kitchen_socket"],
            CONF_POLLING_INTERVAL: 45,
            CONF_AUTO_SYNC_WEBHOOKS: True,
            CONF_TIMEOUT: 12,
        },
    )
    handler = SevenhugsOptionsFlowHandler(entry)
    form = await handler.async_step_init(None)

    assert form["type"] == FlowResultType.FORM
    assert form["step_id"] == "init"
    assert "data_schema" in form

    schema = form["data_schema"].schema

    # 1. Vérification de CONF_ENTITIES
    entity_val = None
    for k, v in schema.items():
        if getattr(k, "schema", None) == CONF_ENTITIES:
            entity_val = v
            def_val = k.default() if callable(getattr(k, "default", None)) else getattr(k, "default", None)
            assert def_val == ["light.living_room_ceiling", "switch.kitchen_socket"]
            break
    assert entity_val is not None
    assert isinstance(entity_val, selector.EntitySelector)
    assert entity_val.config.multiple is True
    assert entity_val.config.domain == DEFAULT_EXPOSED_DOMAINS

    # 2. Vérification de CONF_TRIGGER_RESYNC
    trigger_found = False
    for k, v in schema.items():
        if getattr(k, "schema", None) == CONF_TRIGGER_RESYNC:
            trigger_found = True
            def_val = k.default() if callable(getattr(k, "default", None)) else getattr(k, "default", None)
            assert def_val is False
            break
    assert trigger_found is True

    # 3. Vérification des 3 options de base
    keys = [getattr(k, "schema", k) for k in schema.keys()]
    assert CONF_POLLING_INTERVAL in keys
    assert CONF_AUTO_SYNC_WEBHOOKS in keys
    assert CONF_TIMEOUT in keys


@pytest.mark.asyncio
async def test_options_flow_save_with_entities_and_trigger_resync() -> None:
    """Scénario 25 : Sauvegarde des options enrichies avec entités sélectionnées et trigger_resync."""
    entry = ConfigEntry(
        entry_id="opt_save_entry",
        domain=DOMAIN,
        title="Sevenhugs Smart Remote (5726)",
        data={},
        options={},
    )
    handler = SevenhugsOptionsFlowHandler(entry)

    user_input = {
        CONF_ENTITIES: ["light.living_room", "switch.fan", "scene.good_night"],
        CONF_TRIGGER_RESYNC: True,
        CONF_POLLING_INTERVAL: 90,
        CONF_AUTO_SYNC_WEBHOOKS: True,
        CONF_TIMEOUT: 15,
    }

    result = await handler.async_step_init(user_input)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENTITIES] == ["light.living_room", "switch.fan", "scene.good_night"]
    assert result["data"][CONF_TRIGGER_RESYNC] is True
    assert result["data"][CONF_POLLING_INTERVAL] == 90
    assert result["data"][CONF_AUTO_SYNC_WEBHOOKS] is True
    assert result["data"][CONF_TIMEOUT] == 15


@pytest.mark.asyncio
async def test_options_flow_backward_compatibility_legacy_keys() -> None:
    """Scénario 26 : Compatibilité ascendante stricte lors de la soumission des 3 seules clés historiques."""
    entry = ConfigEntry(
        entry_id="opt_legacy_entry",
        domain=DOMAIN,
        title="Sevenhugs Smart Remote (5726)",
        data={},
        options={
            CONF_POLLING_INTERVAL: 30,
            CONF_AUTO_SYNC_WEBHOOKS: True,
            CONF_TIMEOUT: 10,
        },
    )
    handler = SevenhugsOptionsFlowHandler(entry)

    legacy_input = {
        CONF_POLLING_INTERVAL: 120,
        CONF_AUTO_SYNC_WEBHOOKS: False,
        CONF_TIMEOUT: 20,
    }

    result = await handler.async_step_init(legacy_input)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POLLING_INTERVAL] == 120
    assert result["data"][CONF_AUTO_SYNC_WEBHOOKS] is False
    assert result["data"][CONF_TIMEOUT] == 20


@pytest.mark.asyncio
async def test_webhook_lifecycle_in_setup_and_unload() -> None:
    """Scénario 27 : Enregistrement et désenregistrement du webhook local lors de setup/unload."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="entry_wh_lifecycle_test",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: "a06fe048b48b260139",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-wh-test",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
        )

        expected_webhook_id = f"sevenhugs_{entry.entry_id}"
        assert expected_webhook_id not in getattr(hass.webhook, "_webhooks", {})

        setup_ok = await async_setup_entry(hass, entry)
        assert setup_ok is True
        assert expected_webhook_id in hass.webhook._webhooks
        wh_info = hass.webhook._webhooks[expected_webhook_id]
        assert wh_info.local_only is True
        assert "POST" in wh_info.allowed_methods

        runtime_data = hass.data[DOMAIN][entry.entry_id]
        assert runtime_data.get("webhook_id") == expected_webhook_id
        assert runtime_data.get("injector") is not None

        unload_ok = await async_unload_entry(hass, entry)
        assert unload_ok is True
        assert expected_webhook_id not in hass.webhook._webhooks
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_background_sync_scheduling_on_setup_and_options() -> None:
    """Scénario 28 : Planification en tâche de fond de la synchronisation lors du setup et changement d'options."""
    server = MockSmartRemoteServer()
    await server.start()
    try:
        hass = HomeAssistant()
        entry = ConfigEntry(
            entry_id="entry_bg_sync_test",
            domain=DOMAIN,
            title="Sevenhugs Smart Remote (5726)",
            data={
                CONF_HOST: server.host,
                CONF_PORT: server.port,
                CONF_SERIAL_NUMBER: "a06fe048b48b260139",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-sync-test",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
            options={
                CONF_ENTITIES: ["light.living_room"],
                CONF_AUTO_SYNC_WEBHOOKS: True,
                CONF_TRIGGER_RESYNC: True,
            },
        )

        created_tasks = []
        orig_async_create_task = hass.async_create_task

        def spy_async_create_task(target: Any, *args: Any, **kwargs: Any) -> Any:
            created_tasks.append(target)
            return orig_async_create_task(target)

        hass.async_create_task = spy_async_create_task

        with patch("custom_components.sevenhugs_smart_remote.lua_injector.SevenhugsLuaInjector.async_sync_entities") as mock_sync:
            async def fake_sync(*args, **kwargs):
                return True
            mock_sync.side_effect = fake_sync

            setup_ok = await async_setup_entry(hass, entry)
            assert setup_ok is True
            assert mock_sync.called
            assert len(created_tasks) > 0

        with patch.object(hass.config_entries, "async_reload") as mock_reload:
            async def fake_reload(eid):
                return True
            mock_reload.side_effect = fake_reload

            from custom_components.sevenhugs_smart_remote import update_listener
            await update_listener(hass, entry)
            mock_reload.assert_called_once_with(entry.entry_id)

        await async_unload_entry(hass, entry)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_options_flow_delete_remote_devices() -> None:
    """Scénario 29 : Sélection et suppression définitive d'appareils du carrousel de la télécommande."""
    from custom_components.sevenhugs_smart_remote.const import CONF_DELETE_DEVICES
    from unittest.mock import AsyncMock

    hass = HomeAssistant()
    entry = ConfigEntry(
        entry_id="entry_delete_devices_test",
        domain=DOMAIN,
        title="Sevenhugs Smart Remote (5726)",
        data={},
        options={},
    )
    mock_coord = unittest.mock.MagicMock()
    mock_coord.data = {
        "raw_settings": {
            "carousel_order": [
                {"id": "sevenhugs.control.hue:spot1", "name": "Hue Spot 1", "category": "lights"},
                {"id": "sevenhugs.control.groups:934fdf6a", "name": "plafond", "category": "lights"},
            ]
        }
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": mock_coord}

    handler = SevenhugsOptionsFlowHandler(entry)
    handler.hass = hass

    form = await handler.async_step_init(None)
    assert form["type"] == FlowResultType.FORM
    schema = form["data_schema"].schema
    delete_sel = None
    for k, v in schema.items():
        if getattr(k, "schema", None) == CONF_DELETE_DEVICES:
            delete_sel = v
            break
    assert delete_sel is not None
    assert len(delete_sel.config.options) == 2
    assert delete_sel.config.options[0]["value"] == "sevenhugs.control.hue:spot1"
    assert delete_sel.config.options[0]["label"] == "Hue Spot 1 (lights)"

    # Test de client.async_delete_remote_items
    from custom_components.sevenhugs_smart_remote.client import AsyncSmartRemoteClient
    client = AsyncSmartRemoteClient(host="127.0.0.1", session=unittest.mock.MagicMock(), keys={})
    calls: list[tuple[str, str, Any]] = []

    async def fake_request(method: str, path: str, json_data: Any = None) -> Any:
        calls.append((method, path, json_data))
        if method == "GET" and path == "/api/devices":
            return [{"category": "lights", "devices_info": [{"package_id": "sevenhugs.control.hue", "device_id": "spot1", "name": "Hue Spot 1"}]}]
        if method == "GET" and path == "/api/components-groups":
            return [{"id": "934fdf6a", "package_id": "sevenhugs.control.groups", "name": "plafond"}]
        if method == "GET" and path == "/api/settings":
            return {"carousel_order": [{"id": "sevenhugs.control.hue:spot1"}, {"id": "sevenhugs.control.groups:934fdf6a"}]}
        return {}

    client.async_request = AsyncMock(side_effect=fake_request)
    deleted = await client.async_delete_remote_items(["sevenhugs.control.hue:spot1", "sevenhugs.control.groups:934fdf6a"])
    assert deleted == 2
    delete_calls = [c for c in calls if c[0] == "DELETE"]
    assert len(delete_calls) == 2
    assert delete_calls[0][1] == "/api/devices"
    assert delete_calls[1][1] == "/api/components-groups"

    # Test de compatibilité MappingProxyType (Home Assistant Core 2024+ / 2026+)
    from types import MappingProxyType
    from custom_components.sevenhugs_smart_remote.lua_injector import SevenhugsLuaInjector

    entry.data = MappingProxyType({CONF_REMOTE_ID: "5726", CONF_SERIAL_NUMBER: "sn_mp"})  # type: ignore[assignment]
    entry.options = MappingProxyType({  # type: ignore[assignment]
        CONF_ENTITIES: ["light.salon", "script.cinema"],
        CONF_DELETE_DEVICES: ["sevenhugs.control.hue:spot1"],
    })
    injector = SevenhugsLuaInjector(hass, entry, client)
    client.async_push_webhooks = AsyncMock(return_value=True)
    sync_ok = await injector.async_sync_entities()
    assert sync_ok is True
    client.async_push_webhooks.assert_awaited_once()
    pushed_triggers = client.async_push_webhooks.call_args[0][0]
    assert len(pushed_triggers) == 2


# ============================================================================
# EXÉCUTION UNITTEST & PYTEST DUAL-COMPATIBLE
# ============================================================================

ALL_CONFIG_FLOW_TESTS = [
    test_zeroconf_discovery_success,
    test_zeroconf_bytes_properties,
    test_zeroconf_ip_update_already_configured,
    test_user_step_initial_form,
    test_user_step_valid_host,
    test_user_step_cannot_connect,
    test_pair_init_latency_non_blocking,
    test_pair_init_remote_busy_500,
    test_pair_init_timeout_connect,
    test_pair_pin_valid_formatted,
    test_pair_pin_valid_unformatted,
    test_pair_pin_invalid_format_short,
    test_pair_pin_invalid_format_letters,
    test_pair_pin_auth_failed_bad_pin,
    test_pair_pin_pairing_failed_network,
    test_device_registry_entry_created,
    test_already_configured_manual_entry,
    test_reconfigure_flow_success,
    test_reconfigure_flow_cannot_connect,
    test_reconfigure_flow_mismatched_device,
    test_reauth_flow_success,
    test_options_flow_init_and_save,
    test_lifecycle_setup_and_unload,
    # Phase 4 Milestone 4 tests:
    test_options_flow_form_rendering_with_selectors,
    test_options_flow_save_with_entities_and_trigger_resync,
    test_options_flow_backward_compatibility_legacy_keys,
    test_webhook_lifecycle_in_setup_and_unload,
    test_background_sync_scheduling_on_setup_and_options,
    test_options_flow_delete_remote_devices,
]


def load_tests(loader: Any, standard_tests: Any, pattern: Any) -> Any:
    """Protocole unittest load_tests pour exécuter les 28 tests asynchrones."""
    suite = unittest.TestSuite()
    for fn in ALL_CONFIG_FLOW_TESTS:
        tc = unittest.FunctionTestCase(lambda f=fn: asyncio.run(f()), description=fn.__name__)
        suite.addTest(tc)
    return suite


if __name__ == "__main__":
    passed = 0
    failed = 0
    print("\n=== Exécution de tests/test_config_flow.py ===")
    for test_fn in ALL_CONFIG_FLOW_TESTS:
        try:
            asyncio.run(test_fn())
            print(f"  [OK] {test_fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {test_fn.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n=== Bilan test_config_flow.py : {passed} réussis, {failed} échoués ===\n")
    sys.exit(1 if failed else 0)
