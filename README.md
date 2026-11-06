# Sevenhugs Smart Remote Rebirth (`SR1A`)

**🇬🇧 English** | [🇫🇷 Français](README.fr.md)

---

[![hacs_badge](https://img.shields.io/badge/HACS-Custom%20Repository-orange.svg)](https://hacs.xyz/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1%2B%20%7C%202026.11%2B-blue.svg)](https://www.home-assistant.io/)
[![Version](https://img.shields.io/github/v/release/Ldo-fr/smart-remote-rebirth?include_prereleases&color=yellow&label=version)](https://github.com/Ldo-fr/smart-remote-rebirth/releases)
[![Build & Tests](https://img.shields.io/badge/tests-330%20passed%20(100%25)-brightgreen.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Native **Home Assistant** integration (compatible with HACS / Marketplace) and local reverse-engineering toolkit for the **Sevenhugs Smart Remote** universal remote control (model `SR1A`, FCC ID `2AEVC-SR1A`).

---

> [!WARNING]
> ### ⚠️ WORK-IN-PROGRESS PROJECT (ALPHA / EXPERIMENTAL)
> **This software is a preliminary work-in-progress provided for testing and community research purposes.**
> - **Not intended for production:** Instabilities, temporary disconnects, or structure adjustments may occur across updates. Do not rely on this integration for critical alarm or security systems.
> - **Independent reverse-engineering:** This project is neither affiliated with nor endorsed by Sevenhugs Inc. / SAS (whose cloud servers were permanently decommissioned in May 2022).
> - **100% local, no cloud required:** All communications take place strictly **locally over your Wi-Fi network** (SRP-6a, Ed25519, ChaCha20-Poly1305 encryption).

> [!NOTE]
> ### 🎯 Project Scope & Purpose
> **The goal of this project is not to restore 100% of the original features.**
> Proprietary cloud-dependent features — such as the online infrared code database, the "Point Mode" room beacons (UWB), the weather widget, or native legacy cloud integrations — are not being recreated.
> The primary objective is to **repurpose the remote as a 100% local touch controller**: leverage its display and touchscreen to trigger **local webhooks** and effortlessly drive your **Home Assistant** entities, scenes, and automations.

> [!NOTE]
> ### 💡 Project Motivation & Non-Affiliation Disclaimer
> **I have absolutely no affiliation with the former Sevenhugs company (Sevenhugs SAS) or its successors.**
> This project is an independent, non-profit personal initiative driven by hobbyist passion. The Sevenhugs Smart Remote is a beautifully engineered piece of hardware that became prematurely unusable following the permanent shutdown of its original cloud servers in May 2022.
> My sole purpose is to **bring a discontinued product back to life**, save these remotes from planned obsolescence and prevent them from ending up as electronic waste in drawers, giving them a modern, 100% local second life powered by Home Assistant.

---

## 📊 Feature Status & Roadmap

| Feature | Status | Description |
| :--- | :---: | :--- |
| **Local Graphical Pairing (Config Flow)** | ✅ Operational | Automatic mDNS discovery (`_smart_remote._tcp.local.`), 8-digit PIN entry in Home Assistant UI, non-blocking asynchronous SRP-6a computation. |
| **Status & Connectivity (Diagnostics)** | ✅ Operational | Connection status tracking (online / deep sleep / disconnected) via periodic local polling and hardware metadata inspection (firmware, MAC, serial). |
| **Bidirectional Control (Local Webhooks)** | ✅ Operational | Secure (`local_only=True`) touch screen button presses reception triggering instantaneous HA services (`light`, `switch`, `scene`, `media_player`, etc.). |
| **Automation Events** | ✅ Operational | Systematic firing of the `smart_remote_button_pressed` event on the Home Assistant event bus. |
| **Deep Sleep Management** | ✅ Operational | Tolerance to Wi-Fi sleep cycles when the remote sleeps and seamless keep-alive reconnection when picked up. |
| **Entity Management & Cleanup (Options Flow)** | ✅ Operational | Graphical UI in Home Assistant to select exposed entities and permanently delete legacy carousel devices/groups. |
| **Lua Assistant Injector** | ✅ Operational | Automated webhook button provisioning pushed directly to the remote's embedded HTTP server. |
| **Maintenance Buttons** | ✅ Operational | Dedicated buttons in Home Assistant to force telemetry sync, ping the remote, and backup configuration dumps. |
| **Native Rich UI Injection (`sevenhugs.ui.light` / `sevenhugs.ui.media`)** | 💡 Planned Later | Exploratory research track to leverage the firmware's built-in UI templates (dimmer sliders, color wheels, dedicated media controls). |

---

## 🚀 Installation in Home Assistant

### Method 1: Via HACS / Marketplace (Recommended)

1. Ensure **HACS** (or the new **Marketplace** menu in Home Assistant 2026.11+) is installed.
2. Open **HACS / Marketplace** in the Home Assistant sidebar.
3. Click the top-right menu (three dots) > **Custom repositories**.
4. Fill in:
   - **Repository**: `https://github.com/Ldo-fr/smart-remote-rebirth`
   - **Type**: `Integration`
5. Click **Add**, then search for **Smart Remote Rebirth**.
6. Click **Download**, then **restart Home Assistant**.

---

### Method 2: Manual Installation

1. Download or clone this repository:
   ```bash
   git clone https://github.com/Ldo-fr/smart-remote-rebirth.git
   ```
2. Copy the `custom_components/smart_remote_rebirth` folder into your Home Assistant's `config/custom_components/` directory:
   ```bash
   cp -r smart-remote-rebirth/custom_components/smart_remote_rebirth /path/to/your/config/custom_components/
   ```
3. **Restart Home Assistant**.

---

## ⚙️ Configuration & Pairing

Once the integration is installed and Home Assistant has restarted:

1. Navigate to **Settings > Devices & Services > Add Integration**.
2. Search for **Smart Remote Rebirth**.
3. **Guided Pairing**:
   - If your remote is powered on and connected to the same Wi-Fi network, it will be **discovered automatically** via mDNS. Otherwise, manually enter its local IP address.
   - Click **Next**. The integration initiates the SRP-6a hardware calculation (~16 seconds).
   - An **8-digit PIN code** (`XXX-XX-XXX`) will appear on the touchscreen of your Smart Remote.
   - Enter this PIN in Home Assistant and validate promptly. *(Note: The remote displays the PIN for about 30 to 45 seconds before putting its screen to sleep. If the screen turns off or the code is rejected, the remote cancels the session: close the dialog and restart pairing to generate a new PIN).*
4. **All set!** Your remote appears as a device with all its entities (battery, Wi-Fi, charging dock, diagnostic sensors, and action buttons).

> [!TIP]
> Once paired, cryptographic session keys are securely stored in Home Assistant. You will **never need to re-enter a PIN code**, even after restarting Home Assistant or rebooting the remote control.

---

## 🧪 Testing the Integration Locally (Interactive Quickstart POC)

The repository includes a standalone demonstration script requiring no external setup:
```bash
python3 tools/poc_quickstart.py
```
This script boots a mock Sevenhugs server, initializes the Home Assistant component, reads battery telemetry, and simulates a touchscreen button press that turns on a virtual Home Assistant light in under one second.

*(You can also test directly with a physical remote on your network: `python3 tools/poc_quickstart.py --ip 192.168.1.XX`).*

---

## 🛠️ Historical CLI & Reverse-Engineering Tools

For developers and enthusiasts wishing to interact directly with the remote's raw API outside Home Assistant, the repository preserves the original exploration tools in the `tools/` directory:

- [**`tools/pair_and_verify.py`**](tools/pair_and_verify.py): Standalone CLI script for initial pairing and cryptographic handshake.
- [**`tools/pair_verify.py`**](tools/pair_verify.py): Quick encrypted session check and state dumps extractor.
- [**`tools/sevenhugs_bridge.py`**](tools/sevenhugs_bridge.py): Local Web gateway and configuration studio.
- [**`docs/PROTOCOL_AND_API.md`**](docs/PROTOCOL_AND_API.md): Full documented protocol specification (SRP-6a 3072-bit, Ed25519, X25519, ChaCha20-Poly1305, `/api/*` endpoints, and `/rom/webapp/resources/setup.lua` assistants).
- [**`docs/HOME_ASSISTANT_INTEGRATION_PLAN.md`**](docs/HOME_ASSISTANT_INTEGRATION_PLAN.md): Technical architecture and roadmap document.

---

## 🤝 Contributing & Bug Reports

Contributions are warmly welcomed! Feel free to:
1. Open an **Issue** to report unexpected behavior, disconnects, or suggest an improvement.
2. Submit a **Pull Request** to expand entity support or enhance the documentation.

---

## 📄 License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for details.
