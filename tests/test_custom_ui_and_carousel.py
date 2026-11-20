"""Tests unitaires et E2E pour le Jalon v0.6 (Ordonnancement du Carrousel & Grilles Custom UI 6x12)."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock  # noqa: F401
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from tests.conftest import MockSmartRemoteServer
from cryptography.hazmat.primitives.asymmetric import ed25519

from custom_components.smart_remote_rebirth import (
    DOMAIN,
     async_setup_entry,
    async_unload_entry,
)
from custom_components.smart_remote_rebirth.config_flow import (
    SmartRemoteOptionsFlowHandler,
)
from custom_components.smart_remote_rebirth.const import (
    CONF_AUTO_SYNC_WEBHOOKS,
    CONF_CAROUSEL_ORDER,
    CONF_CLIENT_PRIVATE_KEY,
    CONF_CLIENT_PUBLIC_KEY,
    CONF_CONTROLLER_ID,
    CONF_CUSTOMIZE_DEVICE,
    CONF_ENTITIES,
    CONF_HOST,
    CONF_PORT,
    CONF_REMOTE_ID,
    CONF_REMOTE_PUBLIC_KEY,
    CONF_SERIAL_NUMBER,
    SERVICE_RESTORE_CUSTOM_UI_LAYOUT,
    SERVICE_SET_CAROUSEL_ORDER,
    SERVICE_SET_CUSTOM_UI_LAYOUT,
)
from custom_components.smart_remote_rebirth.custom_ui import (
    build_custom_ui_payload,
    merge_carousel_order,
    resolve_component_id_from_carousel,
    validate_component_id,
)


class TestCarouselAndCustomUiEngine(unittest.TestCase):
    """Tests unitaires du moteur custom_ui.py (validation, carrousel et grille 6x12)."""

    def test_validate_component_id_accepts_valid_and_rejects_malicious(self) -> None:
        """Vérifie la validation stricte anti-SSRF/traversée de component_id."""
        pkg, dev = validate_component_id("sevenhugs.control.infrared:SOLO5_M3708_fff1980f")
        self.assertEqual(pkg, "sevenhugs.control.infrared")
        self.assertEqual(dev, "SOLO5_M3708_fff1980f")

        for bad_id in (
            "",
            "   ",
            "no_colon_id",
            "pkg:",
            ":dev",
            "pkg:dev:extra",
            "http://evil.local/api",
            "sevenhugs.control.infrared:../setup",
            "sevenhugs.control.infrared:dev/commands",
            "sevenhugs.control.infrared:dev with space",
        ):
            with self.assertRaises(ValueError, msg=f"Aurait dû rejeter {bad_id!r}"):
                validate_component_id(bad_id)

    def test_resolve_component_id_from_carousel_by_name_or_id(self) -> None:
        """Vérifie la résolution d'un appareil par son nom convivial ou son ID technique."""
        carousel = [
            {"id": "sevenhugs.control.infrared:osm_T3825_31572c5c", "name": "Panasonic TV"},
            {"id": "sevenhugs.control.infrared:SOLO5_M3708_fff1980f", "name": "Bose SOLO5"},
        ]
        self.assertEqual(
            resolve_component_id_from_carousel("bose solo5", carousel),
            "sevenhugs.control.infrared:SOLO5_M3708_fff1980f",
        )
        self.assertEqual(
            resolve_component_id_from_carousel(
                "sevenhugs.control.infrared:osm_T3825_31572c5c", carousel
            ),
            "sevenhugs.control.infrared:osm_T3825_31572c5c",
        )
        # Fallback direct si ID syntaxiquement valide hors carrousel
        self.assertEqual(
            resolve_component_id_from_carousel("sevenhugs.control.dummy_tv:tv_1", carousel),
            "sevenhugs.control.dummy_tv:tv_1",
        )
        # Rejet si nom inconnu et non conforme au format package_id:device_id
        with self.assertRaises(ValueError):
            resolve_component_id_from_carousel("Appareil Inconnu", carousel)

    def test_merge_carousel_order_non_destructive_and_pin_ha_first(self) -> None:
        """Vérifie la fusion non-destructive, le renommage et l'épinglage en tête du carrousel."""
        current = [
            {"id": "sevenhugs.control.hue:spot1", "name": "Spot 1", "hidden": False},
            {"id": "sevenhugs.control.infrared:tv1", "name": "Panasonic TV", "hidden": False},
            {"id": "sevenhugs.control.infrared:bose1", "name": "Bose SOLO5", "hidden": False},
            {"id": "sevenhugs.control.webhooks:1", "name": "Webhooks", "hidden": False},
        ]

        # 1. Renommage + épinglage automatique de l'écran Home Assistant en 1ère position
        pinned = merge_carousel_order(
            current,
            webhook_screen_name="Home Assistant",
            pin_ha_first=True,
        )
        self.assertEqual(pinned[0]["id"], "sevenhugs.control.webhooks:1")
        self.assertEqual(pinned[0]["name"], "Home Assistant")
        self.assertEqual(len(pinned), 4)

        # 2. Réordonnancement partiel avec surcharge de nom et visibilité
        reordered = merge_carousel_order(
            current,
            ordered_items=[
                {"id": "Bose SOLO5", "name": "Barre Bose", "hidden": True},
                "home_assistant",
            ],
            webhook_screen_name="Maison HA",
            pin_ha_first=False,
        )
        self.assertEqual(
            [item["id"] for item in reordered],
            [
                "sevenhugs.control.infrared:bose1",
                "sevenhugs.control.webhooks:1",
                "sevenhugs.control.hue:spot1",
                "sevenhugs.control.infrared:tv1",
            ],
        )
        self.assertEqual(reordered[0]["name"], "Barre Bose")
        self.assertTrue(reordered[0]["hidden"])
        self.assertEqual(reordered[1]["name"], "Maison HA")

    def test_build_custom_ui_payload_auto_placement_and_pagination(self) -> None:
        """Vérifie l'auto-placement 6x12 et le passage automatique à une 2e page quand la 1ère est pleine."""
        # Un widget volume_channel (6x6 -> occupe y=0..5) + 9 boutons 2x2 (occupent y=6..11, 3 par ligne)
        # + 1 dixième bouton 2x2 qui doit basculer automatiquement sur l'écran #2 à (0, 0) !
        widgets: list[dict[str, Any]] = [{"type": "volume_channel"}]
        for i in range(1, 11):
            widgets.append({
                "type": "button",
                "key": f"btn_{i}",
                "name": f"Bouton {i}",
                "long_press_key": "stop" if i == 1 else None,
            })

        payload = build_custom_ui_payload(
            "sevenhugs.control.infrared:SOLO5_M3708_fff1980f",
            widgets=widgets,
            available_commands=[{"commands": [{"key": "vol_up", "name": "Volume +"}]}],
        )

        self.assertEqual(payload["package_id"], "sevenhugs.control.infrared")
        self.assertEqual(payload["device_id"], "SOLO5_M3708_fff1980f")
        self.assertTrue(payload["customized"])

        screens = payload["layout"]["fragments"][0]["screens"]
        self.assertEqual(len(screens), 2, "Un 2e écran 6x12 doit être créé automatiquement")

        screen1_widgets = screens[0]["widgets"]
        screen2_widgets = screens[1]["widgets"]
        self.assertEqual(len(screen1_widgets), 10)  # 1 volume_channel + 9 buttons
        self.assertEqual(len(screen2_widgets), 1)   # 10th button on page 2

        # Vérification des coordonnées sur la page 1
        self.assertEqual((screen1_widgets[0]["x"], screen1_widgets[0]["y"]), (0, 0))
        self.assertEqual((screen1_widgets[1]["x"], screen1_widgets[1]["y"]), (0, 6))
        self.assertEqual((screen1_widgets[2]["x"], screen1_widgets[2]["y"]), (2, 6))
        self.assertEqual((screen1_widgets[3]["x"], screen1_widgets[3]["y"]), (4, 6))
        self.assertEqual((screen1_widgets[9]["x"], screen1_widgets[9]["y"]), (4, 10))

        # Vérification de l'appui long sur le 1er bouton
        self.assertIn("long_press", screen1_widgets[1]["controls"][0])
        self.assertEqual(screen1_widgets[1]["controls"][0]["long_press"]["key"], "stop")

        # Vérification du 10e bouton sur la page 2 à (0, 0)
        self.assertEqual((screen2_widgets[0]["x"], screen2_widgets[0]["y"]), (0, 0))

    def test_build_custom_ui_payload_detects_collisions_and_out_of_bounds(self) -> None:
        """Vérifie le rejet des widgets hors grille 6x12 ou en collision."""
        # 1. Dépassement horizontal (bouton 2x2 placé à x=5 -> 5+2 = 7 > 6)
        with self.assertRaises(ValueError):
            build_custom_ui_payload(
                "sevenhugs.control.infrared:SOLO5",
                widgets=[{"type": "button", "key": "mute", "x": 5, "y": 0}],
            )

        # 2. Dépassement vertical (navigation 6x6 placé à y=8 -> 8+6 = 14 > 12)
        with self.assertRaises(ValueError):
            build_custom_ui_payload(
                "sevenhugs.control.infrared:SOLO5",
                widgets=[{"type": "navigation", "x": 0, "y": 8}],
            )

        # 3. Collision entre deux widgets sur la même cellule
        with self.assertRaises(ValueError):
            build_custom_ui_payload(
                "sevenhugs.control.infrared:SOLO5",
                widgets=[
                    {"type": "navigation", "x": 0, "y": 0},
                    {"type": "button", "key": "mute", "x": 2, "y": 4},
                ],
            )


class TestJalonV06EndToEndIntegration(unittest.IsolatedAsyncioTestCase):
    """Tests d'intégration E2E du Jalon v0.6 avec MockSmartRemoteServer et Home Assistant."""

    async def asyncSetUp(self) -> None:
        self.hass = HomeAssistant()
        self.server = MockSmartRemoteServer(pair_setup_delay=0.01)
        await self.server.start()

        client_sk = ed25519.Ed25519PrivateKey.generate()
        self.entry = ConfigEntry(
            entry_id="entry_v06_e2e",
            domain=DOMAIN,
            title="Smart Remote (5726)",
            data={
                CONF_HOST: self.server.host,
                CONF_PORT: self.server.port,
                CONF_SERIAL_NUMBER: "a06fe048b48b260139",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-v06-e2e",
                CONF_CLIENT_PRIVATE_KEY: client_sk.private_bytes_raw().hex(),
                CONF_CLIENT_PUBLIC_KEY: client_sk.public_key().public_bytes_raw().hex(),
                CONF_REMOTE_PUBLIC_KEY: self.server.remote_pk.hex(),
            },
            options={
                CONF_ENTITIES: ["light.salon"],
                CONF_AUTO_SYNC_WEBHOOKS: True,
            },
            unique_id="a06fe048b48b260139",
        )
        self.hass.states.async_set("light.salon", "off", {"friendly_name": "Lumière Salon"})
        self.hass.config_entries.async_add(self.entry)

    async def asyncTearDown(self) -> None:
        await self.server.stop()

    async def test_post_lua_sync_pins_and_renames_home_assistant_screen(self) -> None:
        """Vérifie qu'après la synchronisation Lua, l'écran Webhooks est renommé 'Home Assistant' et placé 1er."""
        setup_ok = await async_setup_entry(self.hass, self.entry)
        self.assertTrue(setup_ok)

        # Attendre la fin de la tâche de synchronisation Lua en arrière-plan
        sync_task = self.hass.data[DOMAIN][self.entry.entry_id].get("_sync_task")
        if sync_task is not None:
            await sync_task

        self.assertGreaterEqual(len(self.server.carousel_order), 3)
        first_screen = self.server.carousel_order[0]
        self.assertEqual(first_screen["id"], "sevenhugs.control.webhooks:1")
        self.assertEqual(first_screen["name"], "Home Assistant")

        await async_unload_entry(self.hass, self.entry)

    async def test_services_set_carousel_order_and_custom_ui_6x12(self) -> None:
        """Vérifie l'exécution de bout en bout des 3 services HA du Jalon v0.6."""
        await async_setup_entry(self.hass, self.entry)
        sync_task = self.hass.data[DOMAIN][self.entry.entry_id].get("_sync_task")
        if sync_task is not None:
            await sync_task

        # 1. Service set_carousel_order : placer Bose SOLO5 en tête avec un nouveau nom
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CAROUSEL_ORDER,
            {
                "items": [
                    {"id": "Bose SOLO5", "name": "Barre de Son Bose"},
                    "Panasonic TV",
                ],
                "pin_ha_first": False,
            },
            blocking=True,
        )
        self.assertEqual(
            self.server.carousel_order[0]["id"],
            "sevenhugs.control.infrared:SOLO5_M3708_fff1980f",
        )
        self.assertEqual(self.server.carousel_order[0]["name"], "Barre de Son Bose")
        self.assertEqual(
            self.server.carousel_order[1]["id"],
            "sevenhugs.control.infrared:osm_T3825_31572c5c",
        )

        # 2. Service set_custom_ui_layout : configurer une grille 6x12 sur la barre Bose
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CUSTOM_UI_LAYOUT,
            {
                "device": "Barre de Son Bose",
                "top_key": "power",
                "widgets": [
                    {"type": "volume_channel"},
                    {"type": "button", "key": "mute", "name": "Silence"},
                    {"type": "button", "key": "play", "long_press_key": "stop"},
                ],
            },
            blocking=True,
        )

        comp_id = "sevenhugs.control.infrared:SOLO5_M3708_fff1980f"
        self.assertIn(comp_id, self.server.custom_ui_store)
        saved_ui = self.server.custom_ui_store[comp_id]
        self.assertTrue(saved_ui["customized"])
        widgets = saved_ui["layout"]["fragments"][0]["screens"][0]["widgets"]
        self.assertEqual(len(widgets), 3)
        self.assertEqual(widgets[0]["type"], "volume_channel")
        self.assertEqual((widgets[1]["x"], widgets[1]["y"]), (0, 6))
        self.assertEqual(widgets[1]["controls"][0]["touch"]["name"], "Silence")
        self.assertEqual((widgets[2]["x"], widgets[2]["y"]), (2, 6))

        # 3. Service restore_custom_ui_layout : restaurer la disposition par défaut
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_RESTORE_CUSTOM_UI_LAYOUT,
            {"device": "Barre de Son Bose"},
            blocking=True,
        )
        self.assertIn(comp_id, self.server.restored_custom_ui_ids)

        # 4. Vérification que la collision 6x12 lève bien HomeAssistantError
        with self.assertRaises(HomeAssistantError):
            await self.hass.services.async_call(
                DOMAIN,
                SERVICE_SET_CUSTOM_UI_LAYOUT,
                {
                    "device": comp_id,
                    "widgets": [
                        {"type": "button", "key": "mute", "x": 0, "y": 0},
                        {"type": "button", "key": "play", "x": 1, "y": 1},
                    ],
                },
                blocking=True,
            )

        await async_unload_entry(self.hass, self.entry)

    async def test_options_flow_records_reordered_carousel_devices(self) -> None:
        """Vérifie que le réordonnancement des pastilles 📟 dans l'Options Flow enregistre CONF_CAROUSEL_ORDER."""
        await async_setup_entry(self.hass, self.entry)
        sync_task = self.hass.data[DOMAIN][self.entry.entry_id].get("_sync_task")
        if sync_task is not None:
            await sync_task

        flow = SmartRemoteOptionsFlowHandler(self.entry)
        flow.hass = self.hass

        # Affichage initial du formulaire (ordre actuel : Panasonic TV puis Bose SOLO5)
        await flow.async_step_init(None)

        # Soumission avec Bose SOLO5 placé AVANT Panasonic TV
        res = await flow.async_step_init({
            CONF_ENTITIES: [
                "sevenhugs.control.infrared:SOLO5_M3708_fff1980f",
                "sevenhugs.control.infrared:osm_T3825_31572c5c",
                "light.salon",
            ],
        })
        self.assertEqual(
            res["data"][CONF_CAROUSEL_ORDER],
            [
                "sevenhugs.control.infrared:SOLO5_M3708_fff1980f",
                "sevenhugs.control.infrared:osm_T3825_31572c5c",
            ],
        )
        self.assertEqual(res["data"][CONF_ENTITIES], ["light.salon"])

        await async_unload_entry(self.hass, self.entry)

    async def test_options_flow_graphical_custom_ui_wizard(self) -> None:
        """Vérifie l'assistant graphique en 2 étapes (init -> custom_ui) dans la roue crantée ⚙️ Configurer."""
        await async_setup_entry(self.hass, self.entry)
        sync_task = self.hass.data[DOMAIN][self.entry.entry_id].get("_sync_task")
        if sync_task is not None:
            await sync_task

        flow = SmartRemoteOptionsFlowHandler(self.entry)
        flow.hass = self.hass

        # Étape 1 : Sélection d'un appareil à personnaliser dans le formulaire principal
        await flow.async_step_init(None)
        comp_id = "sevenhugs.control.infrared:SOLO5_M3708_fff1980f"
        step1_res = await flow.async_step_init({
            CONF_ENTITIES: [
                "sevenhugs.control.infrared:osm_T3825_31572c5c",
                comp_id,
                "light.salon",
            ],
            CONF_CUSTOMIZE_DEVICE: comp_id,
        })

        # Doit ouvrir l'étape 2 graphique "custom_ui" pré-remplie avec les commandes de l'appareil
        self.assertEqual(step1_res["type"], "form")
        self.assertEqual(step1_res["step_id"], "custom_ui")
        self.assertEqual(step1_res["description_placeholders"]["device_name"], "Bose SOLO5")

        # Étape 2 : Soumission du formulaire graphique de la grille 6x12
        step2_res = await flow.async_step_custom_ui({
            "custom_ui_action": "customize",
            "screen_title": "Cinéma Bose",
            "screen_hidden": False,
            "top_key": "power",
            "main_widget": "volume_channel",
            "button_keys": ["mute", "play", "stop"],
        })

        self.assertEqual(step2_res["type"], "create_entry")
        self.assertEqual(step2_res["data"][CONF_ENTITIES], ["light.salon"])

        # Vérifie que la grille 6x12 a été poussée en direct sur la télécommande
        self.assertIn(comp_id, self.server.custom_ui_store)
        saved_ui = self.server.custom_ui_store[comp_id]
        widgets = saved_ui["layout"]["fragments"][0]["screens"][0]["widgets"]
        self.assertEqual(len(widgets), 4)  # 1 volume_channel (6x6) + 3 boutons (2x2)
        self.assertEqual(widgets[0]["type"], "volume_channel")
        self.assertEqual((widgets[1]["x"], widgets[1]["y"]), (0, 6))
        self.assertEqual((widgets[2]["x"], widgets[2]["y"]), (2, 6))
        self.assertEqual((widgets[3]["x"], widgets[3]["y"]), (4, 6))

        # Vérifie que l'écran a aussi été renommé dans le carrousel
        bose_item = next(i for i in self.server.carousel_order if i["id"] == comp_id)
        self.assertEqual(bose_item["name"], "Cinéma Bose")

        await async_unload_entry(self.hass, self.entry)

    async def test_options_flow_one_click_device_menu(self) -> None:
        """Vérifie le menu 1-clic dans ⚙️ Configurer : liste d'appareils avec bouton Personnaliser direct sans déroulant."""
        from unittest.mock import patch

        self.entry.options = {CONF_ENTITIES: ["light.salon"]}
        await async_setup_entry(self.hass, self.entry)
        sync_task = self.hass.data[DOMAIN][self.entry.entry_id].get("_sync_task")
        if sync_task is not None:
            await sync_task

        flow = SmartRemoteOptionsFlowHandler(self.entry)
        flow.hass = self.hass

        with patch(
            "custom_components.smart_remote_rebirth.config_flow.ha_section",
            side_effect=lambda schema, opts=None: schema,
        ):
            # 1. L'ouverture de ⚙️ Configurer affiche immédiatement un menu de boutons 1-clic
            menu_res = await flow.async_step_init(None)
            self.assertEqual(menu_res["type"], "menu")
            self.assertEqual(menu_res["step_id"], "init")
            menu_opts = menu_res["menu_options"]
            self.assertIn("carousel", menu_opts)
            self.assertIn("custom_device_0", menu_opts)
            self.assertIn("custom_device_1", menu_opts)
            self.assertIn("Panasonic TV", menu_opts["custom_device_0"])
            self.assertIn("Bose SOLO5", menu_opts["custom_device_1"])

            # 2. Un simple clic sur le bouton de Bose SOLO5 (custom_device_1) ouvre directement son éditeur 6x12
            step_fn = getattr(flow, "async_step_custom_device_1")
            custom_form = await step_fn()
            self.assertEqual(custom_form["type"], "form")
            self.assertEqual(custom_form["step_id"], "custom_ui")
            self.assertEqual(custom_form["description_placeholders"]["device_name"], "Bose SOLO5")

            # 3. La validation de l'écran 6x12 pousse la grille et préserve intactes les options existantes
            save_res = await flow.async_step_custom_ui({
                "custom_ui_action": "customize",
                "screen_title": "Bose Direct 1-Clic",
                "screen_hidden": False,
                "top_key": "power",
                "main_widget": "volume_channel",
                "button_keys": ["mute"],
            })
            self.assertEqual(save_res["type"], "create_entry")
            self.assertEqual(save_res["data"][CONF_ENTITIES], ["light.salon"])

            # 4. Le clic sur "carousel" dans le menu ouvre le formulaire du carrousel (sans liste déroulante customize_device)
            carousel_form = await flow.async_step_carousel(None)
            self.assertEqual(carousel_form["type"], "form")
            self.assertEqual(carousel_form["step_id"], "carousel")
            schema_keys = [getattr(k, "schema", str(k)) for k in carousel_form["data_schema"].schema.keys()]
            self.assertIn(CONF_ENTITIES, schema_keys)
            self.assertNotIn(CONF_CUSTOMIZE_DEVICE, schema_keys)

        await async_unload_entry(self.hass, self.entry)


if __name__ == "__main__":
    unittest.main()
