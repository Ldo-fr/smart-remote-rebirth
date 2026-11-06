# Sevenhugs Smart Remote Rebirth (`SR1A`)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom%20Repository-orange.svg)](https://hacs.xyz/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1%2B%20%7C%202026.11%2B-blue.svg)](https://www.home-assistant.io/)
[![Version](https://img.shields.io/badge/version-0.1.0--alpha-yellow.svg)](https://github.com/Ldo-fr/smart-remote-rebirth/releases)
[![Build & Tests](https://img.shields.io/badge/tests-330%20passed%20(100%25)-brightgreen.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Intégration native **Home Assistant** (compatible HACS / Marketplace) et boîte à outils locale pour la télécommande universelle **Sevenhugs Smart Remote** (modèle `SR1A`, FCC ID `2AEVC-SR1A`).

---

> [!WARNING]
> ### ⚠️ PROJET EN VERSION DE TRAVAIL (ALPHA / EXPÉRIMENTAL)
> **Ce logiciel est une version de travail préliminaire fournie à des fins de test et de recherche communautaire.**
> - **Non destiné à la production :** Des instabilités, déconnexions ou changements de structure peuvent survenir au fil des mises à jour. Ne comptez pas sur cette intégration pour des systèmes d'alarme ou de sécurité critiques.
> - **Rétro-ingénierie indépendante :** Ce projet n'est ni affilié, ni soutenu par la société Sevenhugs (dont les serveurs cloud ont été définitivement arrêtés en mai 2022).
> - **Aucun cloud requis :** Toutes les communications s'effectuent à **100 % en local** sur votre réseau Wi-Fi (chiffrement SRP-6a, Ed25519, ChaCha20-Poly1305).

> [!NOTE]
> ### 💡 Démarche du projet & Clause de non-affiliation
> **Je n'ai absolument rien à voir avec l'ancienne société Sevenhugs (Sevenhugs SAS) ou ses ayants droit.**
> Ce projet est une démarche personnelle, indépendante et bénévole de passionné. La Sevenhugs Smart Remote est un appareil matériel d'excellente facture qui s'est retrouvé prématurément privé de configuration suite à l'arrêt définitif des serveurs cloud en mai 2022.
> Mon unique objectif est de **redonner vie à un produit qui n'est plus supporté**, de sauver ces télécommandes de l'obsolescence programmée et d'éviter qu'elles ne finissent comme déchets électroniques au fond d'un tiroir, en leur offrant une seconde vie moderne et 100 % locale grâce à Home Assistant.

---

## 📊 État d'avancement des fonctionnalités

| Fonctionnalité | Statut | Description |
| :--- | :---: | :--- |
| **Appairage local graphique (Config Flow)** | ✅ Opérationnel | Découverte automatique mDNS (`_smart_remote._tcp.local.`), saisie du code PIN 8 chiffres dans l'UI Home Assistant, calcul SRP-6a non-bloquant. |
| **Télémétrie en temps réel (Capteurs)** | ✅ Opérationnel | Capteurs de niveau de batterie (`%`), signal Wi-Fi / RSSI (`dBm`), état sur le dock de charge (`binary_sensor`). |
| **Contrôle bidirectionnel (Webhooks locaux)** | ✅ Opérationnel | Réception sécurisée (`local_only=True`) des appuis touches sur l'écran tactile et déclenchement instantané des services HA (`light`, `switch`, `scene`). |
| **Événements d'automatisation** | ✅ Opérationnel | Émission systématique de l'événement `sevenhugs_button_pressed` sur le bus d'événements Home Assistant. |
| **Gestion de la veille (Deep Sleep)** | ✅ Opérationnel | Tolérance aux micro-coupures Wi-Fi lors de l'endormissement de la télécommande et reconnexion transparente lors de la reprise en main. |
| **Sélection d'entités (Options Flow)** | ✅ Opérationnel | Interface graphique pour sélectionner les appareils et scènes exposés à la télécommande. |
| **Injecteur d'assistants Lua** | ✅ Opérationnel | Automatisation de la configuration des boutons via le serveur embarqué de la télécommande. |
| **Grilles multimédia avancées (6×12)** | ⏳ En cours | Interface dédiée avec contrôle du volume, transport et navigation pour les `media_player`. |
| **Synchronisation de scènes complexes** | ⏳ En cours | Mapping avancé des scènes d'ambiance multi-pièces. |

---

## 🚀 Installation dans Home Assistant

### Méthode 1 : Via HACS / Marketplace (Recommandé)

1. Assurez-vous que **HACS** (ou le nouveau menu **Marketplace** de Home Assistant 2026.11+) est installé.
2. Ouvrez **HACS / Marketplace** dans la barre latérale de Home Assistant.
3. Cliquez sur le menu en haut à droite (trois petits points) > **Dépôts personnalisés** (*Custom repositories*).
4. Renseignez :
   - **Dépôt** : `https://github.com/Ldo-fr/smart-remote-rebirth`
   - **Type** : `Intégration`
5. Cliquez sur **Ajouter**, puis recherchez **Sevenhugs Smart Remote**.
6. Cliquez sur **Télécharger**, puis **redémarrez Home Assistant**.

---

### Méthode 2 : Installation manuelle

1. Téléchargez ou clonez ce dépôt :
   ```bash
   git clone https://github.com/Ldo-fr/smart-remote-rebirth.git
   ```
2. Copiez le dossier `custom_components/sevenhugs_smart_remote` dans le répertoire `config/custom_components/` de votre Home Assistant :
   ```bash
   cp -r smart-remote-rebirth/custom_components/sevenhugs_smart_remote /chemin/vers/votre/config/custom_components/
   ```
3. **Redémarrez Home Assistant**.

---

## ⚙️ Configuration & Appairage

Une fois l'intégration installée et Home Assistant redémarré :

1. Allez dans **Paramètres > Appareils et Services > Ajouter une intégration**.
2. Recherchez **Sevenhugs Smart Remote**.
3. **Appairage assisté** :
   - Si votre télécommande est allumée et connectée au même Wi-Fi, elle est **détectée automatiquement** via mDNS. Sinon, entrez simplement son adresse IP locale.
   - Cliquez sur **Suivant**. L'intégration initialise le calcul d'empreinte matérielle SRP-6a (~16 secondes).
   - Un **code PIN à 8 chiffres** (`XXX-XX-XXX`) s'affiche sur l'écran AMOLED de la Smart Remote.
   - Saisissez ce code dans la fenêtre Home Assistant et validez.
4. **C'est prêt !** Votre télécommande apparaît comme un appareil avec ses entités (niveau de batterie, Wi-Fi, chargeur, et boutons d'action).

> [!TIP]
> Une fois appairée, les clés cryptographiques de session sont conservées de manière sécurisée dans Home Assistant. Vous n'aurez **plus jamais besoin de ressaisir de code PIN**, même après un redémarrage de votre serveur ou de la télécommande.

---

## 🧪 Tester l'intégration localement (POC interactif)

Le dépôt inclut un script de démonstration autonome ne nécessitant aucune dépendance complexe :
```bash
python3 poc_quickstart.py
```
Ce script démarre un serveur Sevenhugs virtuel, initialise le composant Home Assistant, lit la télémétrie de batterie et simule un appui tactile avec allumage d'une ampoule HA en moins d'une seconde.

*(Vous pouvez également tester directement avec une télécommande physique connectée : `python3 poc_quickstart.py --ip 192.168.1.XX`).*

---

## 🛠️ Outils historiques en ligne de commande (CLI & Rétro-ingénierie)

Pour les développeurs et passionnés souhaitant manipuler directement l'API de la télécommande hors Home Assistant, le dépôt conserve les scripts d'exploration d'origine :

- [**`pair_and_verify.py`**](pair_and_verify.py) : Script autonome de jumelage initial en ligne de commande.
- [**`pair_verify.py`**](pair_verify.py) : Test rapide de session chiffrée et extraction des dumps d'état.
- [**`sevenhugs_bridge.py`**](sevenhugs_bridge.py) : Passerelle et studio Web local de configuration.
- [**`docs/PROTOCOL_AND_API.md`**](docs/PROTOCOL_AND_API.md) : Spécification complète et documentée du protocole (SRP-6a 3072-bit, Ed25519, X25519, ChaCha20-Poly1305, endpoints `/api/*` et assistants Lua `/rom/webapp/resources/setup.lua`).
- [**`docs/HOME_ASSISTANT_INTEGRATION_PLAN.md`**](docs/HOME_ASSISTANT_INTEGRATION_PLAN.md) : Plan d'architecture technique et feuille de route détaillée du composant Home Assistant.

---

## 🔄 Workflow de développement & Automatisation des Releases

Le dépôt utilise **GitHub Actions** pour automatiser les tests et la publication des versions :

1. **Développement sur branche** : Créez une branche (`feature/mon-ajout` ou `fix/mon-correctif`).
2. **Pull Request & Validation CI** : À l'ouverture ou modification d'une Pull Request vers `main`, le workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml) vérifie automatiquement :
   - Le format du titre de la PR selon Conventional Commits (`amannn/action-semantic-pull-request`).
   - La qualité et la syntaxe du code Python avec le linter officiel [`Ruff`](https://docs.astral.sh/ruff/).
   - L'ensemble des 330 tests unitaires et d'intégration (`python -m unittest discover tests`).
   - La conformité officielle Home Assistant ([`hassfest`](https://github.com/home-assistant/actions)).
   - La validation de structure HACS / Marketplace ([`hacs/action`](https://github.com/hacs/action)).
3. **Merge sur `main` & Release automatique** :
   Lors de la fusion de la PR sur `main`, le workflow [`.github/workflows/release.yml`](.github/workflows/release.yml) :
   - Calcule automatiquement le nouveau numéro de version selon **Conventional Commits** :
     - `fix: ...` ➔ incrémentation **Patch** (`0.1.0` ➔ `0.1.1`)
     - `feat: ...` ➔ incrémentation **Minor** (`0.1.0` ➔ `0.2.0`)
     - `feat!: ...` ou `BREAKING CHANGE:` ➔ incrémentation **Major** (`0.X.Y` ➔ `1.0.0`)
   - Met à jour le numéro de version dans `custom_components/sevenhugs_smart_remote/manifest.json`.
   - Pose le tag Git `vX.Y.Z`.
   - Publie la **GitHub Release** avec les notes de version automatiques et l'archive `sevenhugs_smart_remote.zip`.
   - Marque automatiquement la release en **Pre-release** tant que la version commence par `0.X.Y`.

---

## 🤝 Contribution & Signalement de bugs

Les contributions sont les bienvenues ! N'hésitez pas à :
1. Ouvrir une **Issue** pour signaler un comportement anormal ou suggérer une amélioration.
2. Soumettre une **Pull Request** pour enrichir le support des entités ou affiner la documentation.

---

## 📄 Licence

Ce projet est distribué sous licence libre **MIT**. Consultez le fichier [LICENSE](LICENSE) pour plus d'informations.
