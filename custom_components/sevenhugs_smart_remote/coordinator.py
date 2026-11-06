"""Coordinateur de données pour Sevenhugs Smart Remote (SR1A).

Gère le polling optimiste périodique (60s), la résilience veille avec tolérance
de 3 échecs consécutifs, la mise en cache locale de télémétrie, et le réveil événementiel.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    AsyncSmartRemoteClient,
    SevenhugsAuthError,
    SevenhugsConnectionError,
)
from .const import (
    CONF_POLLING_INTERVAL,
    CONF_REMOTE_ID,
    CONF_SERIAL_NUMBER,
    DEFAULT_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Paramètres normatifs du coordinateur (Phase 3)
DEFAULT_COORDINATOR_UPDATE_INTERVAL = 60
SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 3

# Tuple d'erreurs réseau tolérées lors des cycles de veille
try:
    import aiohttp
    _NETWORK_ERRORS: tuple[type[Exception], ...] = (
        SevenhugsConnectionError,
        TimeoutError,
        asyncio.TimeoutError,
        ConnectionError,
        OSError,
        aiohttp.ClientError,
    )
except ImportError:
    _NETWORK_ERRORS = (
        SevenhugsConnectionError,
        TimeoutError,
        asyncio.TimeoutError,
        ConnectionError,
        OSError,
    )


class SevenhugsDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinateur de mise à jour des données pour Sevenhugs Smart Remote."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AsyncSmartRemoteClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise le coordinateur de données."""
        self.client = client
        self.entry = entry

        # Intervalle nominal de 60s configurable via options
        polling_interval = entry.options.get(
            CONF_POLLING_INTERVAL,
            getattr(entry, "data", {}).get(CONF_POLLING_INTERVAL, DEFAULT_COORDINATOR_UPDATE_INTERVAL),
        )
        if not isinstance(polling_interval, (int, float)) or polling_interval <= 0:
            polling_interval = DEFAULT_COORDINATOR_UPDATE_INTERVAL

        unique_id = entry.unique_id or getattr(entry, "data", {}).get(CONF_SERIAL_NUMBER, entry.entry_id)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{unique_id}",
            update_interval=timedelta(seconds=polling_interval),
        )

        # Cache d'état local et compteur d'échecs consécutifs
        self._cached_data: dict[str, Any] = {}
        self._consecutive_failures: int = 0

    # =========================================================================
    # Cycle de mise à jour et résilience de veille
    # =========================================================================

    async def _async_update_data(self) -> dict[str, Any]:
        """Interroge la télécommande et met à jour le cache de télémétrie."""
        try:
            raw_data = await self.client.async_request("GET", "/api/settings")
            if not isinstance(raw_data, dict):
                _LOGGER.debug("Réponse /api/settings non-dictionnaire reçue: %s", raw_data)
                raw_data = {}

            # Extraction et normalisation des informations matérielles
            serial_num = (
                raw_data.get("serial_number")
                or self.entry.unique_id
                or self.entry.data.get(CONF_SERIAL_NUMBER, "unknown")
            )
            remotes = raw_data.get("remotes")
            first_remote = remotes[0] if isinstance(remotes, list) and remotes else {}

            mac_addr = (
                raw_data.get("mac_address")
                or first_remote.get("mac_address")
                or self.entry.data.get("mac_address")
            )
            fw_ver = (
                raw_data.get("firmware_version")
                or first_remote.get("firmware_version")
                or self.entry.data.get("firmware_version", "v0.76.4")
            )

            # Batterie (0..100)
            raw_battery = raw_data.get("battery", self._cached_data.get("battery", 100))
            try:
                battery = max(0, min(100, int(raw_battery)))
            except (TypeError, ValueError):
                battery = self._cached_data.get("battery", 100)

            # Signal Wi-Fi / RSSI
            raw_wifi = raw_data.get("wifi_signal")
            if raw_wifi is None:
                raw_wifi = raw_data.get("rssi")
            wifi_signal: int | None = None
            if raw_wifi is not None:
                try:
                    wifi_signal = int(raw_wifi)
                except (TypeError, ValueError):
                    wifi_signal = None

            # État chargeur / dock
            charging = bool(raw_data.get("charging", raw_data.get("docked", False)))
            docked = bool(raw_data.get("docked", charging))

            # Écran ou mode actif
            active_screen = str(
                raw_data.get("active_screen")
                or raw_data.get("current_screen")
                or self._cached_data.get("active_screen", "Home")
            )

            data: dict[str, Any] = {
                "battery": battery,
                "wifi_signal": wifi_signal,
                "charging": charging,
                "docked": docked,
                "active_screen": active_screen,
                "connected": True,
                "serial_number": str(serial_num),
                "mac_address": str(mac_addr) if mac_addr else None,
                "firmware_version": str(fw_ver),
                "name": raw_data.get("name") or self.entry.title or DEFAULT_NAME,
                "raw_settings": raw_data,
            }

            # Réinitialisation du compteur de défaillances et mise à jour du cache
            self._consecutive_failures = 0
            self._cached_data.update(data)
            return data

        except SevenhugsAuthError as err:
            _LOGGER.error("Erreur d'authentification Sevenhugs sur %s: %s", self.name, err)
            raise ConfigEntryAuthFailed(f"Session Sevenhugs invalide ou expirée: {err}") from err

        except _NETWORK_ERRORS as err:
            self._consecutive_failures += 1

            if self._consecutive_failures < SLEEP_CONSECUTIVE_FAILURES_THRESHOLD:
                _LOGGER.debug(
                    "La télécommande est en veille (socket inaccessible, échec %d/%d): %s. Maintien du cache d'état.",
                    self._consecutive_failures,
                    SLEEP_CONSECUTIVE_FAILURES_THRESHOLD,
                    err,
                )
                if self._cached_data:
                    # Maintien optimiste de l'état sans exception
                    return dict(self._cached_data)

                # Échec lors du premier chargement initial sans cache préalable
                raise UpdateFailed(
                    f"Impossible de joindre la télécommande Sevenhugs (démarrage initial): {err}"
                ) from err

            _LOGGER.warning(
                "La télécommande Sevenhugs ne répond plus après %d tentatives consécutives: %s",
                self._consecutive_failures,
                err,
            )
            raise UpdateFailed(
                f"Smart Remote inaccessible après {self._consecutive_failures} échecs consécutifs: {err}"
            ) from err

        except Exception as err:
            _LOGGER.error("Erreur inattendue lors de la mise à jour de %s: %s", self.name, err, exc_info=True)
            raise UpdateFailed(f"Erreur inattendue de communication: {err}") from err

    # =========================================================================
    # Réveil événementiel
    # =========================================================================

    async def async_handle_wake_up(self, reason: str = "event") -> None:
        """Gère un événement de réveil de la télécommande (mDNS, webhook, bouton, etc.)."""
        _LOGGER.debug(
            "Réveil événementiel de la télécommande (%s). Réinitialisation des échecs et rafraîchissement immédiat.",
            reason,
        )
        self._consecutive_failures = 0
        await self.async_request_refresh()

    async def async_wake_up(self, reason: str = "event") -> None:
        """Alias pour async_handle_wake_up."""
        await self.async_handle_wake_up(reason=reason)

    # =========================================================================
    # Propriétés exposées pour les entités (sensor, binary_sensor, button)
    # =========================================================================

    @property
    def battery(self) -> int | None:
        """Retourne le pourcentage de batterie (0-100%)."""
        source = self.data if self.data is not None else self._cached_data
        val = source.get("battery")
        if val is None:
            return None
        try:
            return max(0, min(100, int(val)))
        except (TypeError, ValueError):
            return None

    @property
    def wifi_signal(self) -> int | None:
        """Retourne la force du signal Wi-Fi en dBm (RSSI)."""
        source = self.data if self.data is not None else self._cached_data
        val = source.get("wifi_signal")
        if val is None:
            val = source.get("rssi")
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    @property
    def charging(self) -> bool:
        """Retourne True si la télécommande est en charge ou sur le dock."""
        source = self.data if self.data is not None else self._cached_data
        return bool(source.get("charging") or source.get("docked", False))

    @property
    def active_screen(self) -> str:
        """Retourne le nom de l'écran ou de l'application active."""
        source = self.data if self.data is not None else self._cached_data
        return str(source.get("active_screen") or source.get("current_screen") or "Home")

    @property
    def is_connected(self) -> bool:
        """Retourne True si la télécommande est connectée ou en veille tolérée."""
        if not self.last_update_success:
            return False
        return self._consecutive_failures < SLEEP_CONSECUTIVE_FAILURES_THRESHOLD

    @property
    def serial_number(self) -> str:
        """Retourne le numéro de série matériel de la télécommande."""
        source = self.data if self.data is not None else self._cached_data
        return str(
            source.get("serial_number")
            or self.entry.unique_id
            or self.entry.data.get(CONF_SERIAL_NUMBER, "unknown")
        )

    @property
    def mac_address(self) -> str | None:
        """Retourne l'adresse MAC Wi-Fi de la télécommande."""
        source = self.data if self.data is not None else self._cached_data
        mac = source.get("mac_address") or self.entry.data.get("mac_address")
        return str(mac) if mac else None

    @property
    def firmware_version(self) -> str:
        """Retourne la version du firmware de la télécommande."""
        source = self.data if self.data is not None else self._cached_data
        return str(source.get("firmware_version") or self.entry.data.get("firmware_version", "v0.76.4"))

    @property
    def remote_id(self) -> str:
        """Retourne l'identifiant court à 4 chiffres de la télécommande."""
        return str(self.entry.data.get(CONF_REMOTE_ID, "5726"))

    @property
    def consecutive_failures(self) -> int:
        """Retourne le nombre actuel d'échecs de connexion consécutifs."""
        return self._consecutive_failures

    @property
    def cached_data(self) -> dict[str, Any]:
        """Retourne une copie du dictionnaire de cache en mémoire."""
        return dict(self._cached_data)
