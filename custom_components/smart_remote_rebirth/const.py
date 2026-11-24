"""Constantes pour l'intégration Smart Remote Rebirth."""

from __future__ import annotations

DOMAIN = "smart_remote_rebirth"

# Clés de configuration de la ConfigEntry
CONF_HOST = "host"
CONF_PORT = "port"
CONF_SERIAL_NUMBER = "serial_number"
CONF_CONTROLLER_ID = "controller_id"
CONF_CLIENT_PRIVATE_KEY = "client_private_key"
CONF_CLIENT_PUBLIC_KEY = "client_public_key"
CONF_SECRET_KEY = "client_private_key"
CONF_PUBLIC_KEY = "client_public_key"
CONF_REMOTE_ID = "remote_id"
CONF_REMOTE_PUBLIC_KEY = "remote_public_key"
CONF_SESSION_WRITE_KEY = "session_write_key"
CONF_SESSION_READ_KEY = "session_read_key"
CONF_API_VERSION = "api_version"
CONF_MIN_API_VERSION = "min_api_version"
CONF_KEYS = "keys"

# Versions d'API HTTP par défaut (firmware 0.76.4) — négociées dynamiquement si différent
DEFAULT_API_VERSION = "31"
DEFAULT_MIN_API_VERSION = "24"

# Clés et valeurs par défaut pour les options (R1 : nominal 60s)
CONF_POLLING_INTERVAL = "polling_interval"
DEFAULT_POLLING_INTERVAL = 60
MIN_POLLING_INTERVAL = 15
MAX_POLLING_INTERVAL = 300

# Résilience veille/réveil (R1 : tolérance de 3 échecs consécutifs)
SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 3

CONF_AUTO_SYNC_WEBHOOKS = "auto_sync_webhooks"
DEFAULT_AUTO_SYNC_WEBHOOKS = True

CONF_TIMEOUT = "timeout"
DEFAULT_TIMEOUT = 10

# Options avancées et entités exposées (Phase 4 / R3 & Jalon v0.6)
CONF_ENTITIES = "entities"
CONF_DELETE_DEVICES = "delete_devices"
CONF_TRIGGER_RESYNC = "trigger_resync"
CONF_CAROUSEL_ORDER = "carousel_order"
CONF_WEBHOOK_SCREEN_NAME = "webhook_screen_name"
CONF_PIN_HA_FIRST = "pin_ha_first"
CONF_CUSTOMIZE_DEVICE = "customize_device"
DEFAULT_WEBHOOK_SCREEN_NAME = "Home Assistant"
DEFAULT_PIN_HA_FIRST = True

# Services Home Assistant (Jalon v0.6)
SERVICE_SET_CAROUSEL_ORDER = "set_carousel_order"
SERVICE_SET_CUSTOM_UI_LAYOUT = "set_custom_ui_layout"
SERVICE_RESTORE_CUSTOM_UI_LAYOUT = "restore_custom_ui_layout"
DEFAULT_EXPOSED_DOMAINS: list[str] = [
    "light",
    "switch",
    "scene",
    "script",
    "button",
    "cover",
    "fan",
    "media_player",
    "input_boolean",
    "automation",
]
DEFAULT_ENTITIES: list[str] = []
DEFAULT_TRIGGER_RESYNC: bool = False


# Webhooks et événements (Phase 4 / R1 & R2)
EVENT_BUTTON_PRESSED = "smart_remote_button_pressed"
EVENT_SEVENHUGS_BUTTON_PRESSED = "smart_remote_button_pressed"  # Alias rétro-compatible
WEBHOOK_ID_PREFIX = "smart_remote_"

# Ports et services réseau
DEFAULT_PORT = 80
ZEROCONF_SERVICE_TYPE = "_smart_remote._tcp.local."

# Délais et temporisations réseau
TIMEOUT_CONNECT_CHECK = 3.0       # Test préliminaire port 80 / GET /api/hello
TIMEOUT_PAIR_SETUP_M1 = 60.0      # Délai de calcul matériel SRP-6a (~16s)
TIMEOUT_PAIR_SETUP_FINISH = 30.0  # Échange M3-M6
TIMEOUT_PAIR_VERIFY = 15.0        # Handshake Pair-Verify Keep-Alive
TIMEOUT_HTTP_REQUEST = 15.0       # Requêtes HTTP chiffrées standard

# Informations matérielles & Device Registry
MANUFACTURER = "Sevenhugs"
MODEL_SR1A = "SR1A"
MODEL = "SR1A"
DEFAULT_MODEL = "SR1A"
HARDWARE_VERSION = "FCC ID: 2AEVC-SR1A"
DEFAULT_NAME = "Smart Remote"

# Codes d'erreurs du Config Flow
ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_TIMEOUT_CONNECT = "timeout_connect"
ERROR_REMOTE_BUSY = "remote_busy"
ERROR_INVALID_PIN = "invalid_pin"
ERROR_PAIRING_FAILED = "pairing_failed"
ERROR_ALREADY_CONFIGURED = "already_configured"
ERROR_UNKNOWN = "unknown"

# Plateformes supportées (FR-3 : binary_sensor et button uniquement)
try:
    from homeassistant.const import Platform

    PLATFORMS: list[Platform] = [
        Platform.BINARY_SENSOR,
        Platform.BUTTON,
    ]
except ImportError:
    PLATFORMS: list[str] = [  # type: ignore[no-redef]
        "binary_sensor",
        "button",
    ]
