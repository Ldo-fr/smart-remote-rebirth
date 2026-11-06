# Infrastructure et Méthodologie de Test — Sevenhugs Smart Remote (Phase 4)

Ce document décrit l'infrastructure, l'architecture multi-niveaux et la méthodologie de test end-to-end (E2E) pour l'intégration Home Assistant de la télécommande **Sevenhugs Smart Remote (SR1A)**.

---

## 1. Architecture Multi-Niveaux des Tests (Tiers 1 à 4)

La validation de la Phase 4 repose sur une stratégie de test boîte-opaque et d'intégration système organisée en quatre paliers complémentaires :

```
┌────────────────────────────────────────────────────────────────────────┐
│               Tier 4 : Real-World Workloads & Scenarios                │
│  - Multi-Room Smart Home Workload (Lights, Switches, Scenes, Media)    │
│  - Remote Wake-from-Sleep Event-Driven Telemetry Refresh               │
└───────────────────────────────────▲────────────────────────────────────┘
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│               Tier 3 : Cross-Feature Integration Flows                 │
│  - Bidirectional Loop: Options Flow ➔ Lua Wizard ➔ Button ➔ Webhook    │
│  - Concurrent Entry Reload & In-Flight Webhook Requests                │
│  - Reconfiguration & Reauthentication State Preservation               │
└───────────────────────────────────▲────────────────────────────────────┘
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│               Tier 2 : Boundary Conditions & Fault Injection           │
│  - Malformed HTTP Payloads & Strict Schema Validation (HTTP 400)       │
│  - Webhook Invalidation Post-Unload (HTTP 404) & Idempotent Unregister │
│  - Entity Scalability (0 entities, 100 entities, unsupported domains)   │
│  - Deep Sleep & Network Transports Failures Resilience                 │
└───────────────────────────────────▲────────────────────────────────────┘
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│               Tier 1 : Core Feature Integration Contracts              │
│  - Webhook Registration & Local Routing (sevenhugs_{entry_id})         │
│  - Inbound Payload Processing & HA Service Dispatch (async_call)       │
│  - System Event Bus Emission (sevenhugs_button_pressed)                │
│  - Server-Driven UI Lua Injector Wizard Automation (7HUGS_SID)         │
│  - Options Flow Entity Selection & Immediate Resync Trigger            │
└────────────────────────────────────────────────────────────────────────┘
```

### Tier 1 : Couverture Fonctionnelle Nominale (Core Feature Contracts)
- **Enregistrement et routage Webhook local** : Inscription conforme auprès de Home Assistant (`local_only=True`, méthodes HTTP `["POST", "PUT"]`, identifiant `sevenhugs_{entry_id}`).
- **Traitement des payloads entrants** : Routage transparent vers les services Home Assistant (`light.turn_on`, `switch.toggle`, `scene.turn_on`, services personnalisés) avec transmission des arguments.
- **Émission sur le bus d'événements** : Déclenchement de `sevenhugs_button_pressed` avec traçabilité complète (`entry_id`, `remote_id`, `serial_number`, `payload`).
- **Injecteur Server-Driven UI Lua** : Traduction des entités en triggers HTTP et pilotage de la machine à états de l'assistant Lua embarqué (`POST /api/devices`, négociation `7HUGS_SID`, `connect`, `main_page`, `add_choice`, `advanced_add_edit`, `post_advanced_add_edit`, `done`).
- **Flux d'Options** : Sélection graphique multi-entités (`CONF_ENTITIES`) et demande de re-synchronisation immédiate (`CONF_TRIGGER_RESYNC`).

### Tier 2 : Conditions aux Limites et Résilience (Boundary & Fault Injection)
- **Payloads HTTP malformés** : Rejet strict (HTTP 400 Bad Request) sur JSON invalide, types incorrects, absence de `domain` ou absence conjointe d'`action` et `service`.
- **Révocation et cycle de vie** : Rejet HTTP 404 Not Found dès le déchargement de l'entrée (`async_unload_entry`).
- **Idempotence du désenregistrement** : Tolérance aux appels répétés sans exception.
- **Limites d'injection Lua** : Gestion sans crash de 0 entités, montée en charge à 100 entités, et filtrage gracieux des domaines non supportés (ex. `climate`, `vacuum`, `camera`).
- **Tolérance aux cycles de veille du matériel** : Absorption des erreurs de transport (`SevenhugsConnectionError`, `asyncio.TimeoutError`, `SevenhugsAuthError`) sans bloquer Home Assistant et avec activation du drapeau `_pending_resync`.

### Tier 3 : Interactions Multi-Composants (Cross-Feature Integration)
- **Boucle bidirectionnelle complète** :
  1. L'utilisateur sélectionne des entités dans l'Options Flow.
  2. L'injecteur Lua synchronise les boutons vers la télécommande.
  3. L'utilisateur appuie sur un bouton de la télécommande (requête HTTP POST vers le webhook).
  4. Le webhook valide le payload, exécute le service Home Assistant, émet l'événement sur le bus.
  5. Le coordinateur est notifié du réveil (`async_handle_wake_up("webhook")`) et rafraîchit immédiatement l'état.
- **Rechargement d'entrée en cours de requêtes** : Traitement sans interblocage (deadlock) ni fuite de ressources lors d'appels simultanés au webhook et à `async_unload_entry` / `async_setup_entry`.
- **Reconfiguration et Réauthentification** : Maintien de l'URL du webhook et des listes d'entités sélectionnées lors d'une mise à jour de l'adresse IP (`async_step_reconfigure`) ou d'un renouvellement des clés (`async_step_reauth`).

### Tier 4 : Scénarios Réalistes & Charge Utilisateur (Real-World Workloads)
- **Scénario Maison Connectée Multi-Appareils** : Configuration conjointe d'éclairages salon, prises cuisine, scène cinéma et lecteur multimédia. Déclenchement séquentiel des boutons, vérification des appels de service correspondants et conservation de l'ordre d'émission sur le bus.
- **Réveil de veille événementiel (Wake-From-Sleep Workflow)** : Simulation de télécommande endormie (échecs temporaires de polling). Déclenchement d'un webhook bouton entraînant la réinitialisation instantanée du compteur d'erreurs du coordinateur et la mise à jour des capteurs diagnostiques (batterie, RSSI).

---

## 2. Infrastructure et Composants de Simulation

L'environnement de test fonctionne de manière 100 % autonome, sans dépendre d'une installation active de Home Assistant Core :

| Composant | Rôle dans le banc d'essais | Fichier source |
| :--- | :--- | :--- |
| `MockSmartRemoteServer` | Émulateur TCP Keep-Alive matériel (SRP-6a, Ed25519, X25519, ChaCha20-Poly1305, endpoints `/api/*` et assistant Server-Driven UI Lua `/api/devices/.../setup`) | `tests/conftest.py` |
| `MockWebhookManager` | Gestionnaire de webhooks simulant `homeassistant.components.webhook` (routage, validation de méthodes HTTP, URLs locales, rejet 404/405) | `tests/ha_mock.py` |
| `MockServices` | Registre de services Home Assistant capturant les appels `async_call` et leurs paramètres (`ServiceCall`) | `tests/ha_mock.py` |
| `MockBus` | Bus d'événements asynchrone stockant les événements `Event` et déclenchant les écouteurs enregistrés | `tests/ha_mock.py` |
| `MockStateMachine` | Gestionnaire d'états et d'attributs (`friendly_name`, états `on`/`off`) pour la résolution des noms de boutons | `tests/ha_mock.py` |
| `MockConfigEntriesManager` | Gestionnaire de cycle de vie des entrées (`async_setup_entry`, `async_unload_entry`, `async_reload`) | `tests/ha_mock.py` |

---

## 3. Dérivation des Sorties Attendues (Source Faisant Foi)

Chaque assertion des tests E2E s'appuie sur une source de spécification formelle :
1. **Format Webhook** : Conforme à `PROJECT.md § Interface Contracts` et `custom_components/sevenhugs_smart_remote/webhook.py` (`sevenhugs_{entry_id}`).
2. **Schéma Voluptuous** : Clés obligatoires (`domain`, `action` ou `service`), clés optionnelles (`entity_id`, `data`, `params`, `parameters`), codes de retour HTTP 200 / 400.
3. **Machine à états Lua** : Séquence extraite de la ROM physique (`/rom/webapp/resources/setup.lua`) et documentée dans `docs/PROTOCOL_AND_API.md § 4.2` (`connect` -> `main_page` -> `add_choice` -> `advanced_add_edit` -> `post_advanced_add_edit` -> `done`).
4. **Bus d'événements HA** : Déclaration formelle `EVENT_SEVENHUGS_BUTTON_PRESSED = "sevenhugs_button_pressed"` dans `const.py`.

---

## 4. Commandes d'Exécution

```bash
# Exécution de la suite E2E Phase 4 seule
python3 -m unittest tests/test_e2e_phase4.py

# Exécution de la suite complète du projet (toutes phases confondues)
python3 -m unittest discover tests
```
