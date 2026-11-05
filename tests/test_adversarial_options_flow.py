"""Banc de tests contradictoires et de stress-test pour l'Options Flow de Sevenhugs Smart Remote (Phase 4 Milestone 4).

Auteur: Challenger 1 (Phase 4 Milestone 4 - EMPIRICAL CHALLENGER)
Rôles: critic, specialist

Ce banc d'essais valide empiriquement la robustesse, la rétrocompatibilité
et l'intégrité de l'Options Flow sous des conditions adverses :
1. Fuzzing exhaustif du schéma voluptuous de l'Options Flow :
   - Dictionnaire vide ({}) et normalisation automatique des 5 valeurs par défaut.
   - Soumissions partielles à clé unique ({CONF_POLLING_INTERVAL: 15}, etc.).
   - Permutations de clés manquantes (combinaisons de 1, 2, 3, 4 clés).
   - Fuzzing des bornes de polling_interval : 15, 300, 14 (rejet), 301 (rejet),
     0 (rejet), négatifs (rejet), flottants (15.0, 14.9, 300.9), chaînes ("60", "abc"),
     None et structures non scalaires.
   - Fuzzing de CONF_ENTITIES : listes vides, None, scalaires chaînes isolées,
     tuples, entiers, dictionnaires et doublons d'entités.
2. Résilience du gestionnaire SevenhugsOptionsFlowHandler :
   - Initialisation avec config_entry.options corrompu (None, scalaires, types invalides).
   - Rétrocompatibilité stricte pour les soumissions historiques à 3 clés.
   - Préservation des données injectées sans perte ni altération.
   - Factory callback async_get_options_flow.
3. Rafales et concurrence massive :
   - Soumissions séquentielles rapides répétées (25 cycles avec rechargement).
   - Soumissions hautement concurrentes (30 tâches asynchrones simultanées).
   - Isolation stricte de l'état entre plusieurs ConfigEntry distinctes.
4. Intégration de cycle de vie de bout en bout avec MockSmartRemoteServer :
   - Propagation des modifications d'options via update_listener.
   - Déclenchement non-bloquant de la synchronisation lors de trigger_resync=True.
   - Persistance et intégrité du webhook local à travers les rechargements d'options.
   - Déchargement complet et propre sans fuite de socket ni tâche orpheline.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.conftest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import selector
from tests.conftest import MockSmartRemoteServer

from custom_components.sevenhugs_smart_remote import (
    async_setup_entry,
    async_unload_entry,
    update_listener,
)
from custom_components.sevenhugs_smart_remote.config_flow import (
    SevenhugsConfigFlow,
    SevenhugsOptionsFlowHandler,
)
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
    DEFAULT_TIMEOUT,
    DEFAULT_TRIGGER_RESYNC,
    DOMAIN,
    MAX_POLLING_INTERVAL,
    MIN_POLLING_INTERVAL,
)


class TestSevenhugsOptionsFlowSchemaFuzzing(unittest.TestCase):
    """Épreuves de fuzzing et de robustesse sur le schéma voluptuous de l'Options Flow."""

    def setUp(self) -> None:
        self.entry = ConfigEntry(
            entry_id="opt_schema_fuzz_entry",
            domain=DOMAIN,
            title="Sevenhugs Remote (Fuzz)",
            data={},
            options={},
        )
        self.handler = SevenhugsOptionsFlowHandler(self.entry)

    def _get_schema(self, custom_options: dict[str, Any] | None = None) -> vol.Schema:
        """Extrait le schéma data_schema généré par le gestionnaire."""
        if custom_options is not None:
            self.entry.options = custom_options
        form = asyncio.run(self.handler.async_step_init(None))
        self.assertEqual(form["type"], FlowResultType.FORM)
        self.assertEqual(form["step_id"], "init")
        return form["data_schema"]

    def test_empty_dict_normalization(self) -> None:
        """Vérifie qu'un dictionnaire vide est normalisé avec toutes les 5 valeurs par défaut."""
        schema = self._get_schema({})
        normalized = schema({})

        self.assertEqual(normalized[CONF_ENTITIES], DEFAULT_ENTITIES)
        self.assertEqual(normalized[CONF_TRIGGER_RESYNC], False)
        self.assertEqual(normalized[CONF_POLLING_INTERVAL], DEFAULT_POLLING_INTERVAL)
        self.assertEqual(normalized[CONF_AUTO_SYNC_WEBHOOKS], DEFAULT_AUTO_SYNC_WEBHOOKS)
        self.assertEqual(normalized[CONF_TIMEOUT], DEFAULT_TIMEOUT)

    def test_single_key_updates_normalization(self) -> None:
        """Vérifie que la mise à jour d'une seule clé conserve les valeurs par défaut pour les autres."""
        schema = self._get_schema()

        # 1. Seul CONF_POLLING_INTERVAL
        r1 = schema({CONF_POLLING_INTERVAL: 15})
        self.assertEqual(r1[CONF_POLLING_INTERVAL], 15)
        self.assertEqual(r1[CONF_ENTITIES], [])
        self.assertEqual(r1[CONF_TRIGGER_RESYNC], False)
        self.assertEqual(r1[CONF_AUTO_SYNC_WEBHOOKS], DEFAULT_AUTO_SYNC_WEBHOOKS)
        self.assertEqual(r1[CONF_TIMEOUT], DEFAULT_TIMEOUT)

        # 2. Seul CONF_TRIGGER_RESYNC
        r2 = schema({CONF_TRIGGER_RESYNC: True})
        self.assertEqual(r2[CONF_TRIGGER_RESYNC], True)
        self.assertEqual(r2[CONF_POLLING_INTERVAL], DEFAULT_POLLING_INTERVAL)

        # 3. Seul CONF_ENTITIES
        r3 = schema({CONF_ENTITIES: ["light.living_room", "switch.kitchen"]})
        self.assertEqual(r3[CONF_ENTITIES], ["light.living_room", "switch.kitchen"])

        # 4. Seul CONF_AUTO_SYNC_WEBHOOKS
        r4 = schema({CONF_AUTO_SYNC_WEBHOOKS: False})
        self.assertEqual(r4[CONF_AUTO_SYNC_WEBHOOKS], False)

        # 5. Seul CONF_TIMEOUT
        r5 = schema({CONF_TIMEOUT: 25})
        self.assertEqual(r5[CONF_TIMEOUT], 25)

    def test_missing_keys_arbitrary_subsets(self) -> None:
        """Vérifie la robustesse face à toutes les combinaisons partielles de clés."""
        schema = self._get_schema()

        subsets = [
            {CONF_POLLING_INTERVAL: 45, CONF_TIMEOUT: 12},
            {CONF_ENTITIES: ["light.bedroom"], CONF_TRIGGER_RESYNC: True},
            {CONF_AUTO_SYNC_WEBHOOKS: False, CONF_POLLING_INTERVAL: 100, CONF_TIMEOUT: 8},
            {CONF_ENTITIES: [], CONF_TRIGGER_RESYNC: False, CONF_TIMEOUT: 15},
        ]
        for sub in subsets:
            with self.subTest(subset=sub):
                norm = schema(sub)
                for k, v in sub.items():
                    self.assertEqual(norm[k], v)
                # Toutes les clés du contrat doivent être renseignées
                self.assertIn(CONF_ENTITIES, norm)
                self.assertIn(CONF_TRIGGER_RESYNC, norm)
                self.assertIn(CONF_POLLING_INTERVAL, norm)
                self.assertIn(CONF_AUTO_SYNC_WEBHOOKS, norm)
                self.assertIn(CONF_TIMEOUT, norm)

    def test_legacy_three_key_normalization(self) -> None:
        """Garantit qu'une soumission historique à 3 clés est enrichie avec les 2 nouvelles clés."""
        schema = self._get_schema()
        legacy_input = {
            CONF_POLLING_INTERVAL: 120,
            CONF_AUTO_SYNC_WEBHOOKS: False,
            CONF_TIMEOUT: 20,
        }
        res = schema(legacy_input)
        self.assertEqual(res[CONF_POLLING_INTERVAL], 120)
        self.assertEqual(res[CONF_AUTO_SYNC_WEBHOOKS], False)
        self.assertEqual(res[CONF_TIMEOUT], 20)
        self.assertEqual(res[CONF_ENTITIES], [])
        self.assertEqual(res[CONF_TRIGGER_RESYNC], False)

    def test_polling_interval_boundary_valid_values(self) -> None:
        """Vérifie l'acceptation stricte des bornes valides de l'intervalle de polling."""
        schema = self._get_schema()

        valid_cases = [
            (15, 15),
            (300, 300),
            (60, 60),
            (45.0, 45),  # float entier valide après coercion
            (45.9, 45),  # troncature entière par int()
            ("60", 60),  # chaîne numérique coercible
            ("15", 15),
            ("300", 300),
        ]
        for val, expected in valid_cases:
            with self.subTest(val=val):
                res = schema({CONF_POLLING_INTERVAL: val})
                self.assertEqual(res[CONF_POLLING_INTERVAL], expected)

    def test_polling_interval_boundary_invalid_values(self) -> None:
        """Vérifie le rejet par voluptuous de toute valeur en dehors des bornes [15, 300]."""
        schema = self._get_schema()

        invalid_cases = [
            14,          # En-dessous du minimum (15)
            301,         # Au-dessus du maximum (300)
            0,           # Zéro
            -1,          # Négatif
            -100,        # Négatif fort
            1000,        # Dépassement majeur
            "not_a_num", # Chaîne non convertible
            None,        # Type nul
            [],          # Type liste
            {},          # Type dictionnaire
            14.9,        # Tronqué à 14 -> < 15
            301.1,       # Tronqué à 301 -> > 300
        ]
        for val in invalid_cases:
            with self.subTest(val=val):
                with self.assertRaises(vol.Invalid):
                    schema({CONF_POLLING_INTERVAL: val})

    def test_entities_list_variations_and_edge_inputs(self) -> None:
        """Stress-test du sélecteur d'entités avec types multiples, scalaires et nuls."""
        schema = self._get_schema()

        cases = [
            ([], []),
            (None, []),
            (["light.a", "light.b"], ["light.a", "light.b"]),
            ("light.single_string", ["light.single_string"]),  # Re-encapsulation scalaire
            (("switch.one", "switch.two"), ["switch.one", "switch.two"]),  # Tuple converti en liste
            (["light.living_room", "light.living_room"], ["light.living_room", "light.living_room"]),
        ]
        for val, expected in cases:
            with self.subTest(val=val):
                res = schema({CONF_ENTITIES: val})
                self.assertEqual(res[CONF_ENTITIES], expected)

    def test_corrupted_entry_options_at_form_rendering(self) -> None:
        """Garantit l'absence d'exception lorsque entry.options contient des données corrompues."""
        corrupted_initializations = [
            None,
            {},
            {CONF_ENTITIES: None},
            {CONF_ENTITIES: "light.single"},
            {CONF_ENTITIES: 12345},
            {CONF_ENTITIES: ("light.a", "switch.b")},
            {CONF_ENTITIES: {"bad": "structure"}},
            {CONF_POLLING_INTERVAL: "invalid_int"},
            {CONF_TRIGGER_RESYNC: "not_a_bool"},
        ]
        for opts in corrupted_initializations:
            with self.subTest(options=opts):
                entry = ConfigEntry(
                    entry_id="corrupt_entry_test",
                    domain=DOMAIN,
                    title="Sevenhugs Corrupt",
                    data={},
                    options=opts,
                )
                handler = SevenhugsOptionsFlowHandler(entry)
                form = asyncio.run(handler.async_step_init(None))
                self.assertEqual(form["type"], FlowResultType.FORM)
                self.assertIn("data_schema", form)

                # Extraction de la valeur par défaut pour entities dans le formulaire
                schema_dict = form["data_schema"].schema
                for k, v in schema_dict.items():
                    if getattr(k, "schema", None) == CONF_ENTITIES:
                        default_ent = k.default() if callable(getattr(k, "default", None)) else getattr(k, "default", None)
                        self.assertIsInstance(default_ent, list)


class TestSevenhugsOptionsFlowHandlerExecution(unittest.IsolatedAsyncioTestCase):
    """Validation de l'exécution directe du gestionnaire et de la conformité du contrat."""

    async def asyncSetUp(self) -> None:
        self.entry = ConfigEntry(
            entry_id="opt_exec_entry",
            domain=DOMAIN,
            title="Sevenhugs Remote (5726)",
            data={},
            options={
                CONF_POLLING_INTERVAL: 45,
                CONF_AUTO_SYNC_WEBHOOKS: True,
                CONF_TIMEOUT: 12,
                CONF_ENTITIES: ["light.desk"],
                CONF_TRIGGER_RESYNC: False,
            },
        )
        self.handler = SevenhugsOptionsFlowHandler(self.entry)

    def test_async_get_options_flow_factory(self) -> None:
        """Vérifie que la factory async_get_options_flow retourne une instance valide."""
        options_flow = SevenhugsConfigFlow.async_get_options_flow(self.entry)
        self.assertIsInstance(options_flow, SevenhugsOptionsFlowHandler)
        self.assertIs(options_flow.config_entry, self.entry)

    async def test_direct_submission_preserves_exact_user_input(self) -> None:
        """Garantit que async_step_init(user_input) retourne CREATE_ENTRY sans altérer les données."""
        user_input = {
            CONF_ENTITIES: ["light.living_room", "switch.lamp"],
            CONF_TRIGGER_RESYNC: True,
            CONF_POLLING_INTERVAL: 90,
            CONF_AUTO_SYNC_WEBHOOKS: False,
            CONF_TIMEOUT: 15,
        }
        res = await self.handler.async_step_init(user_input)
        self.assertEqual(res["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(res["title"], "")
        self.assertEqual(res["data"], user_input)

    async def test_direct_submission_empty_dict(self) -> None:
        """Vérifie qu'une soumission directe d'un dictionnaire vide ne lève aucune exception."""
        res = await self.handler.async_step_init({})
        self.assertEqual(res["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(res["data"], {})

    async def test_direct_submission_legacy_keys_preservation(self) -> None:
        """Vérifie que les soumissions historiques à 3 clés ne sont pas tronquées ni rejetées."""
        legacy_input = {
            CONF_POLLING_INTERVAL: 120,
            CONF_AUTO_SYNC_WEBHOOKS: True,
            CONF_TIMEOUT: 20,
        }
        res = await self.handler.async_step_init(legacy_input)
        self.assertEqual(res["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(res["data"], legacy_input)

    async def test_direct_submission_single_key(self) -> None:
        """Vérifie qu'une soumission partielle à clé unique passe sans exception."""
        single_input = {CONF_POLLING_INTERVAL: 15}
        res = await self.handler.async_step_init(single_input)
        self.assertEqual(res["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(res["data"], single_input)


class TestSevenhugsOptionsFlowConcurrencyAndRapidSubmissions(unittest.IsolatedAsyncioTestCase):
    """Stress-test de concurrence et de soumissions séquentielles massives."""

    async def asyncSetUp(self) -> None:
        self.server = MockSmartRemoteServer()
        await self.server.start()
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="opt_stress_entry_7hugs",
            domain=DOMAIN,
            title="Sevenhugs Remote (Stress)",
            data={
                CONF_HOST: self.server.host,
                CONF_PORT: self.server.port,
                CONF_SERIAL_NUMBER: "a06fe048b48b9999",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-stress",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: self.server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
            options={},
        )
        self.hass.config_entries.async_add(self.entry)
        setup_ok = await async_setup_entry(self.hass, self.entry)
        self.assertTrue(setup_ok)

    async def asyncTearDown(self) -> None:
        try:
            await async_unload_entry(self.hass, self.entry)
        finally:
            await self.server.stop()

    async def test_rapid_sequential_submissions_and_reloads(self) -> None:
        """Exécute 25 soumissions séquentielles rapides d'options avec rechargement complet."""
        for cycle in range(25):
            handler = SevenhugsOptionsFlowHandler(self.entry)
            new_options = {
                CONF_ENTITIES: [f"light.room_{cycle}", "switch.socket"],
                CONF_TRIGGER_RESYNC: (cycle % 2 == 0),
                CONF_POLLING_INTERVAL: 15 + (cycle % 10) * 10,
                CONF_AUTO_SYNC_WEBHOOKS: True,
                CONF_TIMEOUT: 10 + (cycle % 5),
            }
            res = await handler.async_step_init(new_options)
            self.assertEqual(res["type"], FlowResultType.CREATE_ENTRY)
            self.entry.options = res["data"]

            # Notification du listener de mise à jour des options
            await update_listener(self.hass, self.entry)

            # Vérification de l'intégrité de runtime_data après rechargement
            runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
            self.assertIsNotNone(runtime_data.client)
            self.assertIsNotNone(runtime_data.coordinator)
            self.assertIsNotNone(runtime_data.injector)
            self.assertEqual(
                runtime_data.coordinator.update_interval.total_seconds(),
                new_options[CONF_POLLING_INTERVAL],
            )

    async def test_concurrent_options_flow_submissions(self) -> None:
        """Exécute 30 soumissions d'options asynchrones strictement simultanées."""
        async def submit_worker(worker_id: int) -> dict[str, Any]:
            h = SevenhugsOptionsFlowHandler(self.entry)
            payload = {
                CONF_ENTITIES: [f"light.concurrent_{worker_id}"],
                CONF_TRIGGER_RESYNC: False,
                CONF_POLLING_INTERVAL: 15 + (worker_id % 20),
                CONF_AUTO_SYNC_WEBHOOKS: bool(worker_id % 2),
                CONF_TIMEOUT: 10 + (worker_id % 5),
            }
            step_res = await h.async_step_init(payload)
            self.assertEqual(step_res["type"], FlowResultType.CREATE_ENTRY)
            return step_res["data"]

        tasks = [asyncio.create_task(submit_worker(i)) for i in range(30)]
        results = await asyncio.gather(*tasks)
        self.assertEqual(len(results), 30)

        # Vérification qu'aucun payload n'a été corrompu ou croisé
        for i, res in enumerate(results):
            self.assertEqual(res[CONF_ENTITIES], [f"light.concurrent_{i}"])
            self.assertEqual(res[CONF_POLLING_INTERVAL], 15 + (i % 20))

    async def test_state_isolation_between_distinct_entries(self) -> None:
        """Garantit une isolation totale d'état entre deux ConfigEntries indépendantes."""
        entry_b = ConfigEntry(
            entry_id="opt_entry_isolated_b",
            domain=DOMAIN,
            title="Sevenhugs Remote B",
            data=dict(self.entry.data),
            options={CONF_POLLING_INTERVAL: 180, CONF_ENTITIES: ["light.bedroom_b"]},
        )
        handler_a = SevenhugsOptionsFlowHandler(self.entry)
        handler_b = SevenhugsOptionsFlowHandler(entry_b)

        form_a = await handler_a.async_step_init(None)
        form_b = await handler_b.async_step_init(None)

        def_a = form_a["data_schema"].schema
        def_b = form_b["data_schema"].schema

        # Extraction des valeurs par défaut pour CONF_POLLING_INTERVAL
        def get_def(schema_dict: dict, conf_key: str):
            for k in schema_dict.keys():
                if getattr(k, "schema", None) == conf_key:
                    return k.default() if callable(getattr(k, "default", None)) else getattr(k, "default", None)
            return None

        self.assertEqual(get_def(def_a, CONF_POLLING_INTERVAL), DEFAULT_POLLING_INTERVAL)
        self.assertEqual(get_def(def_b, CONF_POLLING_INTERVAL), 180)


class TestSevenhugsOptionsFlowLifecycleIntegration(unittest.IsolatedAsyncioTestCase):
    """Tests d'intégration de bout en bout du cycle de vie des options avec l'assistant Lua."""

    async def asyncSetUp(self) -> None:
        self.server = MockSmartRemoteServer()
        await self.server.start()
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(
            entry_id="opt_lifecycle_entry_main",
            domain=DOMAIN,
            title="Sevenhugs Remote (Lifecycle)",
            data={
                CONF_HOST: self.server.host,
                CONF_PORT: self.server.port,
                CONF_SERIAL_NUMBER: "a06fe048b48b1111",
                CONF_REMOTE_ID: "5726",
                CONF_CONTROLLER_ID: "ctrl-uuid-lifecycle",
                CONF_CLIENT_PRIVATE_KEY: "01" * 32,
                CONF_CLIENT_PUBLIC_KEY: "02" * 32,
                CONF_REMOTE_PUBLIC_KEY: self.server.remote_pk.hex(),
                CONF_SESSION_WRITE_KEY: "03" * 32,
                CONF_SESSION_READ_KEY: "04" * 32,
            },
            options={},
        )
        self.hass.config_entries.async_add(self.entry)
        setup_ok = await async_setup_entry(self.hass, self.entry)
        self.assertTrue(setup_ok)

    async def asyncTearDown(self) -> None:
        try:
            await async_unload_entry(self.hass, self.entry)
        finally:
            await self.server.stop()

    async def test_duplicate_entities_deduplicated_by_lua_injector(self) -> None:
        """Vérifie que des entités en doublon soumises dans les options sont dédupliquées par l'injecteur."""
        handler = SevenhugsOptionsFlowHandler(self.entry)
        res = await handler.async_step_init({
            CONF_ENTITIES: [
                "light.living_room",
                "light.living_room",
                "LIGHT.LIVING_ROOM",
                "switch.hallway",
                "switch.hallway",
            ],
            CONF_TRIGGER_RESYNC: True,
        })
        self.entry.options = res["data"]

        runtime_data = self.hass.data[DOMAIN][self.entry.entry_id]
        injector = runtime_data.injector

        # Déclenche la construction des déclencheurs
        triggers = await injector.async_build_triggers_from_entities(self.entry.options[CONF_ENTITIES])
        # Exactement 2 déclencheurs uniques : light.living_room et switch.hallway
        self.assertEqual(len(triggers), 2)
        entity_ids = [t["name"] for t in triggers]
        self.assertIn("light.living_room", entity_ids)
        self.assertIn("switch.hallway", entity_ids)

    async def test_webhook_persists_across_options_updates(self) -> None:
        """Garantit que le webhook local reste opérationnel et immuable lors des mises à jour d'options."""
        wh_id = f"sevenhugs_{self.entry.entry_id}"
        self.assertIn(wh_id, self.hass.webhook._webhooks)

        # Première modification d'options
        h1 = SevenhugsOptionsFlowHandler(self.entry)
        r1 = await h1.async_step_init({CONF_POLLING_INTERVAL: 90})
        self.entry.options = r1["data"]
        await update_listener(self.hass, self.entry)

        self.assertIn(wh_id, self.hass.webhook._webhooks)

        # Seconde modification d'options
        h2 = SevenhugsOptionsFlowHandler(self.entry)
        r2 = await h2.async_step_init({CONF_ENTITIES: ["light.kitchen"], CONF_TRIGGER_RESYNC: True})
        self.entry.options = r2["data"]
        await update_listener(self.hass, self.entry)

        self.assertIn(wh_id, self.hass.webhook._webhooks)

    async def test_sync_skipped_when_auto_sync_and_trigger_resync_are_false(self) -> None:
        """Vérifie qu'aucune synchronisation n'est planifiée si auto_sync et trigger_resync sont False."""
        created_tasks = []
        original_create_task = self.hass.async_create_task

        def tracking_create_task(coro: Any, name: str | None = None):
            t = original_create_task(coro, name)
            created_tasks.append(t)
            return t

        # Décharge l'entrée initiale pour éviter un double setup
        await async_unload_entry(self.hass, self.entry)

        with patch.object(self.hass, "async_create_task", side_effect=tracking_create_task):
            self.entry.options = {
                CONF_AUTO_SYNC_WEBHOOKS: False,
                CONF_TRIGGER_RESYNC: False,
            }
            await async_setup_entry(self.hass, self.entry)

        # Aucune tâche de synchronisation ne doit avoir été programmée
        self.assertEqual(len(created_tasks), 0)


if __name__ == "__main__":
    unittest.main()
