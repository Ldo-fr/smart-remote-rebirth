#!/usr/bin/env python3
"""
Sevenhugs Smart Remote (SR1A) - Local Bridge, Configurator & Cloud Webapp Replacement
=====================================================================================
Permet de contrôler, sauvegarder et configurer localement une Sevenhugs Smart Remote
sans aucun serveur cloud (*.sevenhugs.com) et de l'intégrer à Home Assistant :

Commandes disponibles :
  python3 sevenhugs_bridge.py status
      Affiche l'inventaire complet de la télécommande (appareils, groupes, scènes, carrousel).

  python3 sevenhugs_bridge.py backup [fichier.json]
      Sauvegarde l'intégralité de la configuration déchiffrée et des interfaces personnalisées.

  python3 sevenhugs_bridge.py discover
      Lance la découverte réseau locale de la télécommande (GET /api/discover : Hue, Emulated Hue, Kodi, etc.).

  python3 sevenhugs_bridge.py custom-ui <package_id:device_id>
      Affiche la disposition des écrans/boutons et les commandes disponibles d'un appareil personnalisable.

  python3 sevenhugs_bridge.py webhooks-create --trigger "Nom|POST|http://192.168.86.X:8123/api/webhook/id|{\"key\":\"val\"}" ...
      Crée ou met à jour l'appareil Webhooks natif sur la télécommande via le moteur Lua embarqué (setup.lua).

  python3 sevenhugs_bridge.py web-wizard [--port 8080]
      Lance une interface Web locale (http://localhost:8080) qui remplace le CDN disparu
      (cdn.prod.cobra.sevenhugs.com/webapp/v2/) et permet d'exécuter tous les assistants Lua
      embarqués sur la télécommande (Webhooks, Hue, Kodi, Dummy devices, etc.) depuis un navigateur.
"""

import argparse
import http.client
import http.server
import json
import os
import secrets
import sys
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
PYLIB_DIR = os.path.join(PROJECT_ROOT, ".pylib")
if os.path.isdir(PYLIB_DIR) and PYLIB_DIR not in sys.path:
    sys.path.insert(0, PYLIB_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DEFAULT_KEYS_FILE = os.path.join(PROJECT_ROOT, "smartremote_keys.json")
if not os.path.exists(DEFAULT_KEYS_FILE) and os.path.exists(os.path.join(BASE_DIR, "smartremote_keys.json")):
    DEFAULT_KEYS_FILE = os.path.join(BASE_DIR, "smartremote_keys.json")
API_NONCE = b"\x00\x00\x00\x00HuMiThFr"


def encode_tlv(items):
    out = bytearray()
    for tag, val in items:
        if isinstance(val, int):
            val = bytes([val])
        elif isinstance(val, str):
            val = val.encode("utf-8")
        pos = 0
        while pos < len(val):
            chunk = val[pos : pos + 255]
            out.append(tag)
            out.append(len(chunk))
            out.extend(chunk)
            pos += len(chunk)
    return bytes(out)


def decode_tlv(data):
    pos = 0
    res = {}
    while pos < len(data):
        tag = data[pos]
        length = data[pos + 1]
        val = data[pos + 2 : pos + 2 + length]
        res[tag] = res.get(tag, b"") + val
        pos += 2 + length
    return res


class SmartRemoteClient:
    """Client HAP Pair-Verify + API REST chiffrée ChaCha20-Poly1305 + Moteur Lua setup.lua"""

    def __init__(self, keys_file=DEFAULT_KEYS_FILE, ip_override=None):
        with open(keys_file, "r", encoding="utf-8") as f:
            self.keys = json.load(f)
        self.keys_file = keys_file
        self.ip = ip_override or self.keys["ip"]
        self.controller_id = self.keys["controller_id"]
        self.client_sk = ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(self.keys["client_private_key"])
        )
        self.remote_pk = bytes.fromhex(self.keys["remote_public_key"])
        self.conn = None
        self.write_cipher = None
        self.read_cipher = None

    def base_headers(self, content_type=None):
        h = {
            "X-UUID": self.controller_id,
            "X-Version": "31",
            "X-Min-Version": "24",
            "Connection": "Keep-Alive",
            "User-Agent": "okhttp/3.12.0",
        }
        if content_type:
            h["Content-Type"] = content_type
        return h

    def pair_verify(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = http.client.HTTPConnection(self.ip, 80, timeout=20)

        client_eph_sk = x25519.X25519PrivateKey.generate()
        client_eph_pk = client_eph_sk.public_key().public_bytes_raw()
        m1_tlv = encode_tlv([(0x03, client_eph_pk), (0x06, 1)])

        self.conn.request(
            "POST",
            "/pair-verify",
            body=m1_tlv,
            headers=self.base_headers("application/pairing+tlv8"),
        )
        r1 = self.conn.getresponse()
        m2_raw = r1.read()
        if r1.status != 200:
            raise RuntimeError(f"Pair-Verify M1 failed ({r1.status}): {decode_tlv(m2_raw)}")

        m2 = decode_tlv(m2_raw)
        remote_eph_pk = m2[0x03]
        enc_m2 = m2[0x05]

        shared_secret = client_eph_sk.exchange(
            x25519.X25519PublicKey.from_public_bytes(remote_eph_pk)
        )
        verify_key = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=b"Pair-Verify-Encrypt-Salt",
            info=b"Pair-Verify-Encrypt-Info",
        ).derive(shared_secret)

        cipher = ChaCha20Poly1305(verify_key)
        dec_m2 = decode_tlv(cipher.decrypt(b"\x00\x00\x00\x00PV-Msg02", enc_m2, None))
        remote_id_m2 = dec_m2[0x01]
        remote_sig = dec_m2[0x0A]

        ed25519.Ed25519PublicKey.from_public_bytes(self.remote_pk).verify(
            remote_sig, remote_eph_pk + remote_id_m2 + client_eph_pk
        )

        cid_bytes = self.controller_id.encode("utf-8")
        sig = self.client_sk.sign(client_eph_pk + cid_bytes + remote_eph_pk)
        sub_m3 = encode_tlv([(0x0A, sig), (0x01, cid_bytes)])
        enc_m3 = cipher.encrypt(b"\x00\x00\x00\x00PV-Msg03", sub_m3, None)
        m3_tlv = encode_tlv([(0x05, enc_m3), (0x06, 3)])

        self.conn.request(
            "POST",
            "/pair-verify",
            body=m3_tlv,
            headers=self.base_headers("application/pairing+tlv8"),
        )
        r3 = self.conn.getresponse()
        m4_raw = r3.read()
        m4 = decode_tlv(m4_raw)
        if r3.status != 200 or m4.get(0x06) != b"\x04":
            raise RuntimeError(f"Pair-Verify M3 failed ({r3.status}): {m4}")

        write_key = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=b"Control-Salt",
            info=b"Control-Write-Encryption-Key",
        ).derive(shared_secret)
        read_key = HKDF(
            algorithm=hashes.SHA512(),
            length=32,
            salt=b"Control-Salt",
            info=b"Control-Read-Encryption-Key",
        ).derive(shared_secret)

        self.write_cipher = ChaCha20Poly1305(write_key)
        self.read_cipher = ChaCha20Poly1305(read_key)

    def api_request(self, method, path, json_body=None):
        if self.write_cipher is None:
            self.pair_verify()
        body_bytes = None
        content_type = None
        if json_body is not None:
            raw_json = json.dumps(json_body).encode("utf-8")
            body_bytes = self.write_cipher.encrypt(API_NONCE, raw_json, None)
            content_type = "application/json; charset=UTF-8"

        try:
            self.conn.request(
                method, path, body=body_bytes, headers=self.base_headers(content_type)
            )
            resp = self.conn.getresponse()
            raw = resp.read()
        except Exception:
            # Reconnect and retry once if keep-alive connection was closed
            self.pair_verify()
            if json_body is not None:
                raw_json = json.dumps(json_body).encode("utf-8")
                body_bytes = self.write_cipher.encrypt(API_NONCE, raw_json, None)
            self.conn.request(
                method, path, body=body_bytes, headers=self.base_headers(content_type)
            )
            resp = self.conn.getresponse()
            raw = resp.read()

        if resp.status == 204 or not raw:
            return resp.status, {}
        if len(raw) >= 16:
            try:
                dec = self.read_cipher.decrypt(API_NONCE, raw, None)
                text = dec.decode("utf-8", errors="replace")
                try:
                    return resp.status, json.loads(text)
                except Exception:
                    return resp.status, text
            except Exception as exc:
                if resp.status == 200:
                    raise RuntimeError(
                        f"Tag Poly1305 invalide lors du déchiffrement de {path}"
                    ) from exc
                return resp.status, raw.decode("utf-8", errors="replace")
        if resp.status == 200:
            raise RuntimeError(f"Réponse chiffrée tronquée sur {path} ({len(raw)} octets)")
        return resp.status, raw.decode("utf-8", errors="replace")

    @staticmethod
    def build_lua_payload(state, item_id=None, list_id=None, inputs=None):
        """
        Construit un payload JSON universel compatible avec tous les états de /rom/webapp/resources/setup.lua.
        """
        if state is None:
            return {}
        payload = {"state": state}
        sub = {}
        if inputs:
            for k, v in inputs.items():
                payload[k] = v
                sub[k] = v
        if item_id is not None:
            payload["id"] = item_id
            payload["item"] = item_id
            payload["item_id"] = item_id
            payload["value"] = item_id
            payload["selected"] = item_id
            payload["choice"] = item_id
            payload["method"] = item_id
            payload["target"] = item_id
            sub["id"] = item_id
        if list_id is not None and item_id is not None:
            payload[list_id] = item_id
            sub[list_id] = item_id
        if sub:
            for container in (
                "values",
                "inputs",
                "params",
                "fields",
                "form",
                "data",
                "result",
                "results",
                "selection",
                "list",
                "items",
            ):
                payload[container] = dict(sub)
        return payload


def cmd_status(client):
    print(f"[*] Connexion Pair-Verify à la Smart Remote ({client.ip})...")
    client.pair_verify()
    print("[✓] Session chiffrée établie avec succès !\n")

    _, config = client.api_request("GET", "/api/config")
    _, settings = client.api_request("GET", "/api/settings")
    _, devices = client.api_request("GET", "/api/devices")
    _, groups = client.api_request("GET", "/api/components-groups")
    _, av_scenes = client.api_request("GET", "/api/av-scenes")
    _, home_scenes = client.api_request("GET", "/api/home-scenes")

    remotes = settings.get("remotes", [{}]) if isinstance(settings, dict) else [{}]
    rem = remotes[0] if remotes else {}
    print("=== TÉLÉCOMMANDE ===")
    print(f"  ID / Série : {rem.get('id')} / {rem.get('serial_number')}")
    print(f"  MAC        : {rem.get('mac_address')}")
    print(f"  Firmware   : {rem.get('firmware_version')}")
    print(f"  Wi-Fi SSID : {rem.get('ssid')}")
    if isinstance(config, dict):
        user = config.get("user", {})
        print(f"  Propriétaire : {user.get('first_name')} ({user.get('email')})")

    print("\n=== APPAREILS CONFIGURÉS (/api/devices) ===")
    if isinstance(devices, list):
        for cat in devices:
            category = cat.get("category")
            print(f"  Catégorie : {category}")
            for d in cat.get("devices_info", []):
                cid = f"{d.get('package_id')}:{d.get('device_id')}"
                print(
                    f"    - {d.get('name'):20s} | {cid:48s} | type={d.get('device_type')} | custom_ui={d.get('customizable_ui')}"
                )

    print("\n=== GROUPES (/api/components-groups) ===")
    if isinstance(groups, list):
        for g in groups:
            print(
                f"  - {g.get('name')} (id={g.get('id')}, catégorie={g.get('category')}, {len(g.get('components', []))} appareils)"
            )

    print("\n=== CARROUSEL DE LA TÉLÉCOMMANDE (/api/settings -> carousel_order) ===")
    if isinstance(settings, dict):
        for idx, item in enumerate(settings.get("carousel_order", []), 1):
            vis = "masqué" if item.get("hidden") else "visible"
            print(
                f"  {idx:2d}. {item.get('name'):20s} ({item.get('category'):12s}) | id={item.get('id')} [{vis}]"
            )


def cmd_backup(client, outfile):
    client.pair_verify()
    endpoints = [
        "/api/config",
        "/api/devices",
        "/api/settings",
        "/api/settings/rules",
        "/api/custom/widgets",
        "/api/av-scenes",
        "/api/home-scenes",
        "/api/components-groups",
        "/api/manifests",
        "/api/homekit/pairing",
    ]
    backup = {" endpoints": {}, "custom_devices": {}}
    for ep in endpoints:
        st, data = client.api_request("GET", ep)
        backup[" endpoints"][ep] = data
        print(f"[+] GET {ep:25s} -> HTTP {st}")

    devices = backup[" endpoints"].get("/api/devices", [])
    if isinstance(devices, list):
        for cat in devices:
            for d in cat.get("devices_info", []):
                if d.get("customizable_ui"):
                    comp_id = f"{d['package_id']}:{d['device_id']}"
                    st1, ui = client.api_request("GET", f"/api/custom/{comp_id}")
                    st2, cmds = client.api_request("GET", f"/api/custom/{comp_id}/commands")
                    backup["custom_devices"][comp_id] = {"ui": ui, "commands": cmds}
                    print(f"[+] Custom UI {comp_id} -> HTTP {st1}/{st2}")

    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=2, ensure_ascii=False)
    print(f"\n[✓] Sauvegarde complète écrite dans : {outfile}")


def cmd_discover(client):
    print("[*] Lancement de la découverte réseau locale sur la Smart Remote (GET /api/discover)...")
    st, data = client.api_request("GET", "/api/discover")
    print(f"[+] Statut HTTP : {st}")
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_custom_ui(client, comp_id):
    st1, ui = client.api_request("GET", f"/api/custom/{comp_id}")
    st2, cmds = client.api_request("GET", f"/api/custom/{comp_id}/commands")
    print(f"=== LAYOUT (/api/custom/{comp_id}) [HTTP {st1}] ===")
    print(json.dumps(ui, indent=2, ensure_ascii=False))
    print(f"\n=== COMMANDES DISPONIBLES (/api/custom/{comp_id}/commands) [HTTP {st2}] ===")
    print(json.dumps(cmds, indent=2, ensure_ascii=False))


def cmd_webhooks_create(client, triggers_specs):
    """
    Automatise l'assistant Lua /api/devices/sevenhugs.control.webhooks/setup pour créer
    l'appareil Webhooks avec les déclencheurs HTTP fournis.
    Format de chaque --trigger : "Nom|METHOD|URL|BODY_OPTIONNEL"
    """
    _, manifests = client.api_request("GET", "/api/manifests")
    webhooks_manifest = next(
        m for m in manifests if m.get("id") == "sevenhugs.control.webhooks"
    )
    st, redirect_resp = client.api_request("POST", "/api/devices", webhooks_manifest)
    if st != 200 or not isinstance(redirect_resp, dict) or "url" not in redirect_resp:
        raise RuntimeError(f"Échec de l'initialisation Webhooks ({st}): {redirect_resp}")

    setup_url = redirect_resp["url"]
    if (
        not isinstance(setup_url, str)
        or not setup_url.startswith("/api/devices/")
        or not setup_url.endswith("/setup")
        or "://" in setup_url
        or ".." in setup_url
    ):
        raise RuntimeError(f"URL d'assistant Lua invalide ou non sécurisée: {setup_url!r}")
    c = http.client.HTTPConnection(client.ip, 80, timeout=15)
    c.request("GET", setup_url)
    r = c.getresponse()
    r.read()
    cookie = r.getheader("Set-Cookie", "").split(";")[0]

    def post_lua(payload):
        body = json.dumps(payload)
        c.request(
            "POST",
            setup_url,
            body=body,
            headers={"Cookie": cookie, "Content-Type": "application/json"},
        )
        resp = c.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        return resp.status, json.loads(raw)

    # Étape 1 : connect -> main_page
    post_lua({})
    _, state_obj = post_lua({"state": "main_page"})

    for spec in triggers_specs:
        parts = spec.split("|", 3)
        if len(parts) < 3:
            raise ValueError(
                f"Format --trigger invalide ({spec}). Attendu: 'Nom|METHOD|URL' ou 'Nom|METHOD|URL|BODY'"
            )
        name = parts[0].strip()
        method = parts[1].strip().lower()
        url = parts[2].strip()
        body_val = parts[3] if len(parts) > 3 else ""

        print(f"[*] Ajout du déclencheur HTTP {method.upper()} '{name}' -> {url}")
        post_lua({"state": "add_choice"})
        post_lua(
            client.build_lua_payload(
                "advanced_add_edit", item_id=method, list_id="methods_id"
            )
        )
        inputs = {"url": url, "name": name, "body": body_val}
        _, state_obj = post_lua(
            client.build_lua_payload("post_advanced_add_edit", inputs=inputs)
        )

    print("[*] Validation finale de l'assistant Webhooks (state=done)...")
    st_done, done_obj = post_lua({"state": "done"})
    print(f"[+] Réponse finale ({st_done}) : {json.dumps(done_obj, indent=2, ensure_ascii=False)}")
    c.close()


WEB_WIZARD_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sevenhugs Smart Remote - Local Setup Studio</title>
<style>
  :root {
    --bg: #0f172a;
    --card: #1e293b;
    --accent: #38bdf8;
    --text: #f8fafc;
    --muted: #94a3b8;
    --border: #334155;
  }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 24px;
    display: flex;
    gap: 24px;
    justify-content: center;
    flex-wrap: wrap;
  }
  .sidebar {
    width: 340px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    height: fit-content;
  }
  .phone-frame {
    width: 400px;
    min-height: 620px;
    background: var(--card);
    border: 2px solid var(--border);
    border-radius: 24px;
    padding: 20px;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 40px rgba(0,0,0,0.4);
  }
  h2, h3 { margin-top: 0; }
  .plugin-btn {
    width: 100%;
    text-align: left;
    background: #0f172a;
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 8px;
    cursor: pointer;
    transition: border-color 0.15s;
  }
  .plugin-btn:hover { border-color: var(--accent); }
  .plugin-btn small { color: var(--muted); display: block; font-size: 11px; }
  .wiz-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding-bottom: 12px;
    margin-bottom: 16px;
  }
  .btn {
    background: var(--accent);
    color: #0f172a;
    border: none;
    border-radius: 8px;
    padding: 10px 16px;
    font-weight: 600;
    cursor: pointer;
  }
  .btn-secondary {
    background: transparent;
    color: var(--accent);
    border: 1px solid var(--accent);
  }
  .comp-title { font-size: 20px; font-weight: 700; margin: 12px 0 8px; text-align: center; }
  .comp-text { color: var(--muted); white-space: pre-line; line-height: 1.45; margin: 8px 0; }
  .comp-list-title { font-size: 12px; color: var(--muted); font-weight: 700; margin-top: 14px; margin-bottom: 6px; }
  .list-item {
    background: #0f172a;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 8px;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .list-item:hover { border-color: var(--accent); }
  .input-group { margin-bottom: 14px; }
  .input-group label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 4px; }
  .input-group input {
    width: 100%;
    box-sizing: border-box;
    padding: 10px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: #0f172a;
    color: var(--text);
  }
  pre.raw-json {
    background: #090d16;
    padding: 10px;
    border-radius: 8px;
    font-size: 11px;
    overflow-x: auto;
    color: #a5f3fc;
    margin-top: auto;
  }
</style>
</head>
<body>
  <div class="sidebar">
    <h3>Plugins Embarqués (setup.lua)</h3>
    <p style="font-size:13px;color:var(--muted)">
      Sélectionnez un plugin pour démarrer son assistant de configuration local directement sur la Smart Remote :
    </p>
    <div id="manifests-list">Chargement...</div>
  </div>

  <div class="phone-frame">
    <div class="wiz-header">
      <button id="top-btn" class="btn btn-secondary" style="visibility:hidden">Retour</button>
      <strong id="wiz-title">Smart Remote Studio</strong>
      <span style="width:60px"></span>
    </div>
    <div id="wiz-body">
      <p class="comp-text" style="text-align:center;margin-top:80px">
        Cliquez sur <b>Webhooks</b> ou un autre plugin à gauche pour lancer l'assistant Lua embarqué sur la télécommande.
      </p>
    </div>
    <div id="wiz-footer" style="margin-top:16px;text-align:center"></div>
    <pre class="raw-json" id="raw-debug">En attente...</pre>
  </div>

<script>
const STUDIO_TOKEN = "__STUDIO_TOKEN__";
let currentPkg = null;

async function loadManifests() {
  const res = await fetch('/local-api/manifests', {
    headers: {'X-Studio-Token': STUDIO_TOKEN}
  });
  const list = await res.json();
  const container = document.getElementById('manifests-list');
  container.textContent = '';
  list.forEach(m => {
    const btn = document.createElement('button');
    btn.className = 'plugin-btn';
    const titleEl = document.createElement('strong');
    titleEl.textContent = m.name || '';
    const subEl = document.createElement('small');
    subEl.textContent = m.id || '';
    btn.appendChild(titleEl);
    btn.appendChild(subEl);
    btn.onclick = () => startWizard(m.id);
    container.appendChild(btn);
  });
}

async function startWizard(pkgId) {
  currentPkg = pkgId;
  const res = await fetch('/local-api/wizard/start', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Studio-Token': STUDIO_TOKEN
    },
    body: JSON.stringify({package_id: pkgId})
  });
  const data = await res.json();
  renderWizard(data);
}

async function sendStep(state, itemId=null, listId=null) {
  const inputs = {};
  document.querySelectorAll('[data-input-id]').forEach(el => {
    inputs[el.getAttribute('data-input-id')] = el.value;
  });
  const res = await fetch('/local-api/wizard/step', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Studio-Token': STUDIO_TOKEN
    },
    body: JSON.stringify({state, item_id: itemId, list_id: listId, inputs})
  });
  const data = await res.json();
  renderWizard(data);
}

function renderWizard(data) {
  document.getElementById('raw-debug').textContent = JSON.stringify(data, null, 2);
  document.getElementById('wiz-title').textContent = data.title || currentPkg || 'Assistant';
  const body = document.getElementById('wiz-body');
  const footer = document.getElementById('wiz-footer');
  const topBtn = document.getElementById('top-btn');
  body.textContent = '';
  footer.textContent = '';
  topBtn.style.visibility = 'hidden';

  (data.components || []).forEach(comp => {
    if (comp.type === 'title') {
      const el = document.createElement('div');
      el.className = 'comp-title';
      el.textContent = comp.text || '';
      body.appendChild(el);
    } else if (comp.type === 'text') {
      const el = document.createElement('div');
      el.className = 'comp-text';
      if (comp.align) el.style.textAlign = comp.align;
      el.textContent = comp.text || '';
      body.appendChild(el);
    } else if (comp.type === 'list_button') {
      const btn = document.createElement('button');
      btn.className = 'btn';
      btn.style.width = '100%';
      btn.style.marginTop = '12px';
      btn.textContent = comp.main || 'Continuer';
      btn.onclick = () => sendStep(comp.onclick);
      body.appendChild(btn);
    } else if (comp.type === 'list') {
      if (comp.title) {
        const t = document.createElement('div');
        t.className = 'comp-list-title';
        t.textContent = comp.title;
        body.appendChild(t);
      }
      (comp.items || []).forEach(item => {
        const row = document.createElement('div');
        row.className = 'list-item';
        const leftDiv = document.createElement('div');
        const mainStrong = document.createElement('strong');
        mainStrong.textContent = item.main || '';
        leftDiv.appendChild(mainStrong);
        if (item.detail) {
          leftDiv.appendChild(document.createElement('br'));
          const detailSmall = document.createElement('small');
          detailSmall.style.color = '#94a3b8';
          detailSmall.textContent = item.detail;
          leftDiv.appendChild(detailSmall);
        }
        const arrowSpan = document.createElement('span');
        arrowSpan.textContent = '→';
        row.appendChild(leftDiv);
        row.appendChild(arrowSpan);
        if (item.onclick) {
          row.onclick = () => sendStep(item.onclick, item.id || null, comp.id || null);
        }
        body.appendChild(row);
      });
    } else if (comp.type === 'input_text') {
      const grp = document.createElement('div');
      grp.className = 'input-group';
      const lbl = document.createElement('label');
      lbl.textContent = comp.label || comp.id || '';
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.setAttribute('data-input-id', comp.id || '');
      inp.value = comp.value || '';
      grp.appendChild(lbl);
      grp.appendChild(inp);
      body.appendChild(grp);
    } else if (comp.type === 'navigation') {
      if (comp.top_button && comp.top_button.onclick) {
        topBtn.style.visibility = 'visible';
        topBtn.textContent = comp.top_button.text || 'Retour';
        topBtn.onclick = () => sendStep(comp.top_button.onclick);
      }
      if (comp.button && comp.button.onclick) {
        const b = document.createElement('button');
        b.className = 'btn';
        b.style.width = '100%';
        b.textContent = comp.button.text || 'Suivant';
        b.onclick = () => sendStep(comp.button.onclick);
        footer.appendChild(b);
      }
    }
  });
}

loadManifests();
</script>
</body>
</html>
"""


def cmd_web_wizard(client, port, bind_address="127.0.0.1"):
    client.pair_verify()
    _, manifests = client.api_request("GET", "/api/manifests")
    session_state = {"setup_url": None, "cookie": None}
    studio_token = secrets.token_urlsafe(24)
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}", f"{bind_address}:{port}"}

    class WizardHandler(http.server.BaseHTTPRequestHandler):
        def _check_request_security(self, require_token=True):
            host_hdr = self.headers.get("Host", "")
            if host_hdr and host_hdr not in allowed_hosts:
                self.send_error(403, "Forbidden Host header")
                return False
            origin_hdr = self.headers.get("Origin", "")
            if origin_hdr:
                parsed_origin = urllib.parse.urlparse(origin_hdr)
                if parsed_origin.netloc not in allowed_hosts:
                    self.send_error(403, "Forbidden Origin header")
                    return False
            if require_token:
                req_token = self.headers.get("X-Studio-Token", "")
                if not secrets.compare_digest(req_token, studio_token):
                    self.send_error(403, "Invalid or missing X-Studio-Token")
                    return False
            return True

        def _send_json(self, code, obj):
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                if not self._check_request_security(require_token=False):
                    return
                html_page = WEB_WIZARD_HTML.replace("__STUDIO_TOKEN__", studio_token)
                raw = html_page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif self.path == "/local-api/manifests":
                if not self._check_request_security(require_token=True):
                    return
                self._send_json(200, manifests)
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._check_request_security(require_token=True):
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

            if self.path == "/local-api/wizard/start":
                pkg_id = body.get("package_id")
                manifest = next((m for m in manifests if m.get("id") == pkg_id), None)
                if not manifest:
                    self._send_json(400, {"error": f"Unknown package_id: {pkg_id!r}"})
                    return
                body_to_send = {k: v for k, v in manifest.items() if k != "discover"}
                st, redir = client.api_request("POST", "/api/devices", body_to_send)
                if st != 200 or not isinstance(redir, dict) or "url" not in redir:
                    self._send_json(400, {"error": redir})
                    return
                setup_url = redir["url"]
                if (
                    not isinstance(setup_url, str)
                    or not setup_url.startswith("/api/devices/")
                    or not setup_url.endswith("/setup")
                    or "://" in setup_url
                    or ".." in setup_url
                ):
                    self._send_json(400, {"error": f"Invalid setup_url: {setup_url!r}"})
                    return
                c = http.client.HTTPConnection(client.ip, 80, timeout=15)
                c.request("GET", setup_url)
                r = c.getresponse()
                r.read()
                cookie = r.getheader("Set-Cookie", "").split(";")[0]
                c.request(
                    "POST",
                    setup_url,
                    body="{}",
                    headers={"Cookie": cookie, "Content-Type": "application/json"},
                )
                r2 = c.getresponse()
                new_c = r2.getheader("Set-Cookie")
                if new_c:
                    cookie = new_c.split(";")[0]
                step1 = json.loads(r2.read().decode("utf-8", errors="replace"))
                c.close()
                session_state["setup_url"] = setup_url
                session_state["cookie"] = cookie
                self._send_json(200, step1)

            elif self.path == "/local-api/wizard/step":
                setup_url = session_state["setup_url"]
                cookie = session_state["cookie"]
                if not setup_url:
                    self._send_json(400, {"error": "No active wizard session"})
                    return
                payload = client.build_lua_payload(
                    body.get("state"),
                    item_id=body.get("item_id"),
                    list_id=body.get("list_id"),
                    inputs=body.get("inputs"),
                )
                c = http.client.HTTPConnection(client.ip, 80, timeout=15)
                c.request(
                    "POST",
                    setup_url,
                    body=json.dumps(payload),
                    headers={"Cookie": cookie, "Content-Type": "application/json"},
                )
                r = c.getresponse()
                new_c = r.getheader("Set-Cookie")
                if new_c:
                    session_state["cookie"] = new_c.split(";")[0]
                resp_json = json.loads(r.read().decode("utf-8", errors="replace"))
                c.close()
                self._send_json(200, resp_json)
            else:
                self.send_error(404)

    server = http.server.HTTPServer((bind_address, port), WizardHandler)
    print(f"[✓] Studio Web Sevenhugs démarré sur http://{bind_address}:{port}")
    print("    Ouvrez cette adresse dans votre navigateur pour utiliser les assistants Lua de la télécommande.")
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(
        description="Sevenhugs Smart Remote - Pont local & Configurateur Home Assistant"
    )
    parser.add_argument(
        "--keys",
        default=DEFAULT_KEYS_FILE,
        help="Chemin vers smartremote_keys.json",
    )
    parser.add_argument(
        "--ip",
        default=None,
        help="Adresse IP locale de la Smart Remote (surcharge la valeur de smartremote_keys.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pair = sub.add_parser(
        "pair",
        help="Jumelage initial (Pair-Setup SRP-6a + Pair-Verify) et génération de smartremote_keys.json",
    )
    p_pair.add_argument(
        "--remote-ip",
        default=None,
        help="Adresse IP locale de la Smart Remote à jumeler",
    )

    sub.add_parser("status", help="Affiche l'état complet de la télécommande")

    p_backup = sub.add_parser("backup", help="Sauvegarde toute la configuration JSON déchiffrée")
    p_backup.add_argument(
        "outfile",
        nargs="?",
        default=os.path.join(BASE_DIR, "smartremote_full_backup.json"),
    )

    sub.add_parser("discover", help="Lance GET /api/discover sur la télécommande")

    p_custom = sub.add_parser(
        "custom-ui", help="Inspecte l'interface personnalisée et les commandes d'un appareil"
    )
    p_custom.add_argument(
        "component_id",
        help="Identifiant au format package_id:device_id (ex: sevenhugs.control.infrared:SOLO5_M3708_fff1980f)",
    )

    p_wh = sub.add_parser(
        "webhooks-create",
        help="Crée l'appareil Webhooks sur la télécommande avec des déclencheurs HTTP vers Home Assistant",
    )
    p_wh.add_argument(
        "--trigger",
        action="append",
        required=True,
        help="Format: 'Nom|METHOD|URL|BODY_OPTIONNEL' (répétable)",
    )

    p_web = sub.add_parser(
        "web-wizard",
        help="Lance le serveur Web local remplaçant cdn.prod.cobra.sevenhugs.com",
    )
    p_web.add_argument("--port", type=int, default=8080, help="Port HTTP local (défaut: 8080)")
    p_web.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Adresse IP d'écoute (défaut sécurisé: 127.0.0.1)",
    )

    args = parser.parse_args()

    if args.command == "pair":
        from pair_and_verify import run_pair_and_verify
        target_ip = args.remote_ip or args.ip
        if not target_ip:
            parser.error("La commande 'pair' nécessite --ip <IP> ou --remote-ip <IP>.")
        run_pair_and_verify(target_ip, args.keys)
        return

    client = SmartRemoteClient(args.keys, ip_override=args.ip)

    if args.command == "status":
        cmd_status(client)
    elif args.command == "backup":
        cmd_backup(client, args.outfile)
    elif args.command == "discover":
        cmd_discover(client)
    elif args.command == "custom-ui":
        cmd_custom_ui(client, args.component_id)
    elif args.command == "webhooks-create":
        cmd_webhooks_create(client, args.trigger)
    elif args.command == "web-wizard":
        cmd_web_wizard(client, args.port, bind_address=args.bind)


if __name__ == "__main__":
    main()

