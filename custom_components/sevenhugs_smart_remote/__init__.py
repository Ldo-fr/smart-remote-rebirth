"""Composant Home Assistant Sevenhugs Smart Remote (SR1A).

Phase 1 : Cœur Cryptographique & Client Réseau Asynchrone.
"""

from __future__ import annotations

import os
import sys

# Assure la disponibilité des dépendances vendorisées dans .pylib si présent
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYLIB_DIR = os.path.join(_BASE_DIR, ".pylib")
if os.path.isdir(_PYLIB_DIR) and _PYLIB_DIR not in sys.path:
    sys.path.insert(0, _PYLIB_DIR)

from .client import AsyncSmartRemoteClient, SevenhugsAuthError, SevenhugsConnectionError

DOMAIN = "sevenhugs_smart_remote"

__all__ = [
    "DOMAIN",
    "AsyncSmartRemoteClient",
    "SevenhugsAuthError",
    "SevenhugsConnectionError",
]
