# Spécification Technique : Protocole & API de la Sevenhugs Smart Remote (`SR1A`)

Ce document rassemble l'ensemble des découvertes issues de la rétro-ingénierie réseau et logicielle de la **Sevenhugs Smart Remote** (modèle `SR1A`, firmware `0.76.4-19.6.3-ae`) et de l'application Android officielle (`com.sevenhugs.smartremote` v1.13.3).

---

## 1. Découverte réseau mDNS (`Bonjour / Zeroconf`)

La télécommande annonce un service HTTP sur le port TCP `80` sous le type `_smart_remote._tcp.local.` :

| Clé TXT | Exemple de valeur | Signification |
| :--- | :--- | :--- |
| `md` | `SR1A` | Modèle matériel (`Smart Remote 1A`) |
| `id` | `5726` | Identifiant court de la télécommande (utilisé dans `Pair-Setup` M6 et `Pair-Verify` M2) |
| `sn` | `a06fe048b48b260139` | Numéro de série matériel |
| `fw` | `0.76.4-19.6.3-ae` | Version du firmware embarqué |
| `pv` | `1.0` | Version du protocole de jumelage |
| `sf` | `0` ou `1` | Status Flags (`1` = non jumelée, `0` = déjà jumelée) |
| `c#` | `1` | Compteur de configuration |
| `s#` | `1` | Numéro d'état |
| `ci` | `1` | Identifiant de catégorie d'accessoire |

---

## 2. Authentification et Cryptographie (`HAP` modifié)

La Smart Remote utilise une variante du **HomeKit Accessory Protocol (HAP)** sur HTTP standard (`port 80`), avec encodage **TLV8** (`Content-Type: application/pairing+tlv8`).

### 2.1. En-têtes HTTP obligatoires (`HeaderInterceptor`)
L'analyse de la classe `com.sevenhugs.smartremote.business.api.HeaderInterceptor` dans `classes2.dex` révèle que chaque requête vers la télécommande doit comporter :
```http
X-UUID: <controller_id_uuid>
X-Version: 31
X-Min-Version: 24
Connection: Keep-Alive
```
- `X-Version: 31` correspond à `api_version = 31` du firmware `0.76.4`.
- `X-UUID` doit contenir l'identifiant du contrôleur (`controller_id`) enregistré lors de l'étape `M5` de `Pair-Setup`.

### 2.2. `POST /pair-setup` (Jumelage initial avec code PIN)
1. **M1 (Client $\rightarrow$ Remote)** :
   - TLV : `Tag 0x00 (Method) = 0x01`, `Tag 0x06 (State) = 0x01`.
   - **Particularité matérielle** : Le processeur embarqué de la télécommande met **~16 secondes** à calculer l'exponentiation modulaire SRP-6a 3072 bits avant de répondre avec `M2` et d'afficher le code PIN (`XXX-XX-XXX`) à l'écran. Le timeout HTTP client sur `M1` doit donc être d'au moins 30 à 60 secondes.
   - Si une session `Pair-Setup` précédente a expiré, le premier appel `M1` peut renvoyer `HTTP 500` (qui réinitialise l'état interne) ; il suffit alors de renvoyer `M1`.
2. **M2 (Remote $\rightarrow$ Client)** :
   - TLV : `Tag 0x06 (State) = 0x02`, `Tag 0x02 (Salt)` (16 octets), `Tag 0x03 (Public Key B)` (384 octets).
3. **Calculs SRP-6a (RFC 5054, groupe 3072-bit, $g = 5$, `SHA-512`)** :
   - Identifiant SRP : `I = "Pair-Setup"`, mot de passe : `P = "<XXX-XX-XXX>"` (avec les tirets).
   - Multiplicateur : $k = \text{SHA512}(\text{pad}_{384}(N) \parallel \text{pad}_{384}(g))$.
   - Paramètre de brouillage : $u = \text{SHA512}(\text{pad}_{384}(A) \parallel \text{pad}_{384}(B))$.
   - Clé privée dérivée du PIN : $x = \text{SHA512}(s \parallel \text{SHA512}(I \parallel \text{":"} \parallel P))$.
   - Secret partagé : $S = (B - k \cdot g^x)^ {a + u \cdot x} \bmod N$, puis $K = \text{SHA512}(S)$.
4. **M3 / M4 (Vérification SRP)** :
   - Envoi de `Tag 0x06 = 0x03`, `Tag 0x03 = A`, `Tag 0x04 = M1_proof`.
   - Réception de `Tag 0x06 = 0x04`, `Tag 0x04 = M2_proof`.
5. **M5 / M6 (Échange des clés permanentes Ed25519)** :
   - Clé de chiffrement dérivée via `HKDF-SHA512` (`salt="Pair-Setup-Encrypt-Salt"`, `info="Pair-Setup-Encrypt-Info"`, 32 octets).
   - Clé de signature contrôleur via `HKDF-SHA512` (`salt="Pair-Setup-Controller-Sign-Salt"`, `info="Pair-Setup-Controller-Sign-Info"`, 32 octets).
   - Signature Ed25519 sur `controller_sign_key || controller_id || client_ed25519_pk`.
   - Sub-TLV chiffré avec `ChaCha20-Poly1305` et nonce `\x00\x00\x00\x00PS-Msg05` : `Tag 0x03 (PublicKey)`, `Tag 0x01 (Identifier)`, `Tag 0x0A (Signature)`.
   - Réponse `M6` déchiffrée avec nonce `\x00\x00\x00\x00PS-Msg06` : contient `remote_id` (`Tag 0x01`, ex: `"5726"`) et `remote_public_key` (`Tag 0x03`, 32 octets Ed25519).

### 2.3. `POST /pair-verify` (Ouverture de session sans code PIN)
> [!CAUTION]
> **Deux conditions critiques pour que `M3` ne échoue pas en `HTTP 400` (`{'0x06': '04', '0x07': '02'}`)** :
> 1. **Même socket TCP (`Keep-Alive`)** : Les requêtes `POST /pair-verify` (M1) et `POST /pair-verify` (M3) **doivent impérativement transiter sur la même connexion TCP**. Si le client ferme le socket après M2, la télécommande détruit la clé éphémère X25519 et rejette M3.
> 2. **En-tête `X-UUID`** : L'en-tête HTTP `X-UUID: <controller_id>` doit être présent dès les requêtes `/pair-verify`.

1. **M1 (Client $\rightarrow$ Remote)** :
   - TLV : `Tag 0x03 = client_eph_x25519_pk` (32 octets), `Tag 0x06 = 0x01`.
2. **M2 (Remote $\rightarrow$ Client)** :
   - TLV : `Tag 0x03 = remote_eph_x25519_pk` (32 octets), `Tag 0x05 = encrypted_m2`, `Tag 0x06 = 0x02`.
   - Secret partagé X25519 : `shared_secret = X25519(client_eph_sk, remote_eph_pk)`.
   - Clé `verify_key` = `HKDF-SHA512(shared_secret, salt="Pair-Verify-Encrypt-Salt", info="Pair-Verify-Encrypt-Info", length=32)`.
   - `encrypted_m2` est déchiffré via `ChaCha20-Poly1305(verify_key, nonce=b"\x00\x00\x00\x00PV-Msg02")`.
3. **M3 (Client $\rightarrow$ Remote)** :
   - Signature Ed25519 : `sig = Ed25519.sign(client_sk, client_eph_pk + controller_id.encode('utf-8') + remote_eph_pk)`.
   - Sub-TLV : `encode_tlv([(0x0A, sig), (0x01, controller_id)])`.
   - Chiffrement : `enc_m3 = ChaCha20-Poly1305(verify_key, nonce=b"\x00\x00\x00\x00PV-Msg03").encrypt(sub_m3)`.
   - TLV externe : `encode_tlv([(0x05, enc_m3), (0x06, 0x03)])`.
4. **M4 (Remote $\rightarrow$ Client)** :
   - Réponse `HTTP 200` avec TLV `Tag 0x06 = 0x04`.

### 2.4. Dérivation des clés de session et chiffrement de `/api/*` (`CryptoInterceptor`)
Une fois `M4` reçu sur `/pair-verify`, deux clés symétriques de 32 octets sont dérivées de `shared_secret` via `HKDF-SHA512` avec `salt = b"Control-Salt"` :
- **`session_write_key`** (Client $\rightarrow$ Remote) : `info = b"Control-Write-Encryption-Key"`
- **`session_read_key`** (Remote $\rightarrow$ Client) : `info = b"Control-Read-Encryption-Key"`

Chaque requête/réponse HTTP sur `/api/*` (à l'exception de `/api/devices/<pkg>/setup`) est chiffrée d'un seul bloc avec **`ChaCha20-Poly1305`** et un **nonce fixe de 12 octets** :
```python
API_NONCE = b"\x00\x00\x00\x00HuMiThFr"  # 4 octets nuls + "HuMiThFr"
```
Il n'y a **aucun** découpage par blocs de 1024 octets ni préfixe de longueur de 2 octets (contrairement au HAP d'Apple) : le corps HTTP contient directement `ciphertext || poly1305_tag_16_bytes`.

---

## 3. Cartographie complète de l'API REST chiffrée (`/api/*`)

Extraite des interfaces Retrofit `com.sevenhugs.smartremote.swagger.remote.*` et des adaptateurs Moshi `*JsonAdapter` :

| Interface APK | Méthode HTTP | Route | Corps / Réponse & Rôle |
| :--- | :--- | :--- | :--- |
| `SrHelloApi` | `GET` | `/api/hello` | Vérification de connectivité |
| `SrConfigApi` | `GET` | `/api/config` | Renvoie `HomeInfo` (`metadata`, `user`, `smartphone`, `location`, `settings`) |
| `SrConfigApi` | `POST` | `/api/config` | Initialise la configuration (`HomeInfo`) |
| `SrConfigApi` | `PATCH` | `/api/config` | Met à jour les infos du smartphone (`SmartphoneInfo`) |
| `SrConfigApi` | `PUT` | `/api/config` | Restaure une configuration (`ConfigInfo`: `config_id`, `config_key`, `config_name`) |
| `SrDeviceApi` | `GET` | `/api/devices` | Liste les appareils par catégorie (`List<DeviceSummary>`) |
| `SrDeviceApi` | `POST` | `/api/devices` | Démarre l'ajout d'un appareil (`Control7H` manifest) $\rightarrow$ renvoie `Redirect` (`{"url": "/api/devices/<pkg>/setup"}`) |
| `SrDeviceApi` | `PATCH` | `/api/devices` | Démarre l'édition d'un appareil (`DeviceInfo`) $\rightarrow$ renvoie `Redirect` (`{"url": "/api/devices/<pkg>/setup"}`) |
| `SrDeviceApi` | `PUT` | `/api/devices` | Met à jour / renomme un appareil (`DeviceInfo`) |
| `SrDeviceApi` | `DELETE` | `/api/devices` | Supprime un appareil (avec corps `DeviceInfo`) |
| `SrDiscoverApi` | `GET` | `/api/discover` | Lance la découverte LAN depuis la télécommande (SSDP/mDNS/UDP) $\rightarrow$ `List<Control7H>` avec champ `device` peuplé |
| `SrManifestApi` | `GET` | `/api/manifests` | Liste les 22 plugins embarqués dans la ROM (`List<Control7H>`) |
| `SrCustomUiApi` | `GET` | `/api/custom/widgets` | Liste tous les modèles de widgets disponibles (`List<WidgetLayout>`) |
| `SrCustomUiApi` | `GET` | `/api/custom/{package_id}:{device_id}` | Récupère les écrans et boutons d'un appareil (`CustomDevice`) |
| `SrCustomUiApi` | `PUT` | `/api/custom/{package_id}:{device_id}` | Met à jour les écrans et boutons d'un appareil (`CustomDevice`) |
| `SrCustomUiApi` | `GET` | `/api/custom/{package_id}:{device_id}/commands` | Liste toutes les commandes assignables d'un appareil (`DeviceCommands`) |
| `SrCustomUiApi` | `PUT` | `/api/custom/restore` | Restaure la disposition par défaut (`CustomDevice`) |
| `SrComponentsGroupApi` | `GET / POST / PUT / DELETE` | `/api/components-groups` | Gestion des groupes d'appareils (`ComponentsGroup`, ex: groupe d'ampoules) |
| `SrAvSceneApi` | `GET / POST / PUT / DELETE` | `/api/av-scenes` | Gestion des scènes Audio/Vidéo (`AvScene`) |
| `SrAvSceneApi` | `GET / PUT` | `/api/av-scenes/{id}/sequences/{seq_id}` | Gestion des macros/séquences d'actions (`Sequence`) |
| `SrAvSceneApi` | `PUT` | `/api/av-scenes/{id}/sequences/{seq_id}/restore` | Restaure une séquence par défaut |
| `SrHomeSceneApi` | `GET / POST / PUT / DELETE` | `/api/home-scenes` | Gestion des scènes Smart Home (`HomeScene`) |
| `SrHomeSceneApi` | `PUT` | `/api/home-scenes/sources/{source}` | Synchronise les scènes d'une source externe |
| `SrSettingsApi` | `GET / PATCH` | `/api/settings` | Paramètres généraux et ordre d'affichage du carrousel (`carousel_order`) |
| `SrRulesApi` | `GET` | `/api/settings/rules` | Règles et limites des séquences (`max_actions_nb=25`, `max_delay_per_action=30000`) |
| `SrPasscodeApi` | `GET / PUT` | `/api/settings/passcode` | Code de verrouillage parental de la télécommande (`Passcode`) |
| `SrOsmApi` | `POST` | `/api/osm` | Assistant d'apprentissage/test infrarouge (`Osm`: `category`, `brand`, `location`) |
| `SrEntertainmentCenterApi` | `POST` | `/api/entertainment-center` | Création groupée d'un centre multimédia (`EntertainmentCenter`) |
| `SrHomeKitApi` | `GET / POST / DELETE` | `/api/homekit/pairing` | Statut et jumelage Apple HomeKit (`HomeKitInfo`) |

---

## 4. Moteur d'assistants Lua embarqué (`/rom/webapp/resources/setup.lua`)

### 4.1. Pourquoi l'application officielle affiche un écran blanc
Lorsqu'on ajoute un service Wi-Fi ou le plugin `sevenhugs.control.webhooks` via `POST /api/devices`, la télécommande prépare une session Lua interne et renvoie :
```json
{"url": "/api/devices/sevenhugs.control.webhooks/setup"}
```
Lorsque le WebView de l'application officielle effectue `GET http://<IP>/api/devices/sevenhugs.control.webhooks/setup` (en HTTP **non chiffré**), la télécommande renvoie :
- Un en-tête `Set-Cookie: 7HUGS_SID=<id_session>`
- Une coquille HTML de 677 octets qui charge `https://cdn.prod.cobra.sevenhugs.com/webapp/v2/static/js/app.js`.

Le domaine `cdn.prod.cobra.sevenhugs.com` étant éteint, le WebView reste blanc. Mais le moteur backend Lua (`/rom/webapp/resources/setup.lua`) est **intégralement hébergé dans la mémoire ROM de la télécommande**.

### 4.2. Protocole Server-Driven UI (`POST /api/devices/<package_id>/setup`)
En envoyant des requêtes `POST` en JSON (en clair, avec l'en-tête `Cookie: 7HUGS_SID=<id_session>`) à `/api/devices/<package_id>/setup`, `setup.lua` exécute la machine à états du plugin et renvoie la description JSON de chaque écran :

1. **Initialisation (Étape 1)** :
   - Requête : `POST /api/devices/sevenhugs.control.webhooks/setup` avec corps `{}`
   - Réponse :
     ```json
     {
       "title": "Add Webhooks",
       "state": "connect",
       "components": [
         {"resource": "me/banner_setup.png", "type": "image"},
         {"type": "spacer"},
         {"text": "Connect other services with Smart Remote through web requests...", "align": "center", "type": "text"},
         {"main": "Connect", "onclick": "main_page", "main_color": "#4eabff", "type": "list_button"}
       ]
     }
     ```
2. **Page principale des déclencheurs (`state = "main_page"`)** :
   - Requête : `{"state": "main_page"}`
   - Réponse : Liste `"id": "triggers_id"` contenant `"Add a trigger"` (`"onclick": "add_choice"`) et un bouton de validation finale `"Next"` (`"onclick": "done"`).
3. **Choix du type de déclencheur (`state = "add_choice"`)** :
   - Requête : `{"state": "add_choice"}`
   - Réponse : Propose `IFTTT Webhook trigger` (`"onclick": "ifttt_authentication"`) ainsi que la liste `"id": "methods_id"` contenant 4 méthodes HTTP locales :
     - `"id": "delete"` (`HTTP DELETE trigger`, `"onclick": "advanced_add_edit"`)
     - `"id": "get"` (`HTTP GET trigger`, `"onclick": "advanced_add_edit"`)
     - `"id": "post"` (`HTTP POST trigger`, `"onclick": "advanced_add_edit"`)
     - `"id": "put"` (`HTTP PUT trigger`, `"onclick": "advanced_add_edit"`)
4. **Formulaire d'ajout d'une requête HTTP (`state = "advanced_add_edit"`)** :
   - Requête : Transition vers `"state": "advanced_add_edit"` avec sélection de la méthode (`post`, `get`, `put` ou `delete`).
   - Réponse : Affiche les champs `"type": "input_text"` :
     - `"id": "url"` (*Request URL*, obligatoire)
     - `"id": "body"` (*Request Body (Optional)*, présent pour `POST` et `PUT`)
     - `"id": "name"` (*Display Name*, obligatoire)
     - Bouton `"onclick": "post_advanced_add_edit"`
5. **Enregistrement du déclencheur (`state = "post_advanced_add_edit"`)** :
   - Ajoute le déclencheur à la liste `triggers_id` et ramène à `main_page`.
6. **Finalisation (`state = "done"`)** :
   - Crée et persiste l'appareil `sevenhugs.control.webhooks` et ses commandes sur la télécommande.

