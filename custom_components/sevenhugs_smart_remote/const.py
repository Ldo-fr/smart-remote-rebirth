"""Constantes pour l'intégration Sevenhugs Smart Remote."""

from __future__ import annotations

DOMAIN = "sevenhugs_smart_remote"

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
CONF_KEYS = "keys"

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

# Options avancées et entités exposées (Phase 4 / R3)
CONF_ENTITIES = "entities"
CONF_TRIGGER_RESYNC = "trigger_resync"
DEFAULT_EXPOSED_DOMAINS: list[str] = [
    "light",
    "switch",
    "scene",
    "media_player",
]
DEFAULT_ENTITIES: list[str] = []
DEFAULT_TRIGGER_RESYNC: bool = False


# Webhooks et événements (Phase 4 / R1 & R2)
EVENT_SEVENHUGS_BUTTON_PRESSED = "sevenhugs_button_pressed"
WEBHOOK_ID_PREFIX = "sevenhugs_"

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
MODEL_SR1A = "Smart Remote 1A (SR1A)"
MODEL = "Smart Remote 1A (SR1A)"
DEFAULT_MODEL = "SR1A"
HARDWARE_VERSION = "FCC ID: 2AEVC-SR1A"
DEFAULT_NAME = "Sevenhugs Smart Remote"

# Codes d'erreurs du Config Flow
ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_TIMEOUT_CONNECT = "timeout_connect"
ERROR_REMOTE_BUSY = "remote_busy"
ERROR_INVALID_PIN = "invalid_pin"
ERROR_PAIRING_FAILED = "pairing_failed"
ERROR_ALREADY_CONFIGURED = "already_configured"
ERROR_UNKNOWN = "unknown"

# Plateformes supportées (Milestone 3 / Requirement R4)
try:
    from homeassistant.const import Platform

    PLATFORMS: list[Platform] = [
        Platform.SENSOR,
        Platform.BINARY_SENSOR,
        Platform.BUTTON,
    ]
except ImportError:
    PLATFORMS: list[str] = [  # type: ignore[no-redef]
        "sensor",
        "binary_sensor",
        "button",
    ]
