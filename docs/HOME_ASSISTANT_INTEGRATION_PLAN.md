# Plan d'Architecture et de Mise en Œuvre : Intégration Home Assistant (HACS)
## Projet Smart Remote Rebirth — Sevenhugs Smart Remote (`SR1A`)

---

### Informations Générales du Document
- **Titre** : Spécification d'Architecture et Feuille de Route d'Implémentation de l'Intégration HACS Sevenhugs Smart Remote
- **Auteur** : Équipe d'Ingénierie Smart Remote Rebirth
- **Statut** : Document de Référence Normatif (Production-Ready)
- **Version cible du firmware** : `0.76.4-19.6.3-ae` (API Version `31`)
- **Modèle matériel cible** : Sevenhugs Smart Remote `SR1A` (FCC ID `2AEVC-SR1A`)
- **Plateforme cible** : Home Assistant Core $2024.1.0+$ via HACS (Home Assistant Community Store)

---

## Table des Matières

1. [Introduction & Contexte](#1-introduction--contexte)
   - 1.1. Historique et arrêt des services cloud Sevenhugs
   - 1.2. Objectif du projet Smart Remote Rebirth
   - 1.3. Contraintes matérielles et spécificités de la télécommande
2. [R1. Analyse Comparative Approfondie des Architectures d'Intégration](#2-r1-analyse-comparative-approfondie-des-architectures-dintégration)
   - 2.1. Description formelle des architectures candidates
     - 2.1.1. Option A : Intégration HACS pure (Composant Python natif asynchrone)
     - 2.1.2. Option B : Architecture hybride (Add-on Docker Daemon + Intégration HACS légère)
   - 2.2. Évaluation technique multicritère
     - 2.2.1. Faisabilité technique et modèle d'exécution (Asyncio & thread pooling)
     - 2.2.2. Topologie réseau et gestion des ports (mDNS, TCP 80, WebSockets, Webhooks)
     - 2.2.3. Complexité d'installation et expérience utilisateur
     - 2.2.4. Compatibilité avec les 4 modes de déploiement de Home Assistant
     - 2.2.5. Empreinte mémoire, CPU et cycle de vie
   - 2.3. Matrice d'évaluation comparative synthétique
   - 2.4. Recommandation formelle et analyse des arbitrages (Option A retenue)
3. [R2. Conception UX & Parcours Utilisateur Home Assistant](#3-r2-conception-ux--parcours-utilisateur-home-assistant)
   - 3.1. Flux d'appairage initial (Config Flow 100 % graphique)
     - 3.1.1. Découverte automatique mDNS / Zeroconf (`_smart_remote._tcp.local.`)
     - 3.1.2. Saisie manuelle d'adresse IP (mode de repli)
     - 3.1.3. Initialisation du jumelage SRP-6a et gestion du délai matériel de 16 secondes
     - 3.1.4. Dialogue de saisie du code PIN (`XXX-XX-XXX`) et validation cryptographique M1-M6
     - 3.1.5. Finalisation, ouverture de session `Pair-Verify` et inscription au Device Registry
     - 3.1.6. Matrice exhaustive des erreurs et remédiations visuelles
     - 3.1.7. Flux de reconfiguration d'hôte et d'IP (`async_step_reconfigure`)
     - 3.1.8. Flux de réauthentification et re-jumelage (`async_step_reauth` & `async_step_reauth_confirm`)
   - 3.2. Flux d'options et de maintenance (Options Flow)
     - 3.2.1. Sélection et filtrage des entités Home Assistant exposées
     - 3.2.2. Gestion du carrousel de la télécommande (`carousel_order`)
     - 3.2.3. Réglages des intervalles de polling, timeouts et mode d'économie d'énergie
     - 3.2.4. Sauvegarde et restauration de la configuration JSON
   - 3.3. Modèle de synchronisation et mapping fonctionnel des entités
     - 3.3.1. Domaine `light` (Lumières) : Modes Webhooks directs vs Pont Philips Hue émulé
     - 3.3.2. Domaine `switch` (Interrupteurs et prises connectées)
     - 3.3.3. Domaine `media_player` (Téléviseurs, barres de son, récepteurs AV) et grille 6x12
     - 3.3.4. Domaine `scene` (Scènes domotiques)
     - 3.3.5. Entités de télémétrie de la télécommande (`sensor`, `binary_sensor`, `button`)
     - 3.3.6. Payloads JSON normatifs et flux d'échange
4. [R3. Feuille de Route Technique & Spécifications d'Implémentation](#4-r3-feuille-de-route-technique--spécifications-dimplémentation)
   - 4.1. Arborescence cible du composant HACS (Conformité Home Assistant Quality Scale)
   - 4.2. Spécification détaillée des modules Python
     - 4.2.1. Module `crypto.py` : Primitives TLV8, SRP-6a 3072-bit, Ed25519/X25519, ChaCha20-Poly1305
     - 4.2.2. Module `client.py` (`AsyncSmartRemoteClient`) : Gestionnaire HTTP asynchrone Keep-Alive
     - 4.2.3. Module `coordinator.py` : `DataUpdateCoordinator`, gestion de veille et résilience
     - 4.2.4. Module `config_flow.py` : Gestionnaire d'étapes d'appairage et d'options
     - 4.2.5. Module `webhook.py` : Réception locale sécurisée et dispatch d'événements
     - 4.2.6. Plateformes d'entités (`sensor.py`, `binary_sensor.py`, `button.py`, `switch.py`)
   - 4.3. Gestion du cycle de vie matériel, mise en veille et socket Keep-Alive
     - 4.3.1. Comportement du Deep Sleep et déclencheurs de réveil (accéléromètre, dock)
     - 4.3.2. Stratégie de reconnexion paresseuse (Lazy Reconnect) et re-handshake transparent
     - 4.3.3. Politique de journalisation non-bloquante et sans pollution de logs
   - 4.4. Plan de développement jalonné (5 Phases séquentielles)
   - 4.5. Stratégie globale de validation et tests (Unitaires, Mocks HA, Intégration, E2E)
5. [Conclusion & Synthèse des Livrables](#5-conclusion--synthèse-des-livrables)

---

## 1. Introduction & Contexte

### 1.1. Historique et arrêt des services cloud Sevenhugs
Commercialisée initialement sous le nom de Sevenhugs Smart Remote (`SR1A`), cette télécommande domotique universelle haut de gamme se distinguait par son écran tactile capacitif AMOLED incurvé haute résolution, son capteur de pointage intérieur breveté UWB (Ultra-Wideband), ainsi que ses connectivités Wi-Fi 802.11 b/g/n, Bluetooth Low Energy (BLE) et émetteur infrarouge (IR).

En mai 2022, suite à des restructurations d'entreprise, les infrastructures de serveurs hébergées sur le domaine `*.sevenhugs.com` (notamment `cdn.prod.cobra.sevenhugs.com`, `auth.sevenhugs.com` et l'infrastructure de synchronisation cloud) ont été définitivement arrêtées. Par conséquent :
- L'application mobile officielle Android (`com.sevenhugs.smartremote`) et iOS est devenue inopérante lors de l'ajout de nouveaux appareils (affichage d'un écran blanc dans le WebView en raison de l'absence de téléchargement du fichier JavaScript distant `app.js`).
- Les télécommandes réinitialisées en usine ou n'ayant plus accès au cloud Sevenhugs se sont retrouvées privées d'interface de configuration officielle.

### 1.2. Objectif du projet Smart Remote Rebirth
Les investigations techniques poussées de rétro-ingénierie logicielle et réseau (consignées dans `docs/PROTOCOL_AND_API.md` et les rapports d'exploration du dépôt) ont démontré que **la télécommande Sevenhugs Smart Remote est un appareil réseau autonome et résilient** :
1. Son système embarqué Linux héberge un serveur HTTP local complet sur le port TCP `80`.
2. Le moteur d'assistants de configuration est entièrement codé en Lua (`/rom/webapp/resources/setup.lua`) et réside de manière permanente dans la mémoire flash morte (ROM) de la télécommande.
3. Le protocole de jumelage et de contrôle repose sur des primitives cryptographiques ouvertes et standards (variante HomeKit HAP : SRP-6a 3072 bits, Ed25519, X25519, ChaCha20-Poly1305).
4. Le plugin embarqué `sevenhugs.control.webhooks` permet à la télécommande d'émettre des requêtes HTTP directes vers n'importe quel serveur local.

L'objectif de ce plan de mise en œuvre est de concevoir l'architecture d'une **intégration native Home Assistant sous forme de composant personnalisé HACS (Home Assistant Community Store)**, offrant une expérience utilisateur sans friction : appairage 100 % graphique via le Config Flow de Home Assistant, détection automatique par mDNS, synchronisation bidirectionnelle des entités domotiques (lumières, prises, scènes, téléviseurs) et supervision de la télémétrie matérielle (batterie, Wi-Fi, socle de charge).

### 1.3. Contraintes matérielles et spécificités de la télécommande
Toute conception logicielle s'interfaçant avec la Smart Remote doit impérativement respecter quatre contraintes matérielles critiques :
1. **Délai de calcul matériel SRP-6a (~16 secondes)** :
   Le microcontrôleur basse consommation de la télécommande nécessite entre 15 et 18 secondes pour calculer son exponentiation modulaire SRP-6a 3072-bit lors de l'émission de la requête `M1` de `Pair-Setup`. Le client HTTP doit tolérer un timeout prolongé sans bloquer le thread principal.
2. **Maintien strict du socket TCP Keep-Alive lors de `Pair-Verify`** :
   La télécommande stocke la clé publique éphémère X25519 générée en `M2` uniquement dans le contexte de la connexion TCP active. Si la socket est fermée ou recyclée entre les requêtes `M1` et `M3`, la télécommande rejette `M3` avec une erreur `HTTP 400` (`Tag 0x06 = 0x04, Tag 0x07 = 0x02`).
3. **Mise en veille profonde (Deep Sleep)** :
   Afin de préserver sa batterie lorsqu'elle n'est pas posée sur sa station d'accueil magnétique, la télécommande coupe son écran et son interface réseau après 30 à 60 secondes d'inactivité. L'intégration doit adopter une politique de reconnexion transparente sans marquer abusivement les entités comme « Indisponibles ».
4. **Absence de serveur WebSocket embarqué** :
   La télécommande ne supporte aucun serveur ni client WebSocket natif. Les échanges reposent exclusivement sur des requêtes HTTP/1.1 chiffrées émises par Home Assistant vers la télécommande (port 80) et des requêtes HTTP Webhooks émises par la télécommande vers Home Assistant (port 8123).

---

## 2. R1. Analyse Comparative Approfondie des Architectures d'Intégration

L'intégration d'un matériel utilisant une cryptographie asymétrique/symétrique propriétaire et un serveur HTTP embarqué peut être envisagée selon deux paradigmes logiciels distincts au sein de l'écosystème Home Assistant :

```text
===================================================================================
OPTION A : INTÉGRATION HACS PURE (PYTHON NATIF DANS HOME ASSISTANT CORE)
===================================================================================

       +-------------------------------------------------------------+
       |               HOME ASSISTANT CORE RUNTIME                   |
       |                                                             |
       |  +-------------------------------------------------------+  |
       |  |     custom_components/sevenhugs_smart_remote          |  |
       |  |                                                       |  |
       |  |  +--------------------+    +-----------------------+  |  |
       |  |  |    Config Flow     |    | DataUpdateCoordinator |  |  |
       |  |  | (100% Graphique)   |    | (Polling / Health)    |  |  |
       |  |  +---------+----------+    +-----------+-----------+  |  |
       |  |            |                           |              |  |
       |  |            v                           v              |  |
       |  |  +-------------------------------------------------+  |  |
       |  |  |   AsyncSmartRemoteClient (aiohttp Keep-Alive)   |  |  |
       |  |  |   - Crypto Engine (SRP, Ed25519, ChaCha20)     |  |  |
       |  |  +-------------------------+-----------------------+  |  |
       |  +----------------------------|--------------------------+  |
       |                               |                             |
       |  HA Native Services           |                             |
       |  - zeroconf (UDP 5353)        |                             |
       |  - webhook (/api/webhook/sr*) |                             |
       +-------------------------------|-----------------------------+
                                       |
                     Requêtes HTTP/1.1 | Chiffrées (TCP 80)
                                       v
                     +-----------------------------------+
                     |   SEVENHUGS SMART REMOTE (SR1A)   |
                     |   - HTTP Server (Port 80)         |
                     |   - Lua Engine (setup.lua)        |
                     +-----------------------------------+

===================================================================================
OPTION B : ARCHITECTURE HYBRIDE (ADD-ON DOCKER DAEMON + HACS LÉGÈRE)
===================================================================================

       +------------------------------------+    +----------------------------------+
       |        HOME ASSISTANT CORE         |    |       ADD-ON HOME ASSISTANT      |
       |                                    |    |      (Conteneur Docker Dédié)    |
       |  +------------------------------+  |    |                                  |
       |  | custom_components/sevenhugs  |  |    |  +----------------------------+  |
       |  | (Composant HACS Léger)       |  |    |  |     Daemon Python Local    |  |
       |  |                              |  |    |  |  (sevenhugs_bridge.py / WS)|  |
       |  | - Client WebSocket local     |  |    |  +--------------+-------------+  |
       |  | - Entités HA et Dashboard    |  |    |                 |                |
       |  +--------------+---------------+  |    +-----------------|----------------+
       +-----------------|------------------+                      |
                         |                                         |
                         | Canal IPC WebSocket / REST (Port 8090)  |
                         +-----------------------------------------+
                                                                   |
                                                 Requêtes HTTP/1.1 | Chiffrées (TCP 80)
                                                                   v
                                                 +----------------------------------+
                                                 |   SEVENHUGS SMART REMOTE (SR1A)  |
                                                 +----------------------------------+
```

---

### 2.1. Description formelle des architectures candidates

#### 2.1.1. Option A : Intégration HACS pure (Composant Python natif asynchrone)
Dans cette architecture, l'ensemble de la logique applicative est encapsulé dans un unique composant personnalisé (`custom_component`) écrit en Python moderne ($3.11+$), installé dans le répertoire `custom_components/sevenhugs_smart_remote/` de Home Assistant :
- **Exécution directe dans l'Event Loop de Home Assistant Core** : Les requêtes HTTP asynchrones sont gérées par `aiohttp.ClientSession`.
- **Délégation CPU non-bloquante** : Les calculs cryptographiques lourds (SRP-6a 3072 bits) sont confiés au pool de threads worker via `hass.async_add_executor_job(...)`.
- **Réception d'événements native** : Les commandes transmises par la télécommande utilisent le gestionnaire de Webhooks natif de Home Assistant (`hass.components.webhook`), qui écoute sur le port existant 8123 de l'instance.
- **Découverte intégrée** : Utilisation du module natif `homeassistant.components.zeroconf` pour capter les paquets mDNS émis par la télécommande.

#### 2.1.2. Option B : Architecture hybride (Add-on Docker Daemon + Intégration HACS légère)
Dans ce schéma, la solution est scindée en deux briques logicielles distinctes :
1. **Un Add-on Home Assistant (Conteneur Docker Linux indépendant)** :
   Exécute un démon persistant dérivé de `sevenhugs_bridge.py`. Ce démon prend en charge les calculs cryptographiques, le maintien de la session `Pair-Verify` avec la télécommande, et expose un serveur local (REST ou WebSocket) sur un port dédié (par exemple TCP `8090`).
2. **Une intégration HACS légère** :
   Composant minimaliste s'exécutant dans Home Assistant Core, agissant comme un pont (bridge client) qui se connecte au démon local en WebSocket pour récupérer l'état des capteurs et relayer les commandes.

---

### 2.2. Évaluation technique multicritère

#### 2.2.1. Faisabilité technique et modèle d'exécution (Asyncio & thread pooling)
- **Option A** :
  - *Avantages* : La pile asynchrone de Python et d'`aiohttp` permet une gestion optimale des descripteurs de fichiers réseau. L'allocation d'une instance `aiohttp.TCPConnector(force_close=False)` garantit le maintien strict du socket TCP Keep-Alive requis par le protocole `Pair-Verify`. La délégation des calculs SRP au pool de threads worker (`hass.async_add_executor_job`) élimine tout risque de gel de l'Event Loop (lag de l'interface Home Assistant). Le timeout matériel de 16 secondes imposé par la télécommande est absorbé de façon totalement asynchrone sans monopoliser de thread système.
  - *Risques* : Une mauvaise gestion des exceptions d'`aiohttp` pourrait impacter la boucle d'événements. Cela est résolu par un typage strict et une encapsulation hermétique dans `client.py`.
- **Option B** :
  - *Inconvénients majeurs* : L'architecture introduit une double boucle d'événements asynchrone (l'une dans le démon Docker, l'autre dans Home Assistant Core). Cela multiplie les files d'attente, les risques de désynchronisation de l'état (out-of-sync) et ajoute un point de panne supplémentaire (panne du conteneur, blocage du serveur WebSocket interne).

#### 2.2.2. Topologie réseau et gestion des ports (mDNS, TCP 80, WebSockets, Webhooks)
- **Découverte mDNS / Zeroconf (Port UDP 5353)** :
  - *Option A* : Home Assistant Core gère déjà une instance globale `zeroconf` liée à l'interface réseau de la machine hôte. L'intégration n'a besoin que d'inscrire la correspondance `_smart_remote._tcp.local.` dans son fichier `manifest.json`. La détection d'une télécommande déclenche instantanément la découverte dans le tableau de bord.
  - *Option B* : Un conteneur Docker standard utilise par défaut un sous-réseau ponté (Docker bridge) qui bloque le multicast UDP mDNS. L'Add-on doit obligatoirement être configuré avec `host_network: true` dans son fichier `config.yaml`, ce qui compromet l'isolation du conteneur et peut entrer en conflit avec d'autres conteneurs hôtes.
- **Gestion des ports entrants et sortants** :
  - *Option A* : **Zéro port d'écoute ouvert**. Home Assistant agit en tant que client HTTP sortant vers la télécommande (port 80). Les actions de la télécommande sont reçues via le point d'écoute Webhook natif de Home Assistant sur le port `8123`. Aucun port additionnel n'est réservé, éliminant tout risque de conflit de ports.
  - *Option B* : L'Add-on doit ouvrir au minimum un port TCP entrant (ex: `8090` pour son API locale). Si le Studio Web d'assistance est inclus, un port supplémentaire (ex: `8080`) est requis. Dans les environnements dotés de proxies inverses ou d'autres add-ons (Zigbee2MQTT, Node-RED, UniFi), les collisions de ports sont fréquentes.
- **Absence de WebSockets natifs sur la télécommande** :
  - La télécommande n'utilisant aucun WebSocket, l'obligation pour l'Option B de concevoir et maintenir un serveur WebSocket intermédiaire entre l'Add-on et l'intégration HACS constitue une surcouche logicielle inutilement complexe et coûteuse en latence.

#### 2.2.3. Complexité d'installation et expérience utilisateur
- **Option A (Expérience en 1 étape)** :
  1. L'utilisateur installe le dépôt via HACS en un clic et redémarre Home Assistant.
  2. La télécommande apparaît automatiquement dans les notifications de découverte : *"Sevenhugs Smart Remote détectée"*.
  3. L'utilisateur clique sur **Configurer** et suit le Config Flow graphique (attente automatique de 16s, saisie du PIN).
- **Option B (Expérience en 4 étapes fastidieuses)** :
  1. Ajouter manuellement un dépôt personnalisé d'Add-ons dans le magasin Home Assistant.
  2. Installer l'Add-on, paramétrer sa configuration YAML, puis le démarrer.
  3. Installer le composant HACS distinct pour l'intégration.
  4. Configurer l'intégration HACS en renseignant l'adresse IP et le port du conteneur Add-on, avec gestion éventuelle d'un jeton d'authentification API.

#### 2.2.4. Compatibilité avec les 4 modes de déploiement de Home Assistant
Il s'agit d'un critère d'évaluation **strictement éliminatoire** :

| Mode d'installation Home Assistant | Description de l'environnement | Option A (HACS Pure) | Option B (Add-on + HACS) |
| :--- | :--- | :---: | :---: |
| **Home Assistant OS (HAOS)** | Système d'exploitation complet dédié (x86_64, Raspberry Pi, ODROID). | **Compatible** (100 %) | **Compatible** (100 %) |
| **Home Assistant Supervised** | Installation Debian avec moteur Superviseur Docker. | **Compatible** (100 %) | **Compatible** (100 %) |
| **Home Assistant Container** | Conteneur Docker standard autonome (`homeassistant/home-assistant`). | **Compatible** (100 %) | ❌ **INCOMPATIBLE** (Pas de Superviseur d'Add-ons) |
| **Home Assistant Core** | Environnement virtuel Python pur (`venv`) sur Linux/macOS/BSD. | **Compatible** (100 %) | ❌ **INCOMPATIBLE** (Pas de Superviseur d'Add-ons) |

> **Constat technique déterminant** : L'Option B exclut d'emblée l'ensemble des utilisateurs de *Home Assistant Container* et *Home Assistant Core* (qui représentent une part substantielle des utilisateurs experts et des installations sur NAS Synology, QNAP ou serveurs Docker Compose). L'Option A est la seule à garantir une universalité totale.

#### 2.2.5. Empreinte mémoire, CPU et cycle de vie
- **Option A** :
  - Mémoire vive (RAM) : ~8 à 12 Mo intégrés dans le processus Python existant de Home Assistant.
  - Dépendances logicielles : Utilise la bibliothèque `cryptography` ($>=41.0.0$) qui est **déjà compilée et intégrée dans le socle de base de Home Assistant** (utilisée pour TLS, SSH et HomeKit). Aucune compilation native lors de l'installation.
  - CPU : Quasiment nul au repos (0,01 %). Pique à ~15 % durant 30 ms lors de la vérification de preuve SRP-6a.
- **Option B** :
  - Mémoire vive : Consomme entre 80 et 150 Mo de RAM supplémentaires pour l'exécution d'un conteneur Linux Alpine/Debian séparé avec son propre interpréteur Python, sa pile réseau et ses bibliothèques dupliquées.
  - Charge système : Deux couches de surveillance de processus, de logs et de communication inter-processus (IPC).

---

### 2.3. Matrice d'évaluation comparative synthétique

| Critère d'évaluation | Option A : Intégration HACS pure | Option B : Add-on Docker + HACS | Arbitrage & Décision |
| :--- | :--- | :--- | :--- |
| **Universalité des déploiements** | **100 % (HA OS, Container, Supervised, Core)** | ~60 % (HA OS et Supervised uniquement) | **Option A (Décisif)** |
| **Parcours d'installation UX** | 1 clic HACS + Découverte mDNS automatique | 4 étapes : Dépôt Add-on, Setup Docker, HACS, Liaison | **Option A (Décisif)** |
| **Contraintes de ports réseau** | **Aucun port ouvert supplémentaire** (sortant TCP 80, webhooks 8123 existant) | Requiert 1 à 2 ports ouverts (ex: TCP 8090, 8080) | **Option A** |
| **Découverte mDNS / Zeroconf** | Native via le composant `zeroconf` de HA | Complexe : requiert `host_network: true` sur Docker | **Option A** |
| **Gestion du socket Keep-Alive** | Maîtrise directe via `aiohttp.TCPConnector` | Relais sur 2 segments réseau (HA $\leftrightarrow$ Add-on $\leftrightarrow$ Télécommande) | **Option A** |
| **Empreinte système** | Ultra-légère (~10 Mo RAM, 0 conteneur supplémentaire) | Lourde (~100 Mo RAM, image Docker dédiée ~150 Mo) | **Option A** |
| **Complexité de maintenance** | Une seule base de code Python unifiée et typée | Deux projets séparés (Add-on + Intégration) avec protocole IPC | **Option A** |
| **Gestion de veille du matériel** | Optimisme d'état et reconnexion paresseuse en Python natif | Double boucle de timeout et états intermédiaires incertains | **Option A** |

---

### 2.4. Recommandation formelle et analyse des arbitrages (Option A retenue)

#### Recommandation Définitive :
Il est formellement recommandé d'implémenter l'**Option A : Intégration HACS pure (composant personnalisé en Python au sein de Home Assistant Core)**.

#### Justification architecturale approfondie :
1. **Élimination de la friction utilisateur** : La configuration d'une télécommande doit être accessible au grand public. L'Option A permet une détection automatique sans aucune ligne de commande, ni fichier de configuration YAML, ni gestion de conteneur.
2. **Alignement avec les standards de Home Assistant** : L'Option A respecte scrupuleusement les règles du *Home Assistant Quality Scale* en exploitant les mécanismes éprouvés du noyau : `ConfigFlow`, `OptionsFlow`, `DataUpdateCoordinator`, `DeviceRegistry`, `EntityRegistry` et `Webhook`.
3. **Simplicité protocolaire** : La télécommande n'émet pas de flux binaire permanent et ne nécessite pas de traitement temps réel à la microseconde. L'orchestration asynchrone native d'`aiohttp` en Python répond parfaitement au cahier des charges protocolaire sans nécessiter l'usine à gaz d'un conteneur dédié.

---

## 3. R2. Conception UX & Parcours Utilisateur Home Assistant

La conception de l'expérience utilisateur vise le **zéro ligne de commande**, une transparence absolue sur les mécanismes matériels (notamment le délai cryptographique) et une intégration graphique homogène respectant les chartes visuelles de Home Assistant.

---

### 3.1. Flux d'appairage initial (Config Flow 100 % graphique)

Le flux de configuration (`ConfigFlow`) est articulé autour d'une machine à états asynchrone gérant la découverte, l'attente active du calcul de la clé publique de la télécommande, la saisie sécurisée du code PIN et l'établissement des clés permanentes :

```text
                                  [ DÉCOUVERTE DU MATÉRIEL ]
                                              |
                   +--------------------------+--------------------------+
                   |                                                     |
                   v                                                     v
      (Annonce mDNS _smart_remote)                           (Ajout manuel utilisateur)
        async_step_zeroconf                                       async_step_user
                   |                                                     |
                   v                                                     v
      [ Écran Confirmation Zeroconf ]                        [ Formulaire Saisie IP / Hôte ]
      - Affiche Nom, Modèle, Série                           - Test de connectivité port 80
      - Bouton [Démarrer l'appairage]                        - Bouton [Suivant]
                   |                                                     |
                   +--------------------------+--------------------------+
                                              |
                                              v
                                  async_step_pair_init
                               [ Écran d'Attente Interactive ]
                               - Envoi POST /pair-setup (M1)
                               - Compteur/Barre de progression (~16s)
                               - Message : "Regardez l'écran de votre télécommande"
                                              |
                                              | Réception M2 (Salt + Public Key B)
                                              v
                                   async_step_pair_pin
                                [ Formulaire Saisie du PIN ]
                                - Champ formaté: "XXX-XX-XXX"
                                - Validation du PIN & Calcul SRP (M3/M4)
                                - Signature Ed25519 & Clés permanentes (M5/M6)
                                - Vérification de session Pair-Verify (M1->M4)
                                              |
                        +---------------------+---------------------+
                        | (Erreur de code)                          | (Succès)
                        v                                           v
             [ Réaffichage Formulaire ]                   async_create_entry
             - Message "Code PIN invalide"             [ Écran Succès & Device Registry ]
             - Tentative restante                      - Création de l'entrée d'intégration
                                                       - Génération automatique des entités
```

#### 3.1.1. Découverte automatique mDNS / Zeroconf (`_smart_remote._tcp.local.`)
1. **Déclenchement automatique** : Dès que la télécommande est connectée au Wi-Fi local, elle diffuse le service `_smart_remote._tcp.local.`. Le composant `zeroconf` de Home Assistant intercepte l'annonce et extrait les attributs TXT :
   - `id` : Identifiant court (ex: `"5726"`).
   - `sn` : Numéro de série unique matériel (ex: `"a06fe048b48b260139"`).
   - `fw` : Version de firmware (ex: `"0.76.4-19.6.3-ae"`).
   - `sf` : Statut de jumelage (`1` = prête pour appairage, `0` = déjà appairée).
2. **Affichage dans l'UI** : Une tuile de découverte s'affiche dans l'écran *Paramètres $\rightarrow$ Appareils et Services* :
   > **Appareil découvert** : *Sevenhugs Smart Remote (5726)*
3. **Dialogue de confirmation (`async_step_zeroconf_confirm`)** :
   - *Titre* : « Associer votre Sevenhugs Smart Remote »
   - *Corps du texte* : « La télécommande Sevenhugs Smart Remote (Série : `a06fe048b48b260139`) a été détectée à l'adresse IP `192.168.1.50`. Souhaitez-vous démarrer la procédure d'appairage sécurisé ? »
   - *Boutons* : `[Annuler]` et `[Associer la télécommande]`.
4. **Mise à jour dynamique de l'adresse de connexion (`discovery-update-connection`)** :
   Si le numéro de série `sn` intercepté par Zeroconf correspond à une télécommande déjà enregistrée dans Home Assistant mais avec une adresse IP modifiée (ex: renouvellement de bail DHCP), le Config Flow met à jour l'entrée existante via `self._abort_if_unique_id_configured(updates={CONF_HOST: host})` / `hass.config_entries.async_update_entry(...)` et recharge automatiquement l'intégration sans nécessiter de ré-appairage.

#### 3.1.2. Saisie manuelle d'adresse IP (mode de repli)
Dans le cas où le réseau local bloque le trafic multicast mDNS (réseaux d'entreprise, VLAN séparés, isolation Wi-Fi AP), l'utilisateur peut initier l'appairage manuellement :
1. *Menu* : *Ajouter une intégration* $\rightarrow$ recherche de « Sevenhugs Smart Remote ».
2. **Formulaire (`async_step_user`)** :
   - *Champ 1* : Adresse IP ou nom de domaine local (ex. `192.168.1.50` ou `smartremote.local`).
   - *Validation asynchrone* : Test immédiat de connectivité HTTP GET `/api/hello` sur le port 80 avec timeout de 3 secondes.
   - En cas d'échec : Affichage de l'erreur `cannot_connect` avec conseil de vérification de l'alimentation de la télécommande.

#### 3.1.3. Initialisation du jumelage SRP-6a et gestion du délai matériel de 16 secondes
1. Dès validation de l'adresse IP, le Config Flow bascule vers `async_step_pair_init`.
2. **Explication pédagogique à l'écran** :
   - *Titre* : « Préparation du jumelage sécurisé »
   - *Texte* :
     > « Veuillez vous assurer que l'écran de votre télécommande est allumé.  
     > En cliquant sur **Démarrer**, Home Assistant va solliciter la télécommande.  
     > **Attention : Le microcontrôleur de la télécommande met environ 16 secondes à calculer son code de sécurité.**  
     > L'écran de la télécommande va afficher un code PIN à 8 chiffres (format `XXX-XX-XXX`). »
   - *Action* : Clic sur `[Démarrer le jumelage]`.
3. **Traitement d'arrière-plan non-bloquant** :
   - L'intégration émet la requête HTTP :
     ```http
     POST /pair-setup HTTP/1.1
     Host: 192.168.1.50:80
     Content-Type: application/pairing+tlv8
     Connection: Keep-Alive
     User-Agent: okhttp/3.12.0

     Tag 0x00 (Method) = 0x01
     Tag 0x06 (State)  = 0x01
     ```
   - Un timeout HTTP explicite de **60 secondes** est configuré sur cette requête.
   - Pendant ce temps, Home Assistant affiche une animation de chargement avec la mention : *"Calcul de la clé cryptographique sur la télécommande en cours... Veuillez patienter."*
   - Dès réception de la réponse `M2` (`Tag 0x02 = Salt`, `Tag 0x03 = Public Key B`), le Config Flow enregistre ces paramètres en mémoire de session et affiche automatiquement l'écran de saisie du PIN.

#### 3.1.4. Dialogue de saisie du code PIN (`XXX-XX-XXX`) et validation cryptographique M1-M6
1. **Formulaire de saisie (`async_step_pair_pin`)** :
   - *Titre* : « Saisir le code PIN de la télécommande »
   - *Description* : « Regardez l'écran tactile de votre Smart Remote. Entrez le code à 8 chiffres affiché : »
   - *Champ* : `pin_code` (Champ texte avec placeholder `123-45-678`).
2. **Normalisation tolérante de la saisie utilisateur** :
   Le contrôleur nettoie la chaîne saisie en supprimant les espaces et tirets éventuels. Si l'utilisateur saisit `83921470`, le système reformate automatiquement en `839-21-470`.
3. **Exécution des étapes cryptographiques (déléguées au pool executor)** :
   - **Calcul de la clé partagée SRP-6a** :
     - Modulo premier MODP 3072-bit (RFC 5054), $g = 5$.
     - $k = \text{SHA512}(\text{pad}_{384}(N) \parallel \text{pad}_{384}(g))$.
     - Génération de la clé privée éphémère client $a$ (128 octets aléatoires) et $A = g^a \bmod N$.
     - $u = \text{SHA512}(\text{pad}_{384}(A) \parallel \text{pad}_{384}(B))$.
     - $x = \text{SHA512}(s \parallel \text{SHA512}(\text{"Pair-Setup:"} \parallel \text{PIN}))$.
     - $S = (B - k \cdot g^x)^{a + u \cdot x} \bmod N$ et $K = \text{SHA512}(S)$.
     - Calcul de la preuve client $M_1 = \text{SHA512}((H(N) \oplus H(g)) \parallel H(I) \parallel s \parallel A \parallel B \parallel K)$.
   - **Envoi de M3** : `Tag 0x06 = 0x03`, `Tag 0x03 = A`, `Tag 0x04 = M1`.
   - **Réception et vérification de M4** : La télécommande renvoie $M_2$. Le client vérifie $M_2 == \text{SHA512}(A \parallel M_1 \parallel K)$. Si la comparaison échoue, le formulaire est réaffiché avec l'erreur `invalid_auth`.
   - **Échange de clés permanentes Ed25519 (M5 / M6)** :
     - Dérivation HKDF-SHA512 de `session_key` et `controller_sign_key`.
     - Génération d'une paire permanente Ed25519 pour Home Assistant (`client_sk`, `client_pk`) et d'un `controller_id` (UUIDv4).
     - Signature de `controller_sign_key || controller_id || client_pk`.
     - Chiffrement du sous-TLV avec ChaCha20-Poly1305 (nonce `\x00\x00\x00\x00PS-Msg05`) et envoi dans `M5`.
     - Déchiffrement de `M6` (nonce `\x00\x00\x00\x00PS-Msg06`) : Récupération de `remote_id` (ex: `"5726"`) et `remote_public_key` (32 octets Ed25519 de la télécommande).

#### 3.1.5. Finalisation, ouverture de session `Pair-Verify` et inscription au Device Registry
1. **Établissement immédiat de la première session `Pair-Verify`** :
   Sur la même session HTTP, le client exécute `POST /pair-verify` (M1 à M4) via X25519 éphémère et signature mutuelle Ed25519 pour dériver les clés de chiffrement symétrique d'API `session_write_key` et `session_read_key`.
2. **Collecte des attributs matériels finaux** :
   Appel chiffré ChaCha20-Poly1305 sur `GET /api/settings` pour extraire :
   - Adresse MAC : `38:D7:CA:00:16:5E`
   - Firmware réel : `v0.76.4`
   - Nom configuré : `Smart Remote`
3. **Création de la `ConfigEntry` sécurisée** :
   Les données sensibles sont stockées dans le stockage chiffré de Home Assistant (`.storage/core.config_entries`) :
   ```json
   {
     "entry_id": "7hugs_a06fe048b48b260139",
     "version": 1,
     "domain": "sevenhugs_smart_remote",
     "title": "Sevenhugs Smart Remote (5726)",
     "data": {
       "host": "192.168.1.50",
       "port": 80,
       "serial_number": "a06fe048b48b260139",
       "controller_id": "8d8fcf52-47aa-425b-8f1d-bb6fbc5594aa",
       "client_private_key": "<64_hex_chars>",
       "client_public_key": "<64_hex_chars>",
       "remote_id": "5726",
       "remote_public_key": "<64_hex_chars>"
     },
     "options": {
       "polling_interval": 30,
       "auto_sync_webhooks": true
     }
   }
   ```
4. **Inscription dans le Device Registry de Home Assistant** :
   ```python
   device_registry.async_get_or_create(
       config_entry_id=entry.entry_id,
       identifiers={(DOMAIN, entry.data["serial_number"])},
       connections={(dr.CONNECTION_NETWORK_MAC, mac_address)},
       manufacturer="Sevenhugs",
       model="Smart Remote 1A (SR1A)",
       name=f"Sevenhugs Smart Remote ({entry.data['remote_id']})",
       sw_version=firmware_version,
       hw_version="FCC ID: 2AEVC-SR1A",
   )
   ```

#### 3.1.6. Matrice exhaustive des erreurs et remédiations visuelles

| Code d'erreur (`strings.json`) | Étape de survenue | Condition technique | Message affiché à l'utilisateur | Action de remédiation préconisée |
| :--- | :--- | :--- | :--- | :--- |
| `cannot_connect` | `user` / `zeroconf` | Port TCP 80 fermé ou hôte injoignable (timeout socket de 3s). | « Impossible de communiquer avec la télécommande. Vérifiez son adresse IP et sa connexion Wi-Fi. » | Réveiller l'écran de la télécommande et vérifier le bail DHCP de la box. |
| `timeout_connect` | `pair_init` | Aucun retour de M2 après 60 secondes d'attente. | « La télécommande a mis trop de temps à répondre pour générer son code. » | Poser la télécommande sur son socle pour forcer son réveil et relancer. |
| `remote_busy` | `pair_init` | Erreur `HTTP 500` renvoyée sur `POST /pair-setup`. | « La télécommande réinitialise sa session de sécurité. » | L'intégration retente automatiquement après 5 secondes de temporisation. |
| `invalid_pin_format` | `pair_pin` | Longueur différente de 8 chiffres. | « Le format du code PIN est invalide. Huit chiffres sont requis (ex. 123-45-678). » | Corriger la saisie en inspectant l'écran tactile. |
| `invalid_auth` | `pair_pin` | Preuve SRP-6a M4 rejetée (code PIN erroné). | « Le code PIN saisi est incorrect. Veuillez vérifier les chiffres affichés sur la télécommande. » | Saisir le bon code sans quitter l'assistant. |
| `already_configured` | `user` / `zeroconf` | Le numéro de série matériel existe déjà dans les entrées de configuration. | « Cette télécommande Sevenhugs est déjà configurée dans Home Assistant. » | Modifier les options de l'intégration existante. |
| `unknown` | Toute étape | Exception logicielle non interceptée (ex: rupture SSL/TLS locale). | « Une erreur inattendue est survenue. Consultez les journaux d'erreurs de Home Assistant. » | Télécharger le rapport de diagnostic. |

#### 3.1.7. Flux de reconfiguration d'hôte et d'IP (`async_step_reconfigure`)
Conformément à la règle Gold `reconfiguration-flow` du Home Assistant Integration Quality Scale, la modification de l'adresse IP ou du nom d'hôte de la télécommande doit être gérée par un flux de reconfiguration dédié accessible via le menu contextuel de la carte d'intégration (trois points $\rightarrow$ Reconfigurer), et non via le flux d'options.

1. **Dialogue de saisie (`async_step_reconfigure`)** :
   - Présente un formulaire pré-rempli avec l'adresse IP actuellement configurée (`entry.data[CONF_HOST]`).
   - L'utilisateur renseigne la nouvelle adresse IP ou le nom d'hôte local.
2. **Validation réseau et vérification de sécurité** :
   - Vérification de l'accessibilité du port TCP 80 via `ping_hello()`.
   - Vérification de la validité de la session chiffrée avec les clés existantes via `async_pair_verify()`.
   - Contrôle du numéro de série matériel (`serial_number`) via `GET /api/settings` pour garantir que la nouvelle adresse pointe bien vers la même télécommande physique et non vers un autre appareil.
3. **Mise à jour atomique et rechargement** :
   - Mise à jour transparente de la configuration via `self.async_update_reload_and_abort(entry, data_updates={CONF_HOST: new_host})`.

#### 3.1.8. Flux de réauthentification et re-jumelage (`async_step_reauth` & `async_step_reauth_confirm`)
Conformément à la règle Silver `reauthentication-flow`, lorsque la télécommande subit une réinitialisation aux paramètres d'usine (Factory Reset) ou que les clés cryptographiques deviennent invalides, le coordinateur lève une exception `ConfigEntryAuthFailed`. Home Assistant bascule alors l'entrée en état d'alerte et propose le flux de réauthentification.

1. **Confirmation du re-jumelage (`async_step_reauth` / `async_step_reauth_confirm`)** :
   - Affiche un dialogue explicatif rappelant le nom et l'adresse IP de la télécommande.
   - Demande confirmation avant de lancer la renégociation de sécurité.
2. **Ré-exécution du protocole SRP-6a** :
   - Réutilise les étapes `async_step_pair_init` et `async_step_pair_pin` avec le contexte `self.context["reauth"] = True`.
   - L'utilisateur saisit le nouveau code PIN à 8 chiffres généré sur l'écran de la télécommande.
3. **Préservation absolue des entités et automatisations** :
   - Dès validation des preuves M4 et M5/M6, le Config Flow met à jour les clés cryptographiques de la `ConfigEntry` existante via `self.async_update_reload_and_abort(entry, data_updates={CONF_CONTROLLER_ID: controller_id, CONF_CLIENT_PRIVATE_KEY: client_sk.hex(), CONF_CLIENT_PUBLIC_KEY: client_pk.hex(), CONF_REMOTE_ID: remote_id, CONF_REMOTE_PUBLIC_KEY: remote_pk.hex()})`.
   - **Garantie architecturale** : Les identifiants uniques des entités (`unique_id`), leurs noms, leur historique de données et toutes les automatisations Lovelace / Home Assistant existantes sont conservés intacts sans aucune rupture de service.

---

### 3.2. Flux d'options et de maintenance (Options Flow)

Accessible via le bouton **Configurer** de l'intégration dans Home Assistant, le flux d'options permet de piloter le comportement opérationnel sans jamais réinitialiser l'appairage.

#### 3.2.1. Sélection et filtrage des entités Home Assistant exposées
- **Sélecteur d'entités graphique (`EntitySelector`)** :
  L'utilisateur choisit précisément les entités Home Assistant qu'il souhaite rendre contrôlables depuis la télécommande, avec filtrage par domaine :
  - `light` (Plafonniers, rubans LED, lampes de chevet)
  - `switch` (Prises commandées, relais)
  - `scene` (Ambiances lumineuses et de vie)
  - `media_player` (Téléviseurs, Apple TV, amplificateurs)
- **Bouton d'action de synchronisation** :
  Un bouton à bascule permet de choisir entre :
  - *Synchronisation automatique instantanée* : Dès qu'une entité sélectionnée change d'état ou qu'une modification survient, le carrousel de la télécommande est mis à jour.
  - *Synchronisation manuelle* : La télécommande n'est mise à jour que lors du déclenchement du bouton dédié `button.sevenhugs_smart_remote_sync`.

#### 3.2.2. Gestion du carrousel de la télécommande (`carousel_order`)
La télécommande organise son interface sous la forme d'un carrousel vertical ou horizontal d'écrans. Le flux d'options permet d'organiser cette disposition :
- Masquer les services inutilisés (ex: masquage de l'écran météo ou des services cloud défunts).
- Réordonner la priorité d'affichage : Placer le contrôle de la télévision en premier écran, suivi des lumières du salon.
- Requête envoyée en tâche de fond : `PATCH /api/settings` avec le tableau `carousel_order` mis à jour.

#### 3.2.3. Réglages des intervalles de polling, timeouts et mode d'économie d'énergie
- **Intervalle de rafraîchissement (`polling_interval`)** :
  Curseur configurable entre `15` et `300` secondes (valeur par défaut recommandée : `60` secondes).
- **Mode économie d'énergie intelligente** :
  Lorsque ce mode est activé, si la télécommande indique qu'elle est sur batterie et que son accéléromètre ne détecte aucun mouvement depuis plus de 2 minutes, l'intervalle de polling est automatiquement étendu à 300 secondes pour lui permettre de maintenir son Wi-Fi en veille basse consommation (DTIM power save). Dès qu'un appui tactile survient (webhook entrant), le polling repasse instantanément à la cadence nominale.

#### 3.2.4. Sauvegarde et restauration de la configuration JSON
- **Sauvegarde intégrale de la configuration** :
  Exporte un instantané JSON complet (appareils, scènes, disposition UI, groupes de composants) téléchargeable directement depuis le navigateur.
- **Restauration de configuration** :
  Permet d'importer un fichier de configuration JSON préalablement sauvegardé pour réinjecter l'ensemble des écrans, fragments et raccourcis en un seul clic.

---

### 3.3. Modèle de synchronisation et mapping fonctionnel des entités

La télécommande Sevenhugs supporte deux canaux majeurs d'interaction pour piloter la maison connectée :
1. **Les Webhooks HTTP entrants (`sevenhugs.control.webhooks`)** : Idéal pour les commandes discrètes (On/Off, Toggle, Scènes, Télécommande multimédia).
2. **Le pont Philips Hue émulé (`sevenhugs.control.hue`)** : Idéal pour les commandes continues (curseurs de luminosité 0-100%, sélecteur de couleurs RGB, température de blanc).

```text
+---------------------------------------------------------------------------------------------------+
|                        MAPPING FONCTIONNEL DES DOMAINES HOME ASSISTANT                            |
+---------------------------------------------------------------------------------------------------+
| Domaine HA       | Interface Sevenhugs          | Canal Réseau           | Capacité UI Télécommande  |
|------------------+------------------------------+------------------------+---------------------------|
| light (dimmer)   | sevenhugs.ui.light           | Emulated Hue (Port 80) | Curseur 0-100%, Roue RGB  |
| light (on/off)   | sevenhugs.ui.webhooks        | Webhook (Port 8123)    | Bouton carré On/Off       |
| switch           | sevenhugs.ui.plug / webhooks | Webhook (Port 8123)    | Bouton bascule Toggle     |
| media_player     | sevenhugs.control.infrared   | Webhooks (Grille 6x12) | D-pad, Vol+/-, Ch+/-, Num |
| scene            | sevenhugs.ui.webhooks        | Webhook (Port 8123)    | Bouton d'action unique    |
| sensor / diag    | Home Assistant Dashboards    | Polling chiffré API 80 | Télémétrie batterie/Wi-Fi |
+---------------------------------------------------------------------------------------------------+
```

#### 3.3.1. Domaine `light` (Lumières) : Modes Webhooks directs vs Pont Philips Hue émulé

##### Option 1 : Contrôle par curseur continu via `sevenhugs.control.hue` (Recommandé pour variateurs)
La télécommande intègre en ROM l'interface native `sevenhugs.ui.light`. Elle s'associe nativement avec le composant standard `emulated_hue` de Home Assistant :
- **Requête de la télécommande lors du déplacement du curseur de luminosité** :
  ```http
  PUT /api/homeassistant/lights/light.salon_plafonnier/state HTTP/1.1
  Host: 192.168.1.100:80
  Content-Type: application/json

  {
    "on": true,
    "bri": 185
  }
  ```
- **Traduction par Home Assistant** : Appel immédiat du service `light.turn_on(entity_id="light.salon_plafonnier", brightness=185)`.
- **Réglage des couleurs RGB** : La télécommande envoie les coordonnées CIE 1931 `{"xy": [0.457, 0.410]}`, immédiatement converties par Home Assistant.

##### Option 2 : Contrôle par bouton discret via Webhooks directs
Idéal pour les éclairages simples sans variation :
- Configuration d'un bouton poussoir dans `sevenhugs.control.webhooks`.
- Requête émise par la télécommande :
  ```http
  POST /api/webhook/sr_webhook_entryid HTTP/1.1
  Host: 192.168.1.100:8123
  Content-Type: application/json

  {
    "domain": "light",
    "action": "toggle",
    "entity_id": "light.applique_couloir"
  }
  ```

#### 3.3.2. Domaine `switch` (Interrupteurs et prises connectées)
Les prises et commutateurs utilisent la disposition `sevenhugs.ui.plug` ou un widget bouton dans `sevenhugs.control.webhooks` :
- **Payload Webhook transmis lors de l'appui sur l'écran tactile** :
  ```json
  {
    "event": "button_press",
    "domain": "switch",
    "action": "toggle",
    "entity_id": "switch.prise_machine_cafe",
    "source_remote": "5726"
  }
  ```
- **Comportement Home Assistant** : Le gestionnaire de Webhook exécute instantanément `switch.toggle(entity_id="switch.prise_machine_cafe")`.

#### 3.3.3. Domaine `media_player` (Téléviseurs, barres de son, récepteurs AV) et grille 6x12
Pour les lecteurs multimédia, la télécommande Sevenhugs fournit un modèle de grille géométrique stricte de **6 colonnes sur 12 lignes** ($x \in [0..5]$, $y \in [0..11]$) par écran (`CustomDeviceLayout`). 

Afin de respecter la limite physique de 12 lignes de hauteur de l'écran AMOLED tout en offrant une ergonomie complète (Volume, Chaînes, Navigation directionnelle, Pavé numérique, Couleurs et Touches de contrôle système), l'interface de télévision est découpée au sein du fragment `frag_tv_controls` en **deux écrans successifs balayables par carrousel tactile horizontal** :
1. **Écran 1 (« Principal »)** : Regroupe les commandes de visionnage immédiat — le double rocker `volume_channel` ($6 \times 6$ occupant les lignes $0$ à $5$) et la croix directionnelle `navigation` ($6 \times 6$ occupant les lignes $6$ à $11$).
2. **Écran 2 (« Clavier & Actions »)** : Accessible d'un simple swipe horizontal vers la gauche, il regroupe le pavé numérique `numpad` ($6 \times 8$ occupant les lignes $0$ à $7$), la barre de touches de couleur `abcd` ($6 \times 2$ occupant les lignes $8$ et $9$), et 3 boutons individuels de contrôle `button` ($2 \times 2$ chacun, alignés côte à côte sur les lignes $10$ et $11$ : Retour, Accueil et Menu).

```text
+-----------------------------------------------------------------------+
|  TOP BAR : [Icône TV]                      [Bouton Power : Rouge]     |
+=======================================================================+
|                 ÉCRAN 1 : CONTRÔLE & NAVIGATION                       |
|           Navigation Carrousel : [●] Écran 1  |  [○] Écran 2          |
+-----------------------------------------------------------------------+
| y \ x | Col 0   Col 1       | Col 2   Col 3       | Col 4   Col 5     |
+-------+---------------------+---------------------+-------------------+
|  y=0  |                     |                     |                   |
|  y=1  |     [ Volume + ]    |  WIDGET MONOLITHIQUE|    [ Chaîne + ]   |
|  y=2  |                     |   volume_channel    |                   |
|  y=3  |     [ Volume - ]    |    (Dim: 6 x 6)     |    [ Chaîne - ]   |
|  y=4  |                     |                     |                   |
|  y=5  |      [ Mute ]       |                     |    [ Source AV ]  |
+-------+---------------------+---------------------+-------------------+
|  y=6  |                     |     ▲ [ Haut ]      |                   |
|  y=7  |                     |                     |                   |
|  y=8  |    ◄ [ Gauche ]     |       [ OK ]        |    ► [ Droite ]   |
|  y=9  |                     |  WIDGET navigation  |                   |
|  y=10 |                     |     ▼ [ Bas ]       |                   |
|  y=11 |                     |    (Dim: 6 x 6)     |                   |
+=======================================================================+
                 >>> Glissement tactile (Swipe Gauche) >>>
+=======================================================================+
|                 ÉCRAN 2 : PAVÉ NUMÉRIQUE & ACTIONS                    |
|           Navigation Carrousel : [○] Écran 1  |  [●] Écran 2          |
+-----------------------------------------------------------------------+
| y \ x | Col 0   Col 1       | Col 2   Col 3       | Col 4   Col 5     |
+-------+---------------------+---------------------+-------------------+
|  y=0  |       [ 1 ]         |        [ 2 ]        |       [ 3 ]       |
|  y=1  |                     |                     |                   |
|  y=2  |       [ 4 ]         |        [ 5 ]        |       [ 6 ]       |
|  y=3  |                     |   WIDGET numpad     |                   |
|  y=4  |       [ 7 ]         |    (Dim: 6 x 8)     |       [ 8 ]       |
|  y=5  |                     |                     |                   |
|  y=6  |     [ Effacer ]     |        [ 0 ]        |      [ OK ]       |
|  y=7  |                     |                     |                   |
+-------+---------------------+---------------------+-------------------+
|  y=8  | [A: Rouge] [B: Vert]| [C: Jaun] [D: Bleu] |  WIDGET abcd      |
|  y=9  |  Touches Couleur    |  Touches Couleur    |   (Dim: 6 x 2)    |
+-------+---------------------+---------------------+-------------------+
|  y=10 |    [ Retour ]       |     [ Accueil ]     |     [ Menu ]      |
|  y=11 |  (button 2 x 2)     |   (button 2 x 2)    |  (button 2 x 2)   |
+=======================================================================+
```

##### Structure complète du payload d'injection pour `/api/custom/sevenhugs.control.webhooks:ha_tv_salon` :
```json
{
  "package_id": "sevenhugs.control.webhooks",
  "device_id": "ha_tv_salon",
  "customized": true,
  "layout": {
    "top": {
      "id": "top_bar_tv",
      "type": "top",
      "icon": "tv",
      "controls": [
        {
          "touch": {
            "key": "power",
            "name": "Power",
            "origin": "custom",
            "type": 1,
            "device_id": "ha_tv_salon",
            "package_id": "sevenhugs.control.webhooks"
          }
        }
      ]
    },
    "fragments": [
      {
        "id": "frag_tv_controls",
        "name": "Contrôle TV",
        "screens": [
          {
            "name": "Principal",
            "type": "basic",
            "widgets": [
              {
                "id": "vol_ch_widget",
                "type": "volume_channel",
                "x": 0,
                "y": 0,
                "controls": [
                  { "id": "vol_up", "touch": { "key": "volume_up", "name": "Vol+", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "vol_down", "touch": { "key": "volume_down", "name": "Vol-", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "ch_up", "touch": { "key": "channel_up", "name": "Ch+", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "ch_down", "touch": { "key": "channel_down", "name": "Ch-", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } }
                ]
              },
              {
                "id": "dpad_widget",
                "type": "navigation",
                "x": 0,
                "y": 6,
                "controls": [
                  { "id": "up", "touch": { "key": "dpad_up", "name": "Haut", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "down", "touch": { "key": "dpad_down", "name": "Bas", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "left", "touch": { "key": "dpad_left", "name": "Gauche", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "right", "touch": { "key": "dpad_right", "name": "Droite", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "ok", "touch": { "key": "dpad_ok", "name": "OK", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } }
                ]
              }
            ]
          },
          {
            "name": "Clavier & Actions",
            "type": "basic",
            "widgets": [
              {
                "id": "numpad_widget",
                "type": "numpad",
                "x": 0,
                "y": 0,
                "controls": [
                  { "id": "btn_1", "touch": { "key": "num_1", "name": "1", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "btn_2", "touch": { "key": "num_2", "name": "2", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "btn_3", "touch": { "key": "num_3", "name": "3", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "btn_4", "touch": { "key": "num_4", "name": "4", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "btn_5", "touch": { "key": "num_5", "name": "5", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "btn_6", "touch": { "key": "num_6", "name": "6", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "btn_7", "touch": { "key": "num_7", "name": "7", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "btn_8", "touch": { "key": "num_8", "name": "8", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "btn_9", "touch": { "key": "num_9", "name": "9", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "btn_0", "touch": { "key": "num_0", "name": "0", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "btn_enter", "touch": { "key": "num_enter", "name": "Entrée", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } }
                ]
              },
              {
                "id": "abcd_widget",
                "type": "abcd",
                "x": 0,
                "y": 8,
                "controls": [
                  { "id": "color_red", "touch": { "key": "color_red", "name": "Rouge (A)", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "color_green", "touch": { "key": "color_green", "name": "Vert (B)", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "color_yellow", "touch": { "key": "color_yellow", "name": "Jaune (C)", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } },
                  { "id": "color_blue", "touch": { "key": "color_blue", "name": "Bleu (D)", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } }
                ]
              },
              {
                "id": "back_button",
                "type": "button",
                "x": 0,
                "y": 10,
                "controls": [
                  { "id": "nav_back", "touch": { "key": "nav_back", "name": "Retour", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } }
                ]
              },
              {
                "id": "home_button",
                "type": "button",
                "x": 2,
                "y": 10,
                "controls": [
                  { "id": "nav_home", "touch": { "key": "nav_home", "name": "Accueil", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } }
                ]
              },
              {
                "id": "menu_button",
                "type": "button",
                "x": 4,
                "y": 10,
                "controls": [
                  { "id": "nav_menu", "touch": { "key": "nav_menu", "name": "Menu", "origin": "custom", "device_id": "ha_tv_salon", "package_id": "sevenhugs.control.webhooks" } }
                ]
              }
            ]
          }
        ]
      }
    ]
  }
}
```

##### Correspondance des touches vers les services Home Assistant :

| Écran / Zone | Clé Protocole (`key`) | Service Home Assistant Cible | Paramètres / Exemple d'appel |
|:---|:---|:---|:---|
| **Top Bar** | `power` | `media_player.toggle` | `{"entity_id": "media_player.tv_salon"}` |
| **Écran 1** | `volume_up` | `media_player.volume_up` | `{"entity_id": "media_player.tv_salon"}` |
| **Écran 1** | `volume_down` | `media_player.volume_down` | `{"entity_id": "media_player.tv_salon"}` |
| **Écran 1** | `channel_up` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "channel_up"}` |
| **Écran 1** | `channel_down` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "channel_down"}` |
| **Écran 1** | `dpad_up` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "up"}` |
| **Écran 1** | `dpad_down` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "down"}` |
| **Écran 1** | `dpad_left` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "left"}` |
| **Écran 1** | `dpad_right` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "right"}` |
| **Écran 1** | `dpad_ok` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "select"}` |
| **Écran 2** | `num_0` à `num_9` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "{{ key[4:] }}"}` |
| **Écran 2** | `num_enter` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "enter"}` |
| **Écran 2** | `color_red` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "red"}` |
| **Écran 2** | `color_green` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "green"}` |
| **Écran 2** | `color_yellow` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "yellow"}` |
| **Écran 2** | `color_blue` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "blue"}` |
| **Écran 2** | `nav_back` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "back"}` |
| **Écran 2** | `nav_home` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "home"}` |
| **Écran 2** | `nav_menu` | `remote.send_command` | `{"entity_id": "remote.tv_salon", "command": "menu"}` |

#### 3.3.4. Domaine `scene` (Scènes domotiques)
Les scènes créées dans Home Assistant sont injectées directement dans la télécommande via l'API chiffrée :
- **Endpoint** : `PUT /api/home-scenes/sources/home_assistant`
- **Payload injecté par Home Assistant** :
  ```json
  [
    {
      "id": "scene_cinema",
      "name": "Mode Cinéma",
      "icon": "scene_movie",
      "source": "home_assistant",
      "hidden": false
    },
    {
      "id": "scene_lecture",
      "name": "Lecture Zen",
      "icon": "scene_reading",
      "source": "home_assistant",
      "hidden": false
    }
  ]
  ```
- **Déclenchement** : Lorsque l'utilisateur tape sur l'icône de la scène, la télécommande déclenche le Webhook associé, exécutant immédiatement `scene.turn_on(entity_id="scene.mode_cinema")`.

#### 3.3.5. Entités de télémétrie de la télécommande (`sensor`, `binary_sensor`, `button`)
L'intégration enregistre automatiquement les entités suivantes au sein de Home Assistant pour superviser l'état matériel de la télécommande :

| Identifiant de l'entité | Plateforme | Classe d'appareil (`device_class`) | Catégorie (`entity_category`) | Unité | Description fonctionnelle |
| :--- | :--- | :--- | :--- | :---: | :--- |
| `sensor.sevenhugs_battery` | `sensor` | `SensorDeviceClass.BATTERY` | `-` | `%` | Niveau de charge de la batterie Li-ion interne (0 à 100 %). |
| `binary_sensor.sevenhugs_charging` | `binary_sensor` | `BinarySensorDeviceClass.BATTERY_CHARGING` | `-` | - | `on` si la télécommande repose sur sa station de charge magnétique. |
| `sensor.sevenhugs_wifi_signal` | `sensor` | `SensorDeviceClass.SIGNAL_STRENGTH` | `EntityCategory.DIAGNOSTIC` | `dBm` | Puissance du signal RSSI reçu de la borne Wi-Fi. |
| `sensor.sevenhugs_active_screen` | `sensor` | `None` | `EntityCategory.DIAGNOSTIC` | - | Nom de l'écran ou de l'appareil actuellement affiché à l'écran. |
| `button.sevenhugs_sync_entities` | `button` | `None` | `EntityCategory.DIAGNOSTIC` | - | Force la synchronisation immédiate des entités et des webhooks vers la télécommande. |
| `button.sevenhugs_backup_config` | `button` | `None` | `EntityCategory.DIAGNOSTIC` | - | Génère et télécharge un dump complet de sauvegarde de la ROM/RAM. |

---

## 4. R3. Feuille de Route Technique & Spécifications d'Implémentation

### 4.1. Arborescence cible du composant HACS (Conformité Home Assistant Quality Scale)

L'architecture des répertoires et des modules respecte scrupuleusement les exigences des composants personnalisés Home Assistant ainsi que les critères d'excellence du **Quality Scale** (typage strict `mypy`, documentation exhaustive, gestion asynchrone, localisation multilingue) :

```text
custom_components/sevenhugs_smart_remote/
├── __init__.py                # Point d'entrée : async_setup_entry, async_unload_entry, rechargement
├── manifest.json             # Déclaration formelle HACS/HA, intégration zeroconf, dépendances
├── const.py                  # Constantes (DOMAIN, KEYS, URLS, DEFAULT_SETTINGS, TIMEOUTS)
├── config_flow.py            # Logique ConfigFlow (zeroconf, user, pair_init, pair_pin) & OptionsFlow
├── coordinator.py            # SevenhugsDataUpdateCoordinator (gestion du polling et veille)
├── client.py                 # AsyncSmartRemoteClient (client aiohttp Keep-Alive, cryptage ChaCha20)
├── crypto.py                 # Primitives cryptographiques (SRP-6a RFC 5054, Ed25519, X25519, TLV8)
├── entity.py                 # SevenhugsBaseEntity (gestion du DeviceRegistry et liaison coordinator)
├── sensor.py                 # Plateforme Sensor (batterie, RSSI Wi-Fi, écran actif)
├── binary_sensor.py          # Plateforme Binary Sensor (chargeur connecté, connectivité réseau)
├── button.py                 # Plateforme Button (forcer synchro, forcer sauvegarde)
├── switch.py                 # Plateforme Switch (activation auto-sync, verrouillage d'écran)
├── webhook.py                # Gestionnaire de Webhook entrant (/api/webhook/sevenhugs_{entry_id})
├── services.py               # Services HA personnalisés (push_custom_ui, backup_config, restore_config)
├── services.yaml             # Schémas et descriptions des services pour l'interface utilisateur
├── strings.json              # Chaînes de caractères sources en anglais et modèles de formulaires
├── translations/             # Fichiers de localisation multilingues
│   ├── en.json               # Localisation anglaise complète
│   └── fr.json               # Localisation française complète
└── diagnostics.py            # Plateforme de diagnostic (extraction anonymisée pour support de bugs)
```

Fichiers de gouvernance racine :
```text
hacs.json                     # Métadonnées pour le magasin communautaire HACS
README.md                     # Documentation utilisateur complète avec captures d'écran
LICENSE                       # Licence Open Source (Apache 2.0 / MIT)
```

#### Fichier `manifest.json` normatif :
```json
{
  "domain": "sevenhugs_smart_remote",
  "name": "Sevenhugs Smart Remote",
  "codeowners": ["@LudovicLaly"],
  "config_flow": true,
  "documentation": "https://github.com/LudovicLaly/smart-remote-rebirth",
  "issue_tracker": "https://github.com/LudovicLaly/smart-remote-rebirth/issues",
  "dependencies": ["webhook"],
  "integration_type": "hub",
  "iot_class": "local_polling",
  "version": "1.0.0",
  "requirements": [
    "cryptography>=41.0.0"
  ],
  "zeroconf": [
    {
      "type": "_smart_remote._tcp.local.",
      "name": "Smart Remote*"
    }
  ]
}
```

#### Fichier `hacs.json` normatif :
```json
{
  "name": "Sevenhugs Smart Remote",
  "render_readme": true,
  "homeassistant": "2024.1.0"
}
```

#### Architecture du cycle de vie et données d'exécution (`__init__.py`) :
Conformément aux standards modernes de Home Assistant Core (HA 2024.3+, règle Bronze `runtime-data`), l'intégration utilise un typage fort via `entry.runtime_data` au lieu du dictionnaire global `hass.data[DOMAIN]`. De plus, afin de garantir une socket TCP Keep-Alive exclusive et d'isoler le trafic de la télécommande, l'intégration instancie un `TCPConnector` dédié via `async_create_clientsession` :

```python
"""Module d'initialisation et de gestion du cycle de vie de l'intégration Sevenhugs."""

from dataclasses import dataclass
import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .client import AsyncSmartRemoteClient
from .const import CONF_HOST, CONF_KEYS, DOMAIN, PLATFORMS
from .coordinator import SevenhugsDataUpdateCoordinator

type SevenhugsConfigEntry = ConfigEntry[SevenhugsData]


@dataclass
class SevenhugsData:
    """Conteneur de données d'exécution stocké dans entry.runtime_data (standard HA 2024.3+)."""
    client: AsyncSmartRemoteClient
    coordinator: SevenhugsDataUpdateCoordinator
    session: aiohttp.ClientSession


async def async_setup_entry(hass: HomeAssistant, entry: SevenhugsConfigEntry) -> bool:
    """Initialise l'intégration Sevenhugs Smart Remote à partir d'une config entry."""
    # Création du connecteur dédié avec socket Keep-Alive exclusive
    connector = aiohttp.TCPConnector(
        force_close=False,          # Maintient le socket ouvert entre M1 et M3 (Keep-Alive obligatoire)
        limit=1,                    # 1 seule connexion TCP dans tout le pool du client
        limit_per_host=1,           # 1 seule connexion simultanée vers l'IP de la télécommande
        enable_cleanup_closed=True, # Nettoie immédiatement les sockets fermés par la télécommande
        keepalive_timeout=60.0,     # S'aligne sur le timeout de veille de 60s du firmware Sevenhugs
    )
    session = async_create_clientsession(
        hass,
        verify_ssl=False,
        auto_cleanup=False,         # Le cycle de vie est managé explicitement par l'entrée de configuration
        connector=connector,
    )

    client = AsyncSmartRemoteClient(
        host=entry.data[CONF_HOST],
        session=session,
        keys=entry.data[CONF_KEYS],
    )

    coordinator = SevenhugsDataUpdateCoordinator(hass, client=client, entry=entry)
    await coordinator.async_config_entry_first_refresh()

    # Stockage typé moderne dans runtime_data
    entry.runtime_data = SevenhugsData(
        client=client,
        coordinator=coordinator,
        session=session,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SevenhugsConfigEntry) -> bool:
    """Décharge l'intégration et libère la socket TCP dédiée."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.client.close()  # Ferme la session aiohttp et libère les sockets
    return unload_ok
```

---

### 4.2. Spécification détaillée des modules Python

#### 4.2.1. Module `crypto.py` : Primitives TLV8, SRP-6a 3072-bit, Ed25519/X25519, ChaCha20-Poly1305
Ce module regroupe les fonctions cryptographiques pures sans aucune dépendance vis-à-vis du réseau :
1. **Gestionnaire TLV8 binaire** :
   - `encode_tlv(items: list[tuple[int, bytes | int | str]]) -> bytes` : Prend en charge la fragmentation automatique des valeurs de plus de 255 octets en blocs consécutifs de même tag.
   - `decode_tlv(data: bytes) -> dict[int, bytes]` : Réassemble les fragments consécutifs de même tag et permet le parsing imbriqué des sous-TLV.
2. **Calculs SRP-6a 3072-bit (RFC 5054) et compatibilité NimbusDS** :
   - Constantes : Modulo premier $N$ de 384 octets (MODP 3072-bit RFC 5054), générateur $g = 5$.
   - **Gestion de la troncature des zéros BigInteger NimbusDS** :
     La télécommande et l'application officielle Android utilisent la bibliothèque Java `com.nimbusds:srp6a`. La méthode `BigInteger.toByteArray()` de Java, combinée au helper NimbusDS `bigIntegerToBytes`, tronque systématiquement les octets nuls de poids fort. Si le hash SHA-512 de la preuve $M_1$ ou $M_2$ débute par `0x00` (probabilité cumulée de 0,78 % soit 1 tentative sur 128), un client Python naïf produirait 64 octets là où la télécommande en attend ou en émet 63, causant un rejet d'appairage inexplicable.
     Pour assurer une compatibilité rigoureuse à 100 %, `crypto.py` intègre les convertisseurs grand-boutistes conformes `big_integer_to_bytes` et `big_integer_from_bytes`.
3. **Chiffrement Authentifié ChaCha20-Poly1305** :
   - Utilise `cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305`.
   - Fonction `encrypt_api_payload(key: bytes, plaintext: bytes) -> bytes` utilisant le nonce fixe universel `b"\x00\x00\x00\x00HuMiThFr"`.
   - Renvoie directement `ciphertext || poly1305_tag_16_bytes` (corps HTTP monolithique conforme Sevenhugs).

##### Code complet de référence pour `custom_components/sevenhugs_smart_remote/crypto.py` :
```python
"""Primitives cryptographiques pour Sevenhugs Smart Remote (RFC 5054 SRP-6a, Ed25519, TLV8)."""

import hashlib
import secrets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

API_NONCE = b"\x00\x00\x00\x00HuMiThFr"


def big_integer_to_bytes(val: int) -> bytes:
    """Sérialisation BigInteger grand-boutiste 100% compatible NimbusDS SRP-6a.
    
    Reproduit exactement la sémantique de com.nimbusds.srp6.BigIntegerUtils.bigIntegerToBytes:
    élimine les zéros de tête non signés et évite les divergences sur les hashs SHA-512.
    """
    length = (val.bit_length() + 8) // 8
    b = val.to_bytes(length, byteorder="big", signed=True)
    if len(b) > 0 and b[0] == 0:
        b = b[1:]
    return b


def big_integer_from_bytes(b: bytes) -> int:
    """Désérialisation grand entier positif (équivalent Java new BigInteger(1, bytes))."""
    return int.from_bytes(b, byteorder="big")


def pad_to_n(b: bytes, target_len: int = 384) -> bytes:
    """Bourrage grand-boutiste à 384 octets pour SRP-6a RFC 5054."""
    if len(b) < target_len:
        return b.rjust(target_len, b"\x00")
    return b


def encode_tlv(items: list[tuple[int, bytes | int | str]]) -> bytes:
    """Encode une liste de tuples (tag, valeur) en format binaire TLV8."""
    out = bytearray()
    for tag, val in items:
        if isinstance(val, int):
            val_bytes = bytes([val])
        elif isinstance(val, str):
            val_bytes = val.encode("utf-8")
        else:
            val_bytes = val

        pos = 0
        while pos < len(val_bytes):
            chunk = val_bytes[pos : pos + 255]
            out.append(tag)
            out.append(len(chunk))
            out.extend(chunk)
            pos += len(chunk)
    return bytes(out)


def decode_tlv(data: bytes) -> dict[int, bytes]:
    """Décode un flux binaire TLV8 en dictionnaire tag -> valeur réassemblée."""
    pos = 0
    res: dict[int, bytes] = {}
    while pos < len(data):
        tag = data[pos]
        length = data[pos + 1]
        val = data[pos + 2 : pos + 2 + length]
        res[tag] = res.get(tag, b"") + val
        pos += 2 + length
    return res


def compute_srp_m1_sync(
    salt_bytes: bytes,
    b_bytes: bytes,
    pin_code: str,
    n_int: int,
    g_int: int,
) -> tuple[bytes, bytes, bytes]:
    """Exécute les calculs intensifs SRP-6a client dans un thread worker.
    
    Retourne : (A_bytes, M1_proof, shared_key_K).
    Garantit la compatibilité stricte NimbusDS sur les hashs SHA-512 via big_integer_to_bytes.
    """
    b_int = big_integer_from_bytes(b_bytes)
    salt_int = big_integer_from_bytes(salt_bytes)

    # Clé privée éphémère a (1024 bits aléatoires)
    a_bytes = secrets.token_bytes(128)
    a = big_integer_from_bytes(a_bytes)
    a_pub = pow(g_int, a, n_int)
    a_bytes = big_integer_to_bytes(a_pub)

    # Calcul de k = SHA512(pad384(N) || pad384(g))
    n_padded = pad_to_n(big_integer_to_bytes(n_int), 384)
    g_padded = pad_to_n(big_integer_to_bytes(g_int), 384)
    k = big_integer_from_bytes(hashlib.sha512(n_padded + g_padded).digest())

    # Paramètre de brouillage u = SHA512(pad384(A) || pad384(B))
    a_padded = pad_to_n(a_bytes, 384)
    b_padded = pad_to_n(big_integer_to_bytes(b_int), 384)
    u = big_integer_from_bytes(hashlib.sha512(a_padded + b_padded).digest())

    # Dérivation x = SHA512(s || SHA512("Pair-Setup:" || PIN))
    username = b"Pair-Setup"
    h_i_p = hashlib.sha512(username + b":" + pin_code.encode("utf-8")).digest()
    s_bytes = big_integer_to_bytes(salt_int)
    x = big_integer_from_bytes(hashlib.sha512(s_bytes + h_i_p).digest())

    # Secret partagé S = (B - k * (g^x mod N))^(a + u*x) mod N
    val1 = (b_int - k * pow(g_int, x, n_int)) % n_int
    val2 = a + u * x
    s_int = pow(val1, val2, n_int)
    shared_key_k = hashlib.sha512(big_integer_to_bytes(s_int)).digest()

    # Calcul de M1 = SHA512( (H(N) ^ H(g)) || H(I) || s || A || B || K )
    hn = hashlib.sha512(big_integer_to_bytes(n_int)).digest()
    hg = hashlib.sha512(big_integer_to_bytes(g_int)).digest()
    h_xor = big_integer_to_bytes(big_integer_from_bytes(hn) ^ big_integer_from_bytes(hg))
    hi = hashlib.sha512(username).digest()

    m1_input = h_xor + hi + s_bytes + a_bytes + big_integer_to_bytes(b_int) + shared_key_k
    m1_hash = hashlib.sha512(m1_input).digest()
    # Normalisation BigInteger NimbusDS (élimine 0x00 initial éventuel)
    m1_proof = big_integer_to_bytes(big_integer_from_bytes(m1_hash))

    return a_bytes, m1_proof, shared_key_k


def verify_srp_m2(a_bytes: bytes, m1_proof: bytes, k_key: bytes, server_m2_raw: bytes) -> bool:
    """Valide la preuve M2 renvoyée par la télécommande en M4."""
    expected_m2_hash = hashlib.sha512(a_bytes + m1_proof + k_key).digest()
    expected_m2 = big_integer_to_bytes(big_integer_from_bytes(expected_m2_hash))
    server_m2 = big_integer_to_bytes(big_integer_from_bytes(server_m2_raw))
    return expected_m2 == server_m2


def encrypt_api_payload(key: bytes, plaintext: bytes) -> bytes:
    """Chiffre un payload JSON monolithique avec ChaCha20-Poly1305 et le nonce universel."""
    chacha = ChaCha20Poly1305(key)
    return chacha.encrypt(API_NONCE, plaintext, None)


def decrypt_api_payload(key: bytes, ciphertext: bytes) -> bytes:
    """Déchiffre un payload HTTP monolithique et valide le tag Poly1305 de 16 octets."""
    chacha = ChaCha20Poly1305(key)
    return chacha.decrypt(API_NONCE, ciphertext, None)
```

#### 4.2.2. Module `client.py` (`AsyncSmartRemoteClient`) : Gestionnaire HTTP asynchrone Keep-Alive
La classe `AsyncSmartRemoteClient` encapsule l'intégralité du dialogue réseau avec la télécommande, avec isolation stricte de la connexion Keep-Alive et exclusion mutuelle de concurrence :
- **Attributs de connexion et isolation de session** :
  - `host: str` : Adresse IP de la télécommande.
  - `session: aiohttp.ClientSession` : Session asynchrone dédiée créée via `async_create_clientsession(hass, connector=TCPConnector(force_close=False, limit=1, limit_per_host=1, enable_cleanup_closed=True, keepalive_timeout=60.0))` (interdisant tout pool global non borné).
  - `_verify_lock: asyncio.Lock` : Protège la négociation `Pair-Verify` contre les ruées (*thundering herd*) via le patron de verrouillage à double vérification (*Double-Checked Locking*).
  - `_request_lock: asyncio.Lock` : Sérialise l'accès physique à la socket TCP unique afin d'interdire toute émission REST concurrente pendant le handshake M1-M4.
- **Méthodes clés d'authentification** :
  - `async_pair_setup_m1() -> tuple[bytes, bytes]` : Envoie M1 avec timeout de 60s, extrait le Salt et la clé publique $B$.
  - `async_pair_setup_finish(pin: str, salt: bytes, B: bytes) -> tuple[str, bytes]` : Exécute M3 à M6 et sauvegarde le `remote_id` et la clé publique Ed25519 de la télécommande.
  - `async_pair_verify(force: bool = False) -> bool` : Exécute le handshake M1-M4 sur la même socket TCP Keep-Alive sous exclusion mutuelle stricte.
- **Automatisation du Moteur Lua (`setup.lua`)** :
  Méthode `async_push_webhooks(triggers: list[dict]) -> bool` :
  - Étape 1 : `POST /api/devices` avec manifeste Webhooks $\rightarrow$ réception de `/api/devices/sevenhugs.control.webhooks/setup`.
  - Étape 2 : `GET` sur l'URL d'assistant pour capturer le cookie `7HUGS_SID`.
  - Étape 3 : Séquence d'appels `POST` non chiffrés avec cookie : `connect` $\rightarrow$ `main_page` $\rightarrow$ boucle sur `add_choice` / `advanced_add_edit` / `post_advanced_add_edit` pour chaque bouton $\rightarrow$ validation finale `done`.

##### Code complet de référence pour `custom_components/sevenhugs_smart_remote/client.py` :
```python
"""Client HTTP asynchrone haute résilience pour Sevenhugs Smart Remote."""

import asyncio
import json
import logging
import aiohttp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .crypto import (
    decode_tlv,
    encode_tlv,
    encrypt_api_payload,
    decrypt_api_payload,
)

_LOGGER = logging.getLogger(__name__)


class SevenhugsAuthError(Exception):
    """Erreur d'authentification ou session expirée."""


class SevenhugsConnectionError(Exception):
    """Erreur de communication physique avec la télécommande."""


class AsyncSmartRemoteClient:
    """Client API gérant la persistance Keep-Alive, les verrous de concurrence et le chiffrement."""

    def __init__(
        self,
        host: str,
        session: aiohttp.ClientSession,
        keys: dict,
    ) -> None:
        self._host = host
        self._session = session
        self._keys = keys

        self._controller_id: str = keys["controller_id"]
        self._client_sk = ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(keys["client_private_key"])
        )
        self._remote_id: str = keys.get("remote_id", "")
        self._session_write_key: bytes | None = (
            bytes.fromhex(keys["session_write_key"]) if "session_write_key" in keys else None
        )
        self._session_read_key: bytes | None = (
            bytes.fromhex(keys["session_read_key"]) if "session_read_key" in keys else None
        )

        self._is_verified: bool = bool(self._session_write_key and self._session_read_key)

        # Verrous d'exclusion mutuelle pour protéger la socket Keep-Alive unique
        self._verify_lock = asyncio.Lock()   # Empêche la ruée (thundering herd) sur Pair-Verify
        self._request_lock = asyncio.Lock()  # Sérialise les échanges physiques sur la socket

    @property
    def is_verified(self) -> bool:
        """Indique si une session Pair-Verify active et valide est disponible."""
        return self._is_verified

    async def close(self) -> None:
        """Ferme la session HTTP et libère le connecteur TCP dédié."""
        if not self._session.closed:
            await self._session.close()

    async def async_pair_verify(self, force: bool = False) -> bool:
        """Effectue le handshake Pair-Verify (M1 -> M4) sous verrou strict d'exclusion mutuelle.
        
        Garantit :
        1. Absence de ruée : si plusieurs coroutines appellent verify en même temps, une seule l'exécute.
        2. Double-check : les autres constatent à l'acquisition du verrou que la session est prête.
        3. Exclusion de la socket : aucune requête REST ne peut s'intercaler entre M1 et M3.
        """
        async with self._verify_lock:
            if self._is_verified and not force:
                _LOGGER.debug("Pair-Verify déjà complété par une autre coroutine, session valide.")
                return True

            _LOGGER.debug("Démarrage du handshake Pair-Verify sur la socket dédiée...")
            async with self._request_lock:
                success = await self._execute_pair_verify_handshake()
                if success:
                    self._is_verified = True
                return success

    async def _execute_pair_verify_handshake(self) -> bool:
        """Exécution atomique interne de M1 à M4 sur la même socket Keep-Alive."""
        headers = {
            "Content-Type": "application/pairing+tlv8",
            "X-UUID": self._controller_id,
            "X-Version": "31",
            "X-Min-Version": "24",
            "Connection": "Keep-Alive",
            "User-Agent": "okhttp/3.12.0",
        }
        url = f"http://{self._host}/pair-verify"

        # 1. Génération clé éphémère X25519
        client_eph_sk = x25519.X25519PrivateKey.generate()
        client_eph_pk = client_eph_sk.public_key().public_bytes_raw()
        m1_data = encode_tlv([(0x03, client_eph_pk), (0x06, 1)])

        # Envoi de M1
        try:
            async with self._session.post(url, data=m1_data, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp1:
                if resp1.status != 200:
                    raise SevenhugsAuthError(f"Pair-Verify M1 rejeté (HTTP {resp1.status})")
                m2_raw = await resp1.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            self._is_verified = False
            raise SevenhugsConnectionError(f"Échec réseau lors de Pair-Verify M1: {err}") from err

        m2 = decode_tlv(m2_raw)
        remote_eph_pk = m2[0x03]

        # 2. Dérivation du secret partagé et de verify_key
        shared_secret = client_eph_sk.exchange(
            x25519.X25519PublicKey.from_public_bytes(remote_eph_pk)
        )
        verify_key = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=b"Pair-Verify-Encrypt-Salt",
            info=b"Pair-Verify-Encrypt-Info",
        ).derive(shared_secret)

        # 3. Signature Ed25519 et chiffrement de M3
        cid_bytes = self._controller_id.encode("utf-8")
        to_sign = client_eph_pk + cid_bytes + remote_eph_pk
        client_sig = self._client_sk.sign(to_sign)
        sub_m3 = encode_tlv([(0x0A, client_sig), (0x01, cid_bytes)])

        cipher = ChaCha20Poly1305(verify_key)
        enc_m3 = cipher.encrypt(b"\x00\x00\x00\x00PV-Msg03", sub_m3, None)
        m3_data = encode_tlv([(0x05, enc_m3), (0x06, 3)])

        # Envoi de M3 sur la MÊME connexion Keep-Alive
        try:
            async with self._session.post(url, data=m3_data, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp3:
                m4_raw = await resp3.read()
                m4 = decode_tlv(m4_raw)
                if resp3.status != 200 or m4.get(0x06) != b"\x04" or 0x07 in m4:
                    self._is_verified = False
                    raise SevenhugsAuthError(f"Échec Pair-Verify M3 (HTTP {resp3.status}): {m4}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            self._is_verified = False
            raise SevenhugsConnectionError(f"Échec réseau lors de Pair-Verify M3: {err}") from err

        # 4. Dérivation des clés permanentes de session
        self._session_write_key = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=b"Control-Salt",
            info=b"Control-Write-Encryption-Key",
        ).derive(shared_secret)
        self._session_read_key = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=b"Control-Salt",
            info=b"Control-Read-Encryption-Key",
        ).derive(shared_secret)

        _LOGGER.info("Pair-Verify réussi avec succès sur la socket Keep-Alive.")
        return True

    async def async_request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
    ) -> dict:
        """Envoie une requête chiffrée avec sérialisation stricte et ré-authentification transparente."""
        # 1. Vérification préalable de session
        if not self._is_verified:
            await self.async_pair_verify()

        # 2. Exécution avec retry automatique sous verrou
        for attempt in range(2):
            try:
                async with self._request_lock:
                    return await self._raw_encrypted_request(method, path, json_data)
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, SevenhugsAuthError) as err:
                if attempt == 1:
                    _LOGGER.error("Échec persistant de la requête %s %s après ré-authentification: %s", method, path, err)
                    raise
                _LOGGER.debug("Socket fermée ou session invalide (%s). Ré-exécution de Pair-Verify...", err)
                # La coroutine a libéré _request_lock lors de l'exception : aucun risque de deadlock !
                await self.async_pair_verify(force=True)

    async def _raw_encrypted_request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
    ) -> dict:
        """Émet physiquement la requête chiffrée sur la socket active."""
        if not self._session_write_key or not self._session_read_key:
            raise SevenhugsAuthError("Clés de session absentes.")

        url = f"http://{self._host}{path}"
        headers = {
            "Content-Type": "application/octet-stream",
            "X-UUID": self._controller_id,
            "X-Version": "31",
            "X-Min-Version": "24",
            "Connection": "Keep-Alive",
            "User-Agent": "okhttp/3.12.0",
        }

        body_bytes = b""
        if json_data is not None:
            raw_json = json.dumps(json_data, separators=(",", ":")).encode("utf-8")
            body_bytes = encrypt_api_payload(self._session_write_key, raw_json)

        async with self._session.request(
            method,
            url,
            data=body_bytes,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 400:
                # La télécommande renvoie HTTP 400 si la session est expirée ou déphasée
                raise SevenhugsAuthError("Session expirée sur la télécommande (HTTP 400)")
            if resp.status not in (200, 204):
                raise SevenhugsConnectionError(f"Erreur HTTP inattendue {resp.status} sur {path}")

            resp_bytes = await resp.read()
            if not resp_bytes:
                return {}

            decrypted_json = decrypt_api_payload(self._session_read_key, resp_bytes)
            return json.loads(decrypted_json.decode("utf-8"))
```

#### 4.2.3. Module `coordinator.py` : `DataUpdateCoordinator`, gestion de veille et résilience
La classe `SevenhugsDataUpdateCoordinator` hérite de `homeassistant.helpers.update_coordinator.DataUpdateCoordinator` :
- **Intervalle de rafraîchissement adaptatif** :
  - 60s par défaut lorsque la télécommande est connectée.
  - Détection de l'endormissement : Si une requête de polling échoue avec timeout TCP, le coordinateur ne marque **pas** immédiatement les entités en état `unavailable` (optimisme d'état). Il incrémente un compteur d'échecs. Ce n'est qu'après 3 échecs consécutifs (soit 3 minutes) que la télécommande est marquée comme « En veille ».
- **Réveil événementiel** :
  - Si un paquet mDNS est capté par Home Assistant ou si un Webhook entrant arrive de la télécommande, le coordinateur force un rafraîchissement immédiat de l'état sans attendre la prochaine échéance du timer.
- **Mise en cache locale** :
  Conserve en mémoire la dernière configuration connue des appareils et du carrousel afin que les cartes du tableau de bord restent disponibles et lisibles en permanence.

#### 4.2.4. Module `config_flow.py` : Gestionnaire d'étapes d'appairage, de reconfiguration et d'options
- **Flux de configuration principal (`ConfigFlow`)** :
  - `async_step_zeroconf(discovery_info)` : Découverte mDNS. Applique la règle Gold `discovery-update-connection` : si l'appareil est déjà configuré mais que son IP a changé, met à jour l'entrée existante via `self._abort_if_unique_id_configured(updates={CONF_HOST: host}, reload_on_update=True)` / `hass.config_entries.async_update_entry(...)` sans intervention manuelle.
  - `async_step_zeroconf_confirm` : Demande confirmation avant de lancer le jumelage.
  - `async_step_user` : Saisie manuelle de l'adresse IP et test de connectivité HTTP GET `/api/hello`.
  - `async_step_pair_init` : Initialisation de l'échange SRP-6a (`POST /pair-setup` M1) avec gestion du délai de calcul de 16s du microcontrôleur.
  - `async_step_pair_pin` : Saisie du code PIN formaté (`XXX-XX-XXX`), validation cryptographique M3-M6 et persistance des clés.
  - `async_step_reconfigure` : Règle Gold `reconfiguration-flow`. Permet de modifier l'adresse IP/Host depuis le bouton Reconfigurer, valide l'accessibilité réseau et le numéro de série matériel, et applique les modifications via `self.async_update_reload_and_abort`.
  - `async_step_reauth` et `async_step_reauth_confirm` : Règle Silver `reauthentication-flow`. Déclenché automatiquement sur `ConfigEntryAuthFailed`, rejoue le protocole SRP-6a et met à jour les clés cryptographiques via `self.async_update_reload_and_abort` en préservant intégralement les entités et automatisations existantes.
- **Flux d'options (`OptionsFlow`)** :
  - Implémenté via `SevenhugsOptionsFlowHandler` pour configurer les préférences utilisateur : sélection et filtrage des entités exposées (`EntitySelector`), réordonnancement du carrousel d'écrans (`carousel_order`), réglage de l'intervalle de rafraîchissement (`polling_interval`), et sauvegarde/restauration de la configuration JSON.

#### 4.2.5. Module `webhook.py` : Réception locale sécurisée et dispatch d'événements
- Enregistrement du point d'entrée lors du chargement de l'intégration :
  ```python
  webhook_id = f"sevenhugs_{entry.entry_id}"
  webhook.async_register(
      hass,
      DOMAIN,
      "Sevenhugs Smart Remote Trigger",
      webhook_id,
      async_handle_webhook_payload,
      local_only=True  # Sécurité stricte : rejette tout appel venant de l'extérieur du LAN
  )
  ```
- **Traitement du payload entrant** :
  1. Vérification de la structure JSON (`domain`, `action`, `entity_id`).
  2. Si l'action correspond à un service valide (ex: `light.toggle`) : Exécution directe via `hass.services.async_call`.
  3. Dans tous les cas : Émission d'un événement sur le bus système Home Assistant :
     ```python
     hass.bus.async_fire("sevenhugs_button_pressed", {
         "entry_id": entry.entry_id,
         "remote_id": entry.data["remote_id"],
         "payload": data
     })
     ```
     Cela permet aux utilisateurs avancés d'utiliser n'importe quel bouton de la télécommande comme déclencheur d'automatisation dans l'éditeur de scripts de Home Assistant.

#### 4.2.6. Plateformes d'entités (`sensor.py`, `binary_sensor.py`, `button.py`, `switch.py`)
- **Classe de base `SevenhugsBaseEntity` (`entity.py`)** :
  - Hérite de `CoordinatorEntity[SevenhugsDataUpdateCoordinator]`.
  - Déclare explicitement `_attr_has_entity_name = True` (règle Bronze `has-entity-name`), assurant un nommage propre et combiné avec le nom de l'appareil dans l'interface Home Assistant.
  - Définit `_attr_device_info` liant chaque entité à l'appareil physique Sevenhugs dans le Device Registry (`manufacturer="Sevenhugs"`, `model="Smart Remote 1A (SR1A)"`).
- **Typage des entités et catégories** :
  - Distingue strictement la classe d'appareil physique (`device_class`, ex: `SensorDeviceClass.BATTERY`) de la visibilité structurelle (`_attr_entity_category = EntityCategory.DIAGNOSTIC` pour les capteurs et boutons de diagnostic interne, tels que `sensor.sevenhugs_active_screen` et `button.sevenhugs_sync_entities`).
  - Accède directement au coordinateur et au client via `entry.runtime_data.coordinator` et `entry.runtime_data.client` sans lookup de dictionnaire.

---

### 4.3. Gestion du cycle de vie matériel, mise en veille et socket Keep-Alive

#### 4.3.1. Comportement du Deep Sleep et déclencheurs de réveil (accéléromètre, dock)
La télécommande Sevenhugs est conçue comme un équipement mobile à batterie :
- **Mise en veille** : Après 30 secondes d'inactivité tactile, l'écran AMOLED s'éteint. Après 60 secondes, le Wi-Fi coupe ses connexions TCP actives pour économiser la batterie. Les sockets ouvertes sont fermées proprement avec un `FIN/ACK` ou expirent silencieusement.
- **Réveil matériel** : La télécommande se réveille instantanément dans 3 situations :
  1. Détection de mouvement par son accéléromètre interne (l'utilisateur prend la télécommande en main).
  2. Pression sur l'écran tactile capacitif.
  3. Dépôt ou retrait de la station d'accueil magnétique.
- **Rétablissement réseau** : Dès le réveil, la télécommande réassocie son Wi-Fi et émet immédiatement un paquet mDNS `_smart_remote._tcp.local.`.

#### 4.3.2. Stratégie de reconnexion paresseuse (Lazy Reconnect) et re-handshake transparent
Pour concilier l'autonomie de la télécommande et la réactivité du système :
1. **Aucun maintien artificiel de connexion** : Home Assistant ne doit **pas** envoyer de pings agressifs permanents qui empêcheraient la télécommande de s'endormir.
2. **Re-handshake `Pair-Verify` transparent** :
   Le handshake `Pair-Verify` (M1 à M4) ne requiert aucun code PIN et s'exécute en moins de 150 millisecondes. Dès que Home Assistant doit envoyer une commande ou effectuer un rafraîchissement d'état, si la socket TCP a été fermée par la télécommande durant son sommeil, le client `AsyncSmartRemoteClient` capture l'erreur `ClientConnectionError`, ouvre un nouveau socket TCP, exécute `Pair-Verify` en arrière-plan, et envoie la requête initiale sans que l'utilisateur n'ait ressenti la moindre interruption.

#### 4.3.3. Politique de journalisation non-bloquante et sans pollution de logs
Conformément aux directives de qualité de Home Assistant :
- Les échecs de connexion réseau survenant pendant la veille de la télécommande sont tracés au niveau `DEBUG` :
  `"La télécommande est en veille (socket inaccessible). Nouvel essai au prochain cycle."`
- Aucune trace de pile d'appels (`stacktrace` d'exception) n'est déversée dans les journaux système standard (`WARNING` ou `ERROR`) pour un événement normal de mise en veille.
- Les erreurs de niveau `WARNING` sont strictement réservées aux anomalies authentiques : rejet de signature cryptographique, clé corrompue, ou adresse IP modifiée devenue inaccessible pendant plus de 15 minutes consécutives.

---

### 4.4. Plan de développement jalonné (5 Phases séquentielles)

Le développement et la livraison du projet sont planifiés en 5 phases incrémentales :

```text
+----------------------------------------------------------------------------------------------------+
|                                    FEUILLE DE ROUTE D'IMPLÉMENTATION                               |
+----------------------------------------------------------------------------------------------------+

[PHASE 1] Cœur Cryptographique & Client Réseau Asynchrone
  ├── Implémentation de crypto.py (TLV8 binaire, SRP-6a 3072-bit, HKDF, Ed25519, ChaCha20-Poly1305)
  ├── Implémentation de client.py (AsyncSmartRemoteClient, TCPConnector Keep-Alive strict)
  ├── Implémentation du moteur de scripts Lua (/api/devices/sevenhugs.control.webhooks/setup)
  └── Validation par banc d'essais unitaires purs hors Home Assistant

[PHASE 2] Composant Core Home Assistant & Config Flow Graphique
  ├── Création de manifest.json, hacs.json, const.py, strings.json, translations/en.json, fr.json
  ├── Implémentation de config_flow.py (Zeroconf discovery, étape d'attente 16s, saisie PIN)
  ├── Enregistrement du matériel dans le Device Registry de Home Assistant
  └── Tests du parcours utilisateur 100% graphique avec pytest-homeassistant-custom-component

[PHASE 3] Coordinateur de Données & Plateformes d'Entités Diagnostiques
  ├── Implémentation de coordinator.py (Polling optimiste, résilience veille/réveil)
  ├── Implémentation de entity.py, sensor.py (batterie, Wi-Fi), binary_sensor.py (chargeur)
  ├── Implémentation de button.py (force_sync, backup_config)
  └── Validation de l'absence de saturation des logs lors des cycles d'endormissement

[PHASE 4] Webhooks Bidirectionnels, Automatisation Lua & Options Flow
  ├── Implémentation de webhook.py (point d'écoute sécurisé LAN, dispatch des services)
  ├── Développement de l'injecteur Server-Driven UI Lua pour configuration automatique des boutons
  ├── Implémentation de options_flow.py (sélection d'entités exposées, carrousel, options de polling)
  └── Validation des déclenchements temps réel sur des lumières et prises connectées

[PHASE 5] Intégration Multimédia & Scènes, Tests de Qualité & Packaging HACS
  ├── Implémentation du générateur de grilles 6x12 pour les lecteurs multimédia (media_player)
  ├── Implémentation de la synchronisation des scènes domotiques (/api/home-scenes)
  ├── Revue de conformité Home Assistant Quality Scale & typage mypy strict
  └── Rédaction du guide utilisateur final et publication du dépôt HACS
```

---

### 4.5. Stratégie globale de validation et tests (Unitaires, Mocks HA, Intégration, E2E)

La validation logicielle repose sur une pyramide de tests automatisés garantissant la non-régression et la robustesse de chaque composant :

```text
                  / \
                 /   \       Tests E2E sur Matériel Réel (SR1A physique)
                / E2E \      - Appairage PIN réel, test d'autonomie et veille
               /-------\
              / Intég-  \    Tests d'Intégration Asynchrones & Mocks HA
             /  ration   \   - pytest-homeassistant-custom-component, aiohttp-devtools
            /-------------\
           /   Unitaires   \ Tests Unitaires Purs (crypto.py, TLV8, SRP-6a, HKDF)
          /_________________\ - Vecteurs de test RFC 5054, RFC 7539, RFC 8032
```

#### 1. Tests Unitaires Cryptographiques (`tests/test_crypto.py`)
- **Validation TLV8** : Vérification du découpage des chaînes $> 255$ octets et du réassemblage sans perte.
- **Validation SRP-6a 3072-bit** : Validation croisée avec les vecteurs de test officiels de la RFC 5054. Vérification que le modulo $N$ et le générateur $g=5$ produisent les mêmes valeurs de preuve $M_1$ et $M_2$ que l'implémentation de référence `pair_and_verify.py`.
- **Validation ChaCha20-Poly1305** : Test de chiffrement et de déchiffrement avec le nonce fixe `\x00\x00\x00\x00HuMiThFr` et les nonces de session `PS-Msg05`, `PV-Msg02`.

#### 2. Tests d'Intégration Réseau Asynchrones (`tests/test_client.py`)
- **Simulateur de latence matérielle** : Utilisation d'un serveur mock basé sur `aiohttp.web` qui introduit une temporisation délibérée de 16 secondes sur `POST /pair-setup` (M1) afin de valider le comportement non-bloquant du client asynchrone.
- **Validation du socket Keep-Alive** : Vérification stricte que les requêtes `M1` et `M3` de `POST /pair-verify` utilisent le même descripteur de fichier socket sous-jacent.
- **Test de rupture et reconnexion transparente** : Simulation d'une fermeture brutale de socket TCP par le serveur mock au bout de 60 secondes ; vérification que le client relance automatiquement `Pair-Verify` sans générer d'exception pour l'appelant.

#### 3. Tests de Composant Home Assistant (`tests/test_config_flow.py`)
- **Simulation Zeroconf** : Injection d'un enregistrement mDNS `_smart_remote._tcp.local.` dans l'environnement de test de Home Assistant (`pytest-homeassistant-custom-component`) et validation du déclenchement du flux graphique.
- **Validation des formulaires d'erreur** : Vérification que la saisie d'un code PIN erroné renvoie le message d'erreur approprié sans quitter l'assistant de configuration.
- **Inscription au registre** : Vérification de la présence des attributs dans le Device Registry (`model`, `sw_version`, `mac`).

#### 4. Banc d'Essais End-to-End (E2E) sur Matériel Réel
- Appairage de bout en bout d'une télécommande physique Sevenhugs `SR1A` réinitialisée en usine.
- Déclenchement d'un ensemble de 5 lumières et d'une télévision connectée depuis l'écran tactile de la télécommande.
- Test de l'autonomie sur batterie sur 24 heures et validation de l'absence de réveils parasites en veille.

---

## 5. Conclusion & Synthèse des Livrables

Ce plan d'architecture et de mise en œuvre démontre que la renaissance du projet **Smart Remote Rebirth** au sein de l'écosystème Home Assistant est non seulement techniquement viable, mais qu'elle permet d'atteindre un niveau de convivialité, d'élégance et d'intégration supérieur à celui de l'application officielle originale :

1. **Option A (HACS pure)** est la seule architecture offrant une compatibilité universelle sur l'ensemble des modes d'installation de Home Assistant (HA OS, Container, Supervised, Core), tout en supprimant l'ouverture de ports inutiles et la gestion complexe de conteneurs Docker.
2. **Le parcours utilisateur 100 % graphique** guide l'utilisateur pas-à-pas lors du Config Flow en absorbant de façon fluide la contrainte matérielle des 16 secondes de calcul SRP-6a.
3. **Le modèle de communication bidirectionnel** associe les requêtes chiffrées sortantes `ChaCha20-Poly1305` sur le port 80 et les Webhooks locaux entrants sur le port 8123 pour garantir une latence d'action quasi instantanée ($< 20$ ms).
4. **La feuille de route jalonnée en 5 phases** et la stratégie de validation exhaustive garantissent une livraison robuste, modulaire et conforme aux normes les plus strictes du Home Assistant Quality Scale.
