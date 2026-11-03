# Sevenhugs Smart Remote Rebirth (`SR1A`)

Outil local de jumelage, de sauvegarde, de configuration et d'intégration **Home Assistant** pour la télécommande universelle **Sevenhugs Smart Remote** (modèle `SR1A`, FCC ID `2AEVC-SR1A`).

Depuis la fermeture définitive des serveurs cloud Sevenhugs (`*.sevenhugs.com` et `cdn.prod.cobra.sevenhugs.com`) en mai 2022, l'application mobile officielle affiche un écran blanc lorsqu'on tente d'ajouter ou de configurer des intégrations Wi-Fi ou des Webhooks.

Ce projet est le fruit d'une rétro-ingénierie complète du protocole cryptographique de la télécommande et de son moteur Lua embarqué (`/rom/webapp/resources/setup.lua`). Il permet de **reprendre le contrôle à 100 % en local**, sans aucun serveur externe ni modification matérielle.

---

## Fonctionnalités principales

- **Jumelage cryptographique local (`Pair-Setup` & `Pair-Verify`)** : Implémentation complète du protocole HAP modifié par Sevenhugs (`SRP-6a 3072-bit`, `X25519`, `Ed25519`, `HKDF-SHA512`, `ChaCha20-Poly1305`).
- **Client API REST chiffré (`/api/*`)** : Lecture et modification de toute la configuration de la télécommande (appareils, pièces/groupes, scènes, ordre du carrousel, dispositions d'écrans et boutons personnalisés).
- **Remplacement local du CDN disparu (`web-wizard`)** : Serveur Web local (`http://localhost:8080`) qui remplace le frontend disparu `cdn.prod.cobra.sevenhugs.com/webapp/v2/static/js/app.js` et affiche directement les assistants Lua embarqués dans la ROM de la télécommande (`Webhooks`, `Philips Hue`, `Kodi`, `Dummy devices`, etc.).
- **Intégration directe avec Home Assistant** :
  1. **Via le plugin natif `sevenhugs.control.webhooks`** : Création en une ligne de commande (ou via l'interface Web locale) de boutons qui envoient des requêtes HTTP (`POST`, `GET`, `PUT`, `DELETE`) en Wi-Fi directement vers `http://<IP_HOME_ASSISTANT>:8123/api/webhook/<ID>`.
  2. **Via `emulated_hue`** : Contrôle direct des entités Home Assistant (lumières, prises, volets) via le plugin local Philips Hue (`sevenhugs.control.hue`).

---

## Structure du projet

```text
smart-remote-rebirth/
├── README.md                       # Ce guide d'utilisation et d'intégration
├── requirements.txt                # Dépendance Python (cryptography>=41.0.0)
├── smartremote_keys.example.json   # Modèle du fichier de clés généré au jumelage
├── sevenhugs_bridge.py             # Outil CLI unifié + Studio Web local (web-wizard)
├── pair_and_verify.py              # Script autonome de jumelage initial (Pair-Setup + Pair-Verify)
├── pair_verify.py                  # Script autonome de reconnexion (Pair-Verify) et dump rapide
└── docs/
    └── PROTOCOL_AND_API.md         # Documentation technique complète (Crypto, API REST, Moteur Lua)
```

---

## Installation

1. Clonez ce dépôt et créez un environnement virtuel Python 3 :
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
   *(Seule la bibliothèque `cryptography` est requise ; tout le reste utilise la bibliothèque standard Python).*

---

## Guide d'utilisation pas-à-pas

### 1. Trouver l'adresse IP de la Smart Remote
La télécommande annonce sa présence sur le réseau local via mDNS sous le service `_smart_remote._tcp.local.` (port TCP `80`).
```bash
avahi-browse -rt _smart_remote._tcp
```

### 2. Jumelage initial (`pair`) — *Une seule fois*
Si vous n'avez pas encore de fichier `smartremote_keys.json`, lancez le jumelage initial :
```bash
python3 sevenhugs_bridge.py --ip 192.168.1.XX pair
```
1. Le script envoie la requête `M1` à `/pair-setup`.
2. **Patientez environ 16 secondes** pendant que le processeur de la télécommande calcule sa clé publique SRP-6a 3072 bits.
3. Un code de sécurité à 8 chiffres (`XXX-XX-XXX`) s'affiche sur l'écran de la Smart Remote : saisissez-le dans le terminal.
4. Le script termine `Pair-Setup` (`M3` à `M6`), enchaîne immédiatement `Pair-Verify` (`M1` à `M4`), et sauvegarde vos clés permanentes Ed25519 dans `smartremote_keys.json`.

> [!TIP]
> Une fois `smartremote_keys.json` généré, **vous n'aurez plus jamais besoin de saisir de code PIN**. Toutes les commandes suivantes établissent une session chiffrée `Pair-Verify` automatiquement en moins d'une seconde.

---

### 3. Inspecter et sauvegarder la télécommande (`status` & `backup`)

- **Afficher l'inventaire complet** (modèle, firmware, appareils configurés, groupes, ordre du carrousel) :
  ```bash
  python3 sevenhugs_bridge.py status
  ```

- **Sauvegarder toute la configuration déchiffrée** (tous les endpoints `/api/*` + toutes les interfaces personnalisées et listes de commandes IR/Wi-Fi) :
  ```bash
  python3 sevenhugs_bridge.py backup ma_sauvegarde.json
  ```

- **Inspecter les écrans/boutons et les commandes d'un appareil** :
  ```bash
  python3 sevenhugs_bridge.py custom-ui sevenhugs.control.infrared:SOLO5_M3708_fff1980f
  ```

---

### 4. Lancer le Studio Web local (`web-wizard`)

L'application officielle Sevenhugs échoue lors de l'ajout ou de l'édition d'un service Wi-Fi car elle redirige vers `/api/devices/<package_id>/setup`, qui tente de charger un script Vue.js hébergé sur `https://cdn.prod.cobra.sevenhugs.com/webapp/v2/static/js/app.js` (hors ligne).

Or, **le moteur backend (`/rom/webapp/resources/setup.lua`) tourne toujours localement sur la télécommande !**

Pour utiliser graphiquement tous les assistants embarqués de la télécommande :
```bash
python3 sevenhugs_bridge.py web-wizard --port 8080
```
Ouvrez ensuite **`http://localhost:8080`** dans votre navigateur :
- Choisissez un plugin dans la colonne de gauche (`Webhooks`, `Dummy Light`, `Dummy TV`, `Kodi`, `Philips Hue`, etc.).
- Naviguez dans l'assistant Lua de la télécommande, ajoutez vos déclencheurs HTTP ou appareils, puis cliquez sur **Next / Done** pour enregistrer l'appareil sur la télécommande.

---

### 5. Intégrer la Smart Remote avec Home Assistant

#### Méthode A : Boutons Webhooks HTTP directs (Recommandé)
Le plugin embarqué `sevenhugs.control.webhooks` permet à la télécommande d'envoyer directement des requêtes HTTP (`POST`, `GET`, `PUT`, `DELETE`) sur votre réseau local Wi-Fi, sans passer par le smartphone ni par Internet.

1. Dans **Home Assistant**, créez une automatisation déclenchée par un **Webhook** (ex. ID `sr_salon_toggle`, méthode `POST`, option *"Uniquement accessible depuis le réseau local"* activée).
2. Créez l'appareil Webhooks et ses déclencheurs sur la Smart Remote en une seule commande :
   ```bash
   python3 sevenhugs_bridge.py webhooks-create \
     --trigger 'Lumière Salon|POST|http://192.168.1.100:8123/api/webhook/sr_salon_toggle|{"entity":"light.salon"}' \
     --trigger 'Mode Cinéma|POST|http://192.168.1.100:8123/api/webhook/sr_cinema|{"scene":"cinema"}' \
     --trigger 'Volets Salon|POST|http://192.168.1.100:8123/api/webhook/sr_volets|{"action":"toggle"}'
   ```
   *(Vous pouvez également les ajouter ou les modifier graphiquement via `python3 sevenhugs_bridge.py web-wizard`).*

#### Méthode B : Emulated Hue (Contrôle d'intensité et d'état)
La télécommande possède le plugin local `sevenhugs.control.hue`. En activant l'intégration [`emulated_hue`](https://www.home-assistant.io/integrations/emulated_hue/) dans Home Assistant (sur le port `80`), Home Assistant simule un pont Philips Hue local et expose vos entités (`light`, `switch`, `script`, `cover`) directement à la télécommande.

Vous pouvez lancer la découverte réseau locale de la télécommande avec :
```bash
python3 sevenhugs_bridge.py discover
```

---

## Documentation technique du protocole

Tous les détails techniques découverts lors de la rétro-ingénierie (paramètres cryptographiques HAP, subtilités de `Pair-Verify`, chiffrement `ChaCha20-Poly1305` avec nonce `HuMiThFr`, table complète des 31 routes REST `/api/*`, schémas JSON et fonctionnement du moteur Lua `/rom/webapp/resources/setup.lua`) sont documentés dans :

👉 **[`docs/PROTOCOL_AND_API.md`](file:///web/smart-remote-rebirth/docs/PROTOCOL_AND_API.md)**
