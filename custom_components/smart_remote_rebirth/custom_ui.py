"""Moteur d'ordonnancement du carrousel et constructeur de grilles Custom UI 6x12 (Jalon v0.6).

Fournit :
- Validation sécurisée des identifiants `package_id:device_id` (`validate_component_id`).
- Fusion non-destructive et renommage du carrousel (`merge_carousel_order`).
- Constructeur déclaratif et validateur géométrique 6x12 pour `SrCustomUiApi`
  (`build_custom_ui_payload`), avec placement automatique, détection de collisions
  et pagination multi-écrans.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
from typing import Any

GRID_WIDTH = 6
GRID_HEIGHT = 12

# Spécifications géométriques et contrôles pré-remplis des widgets firmware v0.76.4
WIDGET_SPECS: dict[str, dict[str, Any]] = {
    "button": {
        "width": 2,
        "height": 2,
        "controls": [],
    },
    "abcd": {
        "width": 6,
        "height": 2,
        "controls": ["a", "b", "c", "d"],
    },
    "navigation": {
        "width": 6,
        "height": 6,
        "controls": ["left", "up", "right", "down", "ok"],
    },
    "arrows_trackpad": {
        "width": 6,
        "height": 6,
        "controls": ["left", "up", "right", "down", "ok"],
    },
    "volume_channel": {
        "width": 6,
        "height": 6,
        "controls": ["vol_down", "ch_up", "vol_up", "ch_down"],
    },
    "numpad": {
        "width": 6,
        "height": 8,
        "controls": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "ok"],
    },
}


def validate_component_id(component_id: str) -> tuple[str, str]:
    """Valide qu'un identifiant de composant respecte strictement le format `package_id:device_id`."""
    if not isinstance(component_id, str) or not component_id.strip():
        raise ValueError("L'identifiant de composant (component_id) doit être une chaîne non vide")

    clean_id = component_id.strip()
    if (
        "://" in clean_id
        or ".." in clean_id
        or "/" in clean_id
        or "\\" in clean_id
        or "@" in clean_id
        or any(c.isspace() for c in clean_id)
    ):
        raise ValueError(f"Caractères interdits dans component_id : {component_id!r}")

    package_id, sep, device_id = clean_id.partition(":")
    if not sep or not package_id or not device_id or ":" in device_id:
        raise ValueError(
            f"Format component_id invalide ({component_id!r}), attendu '<package_id>:<device_id>'"
        )
    return package_id, device_id


def resolve_component_id_from_carousel(
    target: str,
    carousel_order: Sequence[Mapping[str, Any]],
) -> str:
    """Résout un nom d'appareil (ex: 'Bose SOLO5') ou un ID en `package_id:device_id`."""
    if not isinstance(target, str) or not target.strip():
        raise ValueError("La cible (device / component_id) doit être une chaîne non vide")

    clean_target = target.strip()
    target_lower = clean_target.lower()

    # 1. Recherche dans le carrousel par ID exact ou par nom insensible à la casse
    for item in carousel_order:
        if not isinstance(item, Mapping):
            continue
        item_id = str(item.get("id") or "").strip()
        item_name = str(item.get("name") or "").strip()
        if item_id and (item_id == clean_target or item_id.lower() == target_lower):
            validate_component_id(item_id)
            return item_id
        if item_name and item_name.lower() == target_lower and item_id:
            validate_component_id(item_id)
            return item_id

    # 2. Si non trouvé dans le carrousel mais syntaxiquement valide `package_id:device_id`
    validate_component_id(clean_target)
    return clean_target


def merge_carousel_order(
    current_carousel: Sequence[Mapping[str, Any]],
    ordered_items: Sequence[str | Mapping[str, Any]] | None = None,
    *,
    webhook_screen_name: str | None = None,
    pin_ha_first: bool = False,
) -> list[dict[str, Any]]:
    """Réordonne et met à jour `carousel_order` de manière non-destructive.

    - Applique optionnellement le renommage de l'écran `sevenhugs.control.webhooks:*`
      et son épinglage en 1ère position (`pin_ha_first=True`).
    - Réordonne les écrans cités dans `ordered_items` (par ID, alias 'home_assistant'/'webhooks'
      ou nom insensible à la casse) et conserve tous les écrans non cités à la suite.
    """
    working: list[dict[str, Any]] = [
        copy.deepcopy(dict(item))
        for item in current_carousel
        if isinstance(item, Mapping) and item.get("id")
    ]

    # 1. Renommage éventuel de l'écran Webhooks Home Assistant
    if isinstance(webhook_screen_name, str) and webhook_screen_name.strip():
        clean_wh_name = webhook_screen_name.strip()
        for item in working:
            if str(item.get("id", "")).startswith("sevenhugs.control.webhooks:"):
                item["name"] = clean_wh_name

    # 2. Réordonnancement explicite selon ordered_items si fourni
    if ordered_items:
        placed_indices: set[int] = set()
        reordered: list[dict[str, Any]] = []

        for spec in ordered_items:
            if isinstance(spec, str):
                query = spec.strip()
                override_name: str | None = None
                override_hidden: bool | None = None
            elif isinstance(spec, Mapping):
                query = str(spec.get("id") or spec.get("name") or "").strip()
                raw_name = spec.get("name")
                override_name = (
                    str(raw_name).strip()
                    if isinstance(raw_name, str) and raw_name.strip() and "id" in spec
                    else None
                )
                raw_hidden = spec.get("hidden")
                override_hidden = bool(raw_hidden) if isinstance(raw_hidden, bool) else None
            else:
                continue

            if not query:
                continue

            q_lower = query.lower()
            is_webhook_alias = q_lower in (
                "webhooks",
                "home assistant",
                "home_assistant",
                "sevenhugs.control.webhooks",
            )

            matched_idx: int | None = None
            for idx, candidate in enumerate(working):
                if idx in placed_indices:
                    continue
                cid = str(candidate.get("id") or "")
                cname = str(candidate.get("name") or "")
                if (
                    cid == query
                    or cid.lower() == q_lower
                    or cname.lower() == q_lower
                    or (is_webhook_alias and cid.startswith("sevenhugs.control.webhooks:"))
                ):
                    matched_idx = idx
                    break

            if matched_idx is not None:
                placed_indices.add(matched_idx)
                target_item = working[matched_idx]
                if override_name is not None:
                    target_item["name"] = override_name
                if override_hidden is not None:
                    target_item["hidden"] = override_hidden
                reordered.append(target_item)

        # Ajout des éléments non cités à la suite pour ne perdre aucun écran
        for idx, candidate in enumerate(working):
            if idx not in placed_indices:
                reordered.append(candidate)

        working = reordered

    # 3. Épinglage en 1ère position de l'écran Webhooks Home Assistant si demandé
    if pin_ha_first:
        wh_items = [
            item
            for item in working
            if str(item.get("id", "")).startswith("sevenhugs.control.webhooks:")
        ]
        other_items = [
            item
            for item in working
            if not str(item.get("id", "")).startswith("sevenhugs.control.webhooks:")
        ]
        if wh_items:
            working = wh_items + other_items

    return working


def _build_touch_object(
    key: str,
    package_id: str,
    device_id: str,
    command_names: dict[str, str],
    custom_name: str | None = None,
) -> dict[str, Any]:
    """Construit un descripteur d'action tactile conforme au schéma `SrCustomUiApi`."""
    clean_key = str(key).strip()
    resolved_name = (
        custom_name.strip()
        if isinstance(custom_name, str) and custom_name.strip()
        else command_names.get(clean_key, clean_key.replace("_", " ").capitalize())
    )
    return {
        "key": clean_key,
        "name": resolved_name,
        "origin": "default",
        "type": 1,
        "package_id": package_id,
        "device_id": device_id,
    }


class _ScreenGrid:
    """Représente l'occupation d'une page 6x12 de la télécommande."""

    def __init__(self, index: int) -> None:
        self.index = index
        self.occupied: set[tuple[int, int]] = set()
        self.widgets: list[dict[str, Any]] = []

    def can_fit(self, x: int, y: int, width: int, height: int) -> bool:
        """Vérifie si un rectangle (x, y, width, height) tient dans la grille 6x12 sans collision."""
        if x < 0 or y < 0 or x + width > GRID_WIDTH or y + height > GRID_HEIGHT:
            return False
        for dy in range(height):
            for dx in range(width):
                if (x + dx, y + dy) in self.occupied:
                    return False
        return True

    def mark_occupied(self, x: int, y: int, width: int, height: int) -> None:
        """Réserve les cellules occupées par un widget."""
        for dy in range(height):
            for dx in range(width):
                self.occupied.add((x + dx, y + dy))

    def find_first_slot(self, width: int, height: int) -> tuple[int, int] | None:
        """Cherche le premier emplacement libre (y, x) aligné sur un pas de 2."""
        for y in range(0, GRID_HEIGHT - height + 1, 2):
            for x in range(0, GRID_WIDTH - width + 1, 2):
                if self.can_fit(x, y, width, height):
                    return (x, y)
        return None


def _compile_widget(
    spec: Mapping[str, Any],
    widget_seq: int,
    package_id: str,
    device_id: str,
    command_names: dict[str, str],
) -> tuple[dict[str, Any], int, int, int | None, int | None]:
    """Valide et compile un widget déclaratif en dictionnaire firmware `SrCustomUiApi`."""
    wtype = str(spec.get("type") or "button").strip().lower()
    if wtype not in WIDGET_SPECS:
        raise ValueError(
            f"Type de widget inconnu {wtype!r}. Types supportés : {sorted(WIDGET_SPECS)}"
        )

    geom = WIDGET_SPECS[wtype]
    width: int = geom["width"]
    height: int = geom["height"]
    prefill_controls: list[str] = geom["controls"]

    raw_x = spec.get("x")
    raw_y = spec.get("y")
    if (raw_x is None) != (raw_y is None):
        raise ValueError("Les coordonnées 'x' et 'y' doivent être toutes deux fournies ou toutes deux omises")
    x = int(raw_x) if raw_x is not None else None
    y = int(raw_y) if raw_y is not None else None

    if wtype == "button":
        key = str(spec.get("key") or "").strip()
        if not key:
            raise ValueError("Un widget de type 'button' requiert obligatoirement un champ 'key'")
        custom_name = spec.get("name")
        ctrl_entry: dict[str, Any] = {
            "touch": _build_touch_object(
                key,
                package_id,
                device_id,
                command_names,
                str(custom_name) if isinstance(custom_name, str) else None,
            )
        }
        long_press_key = spec.get("long_press_key")
        if isinstance(long_press_key, str) and long_press_key.strip():
            ctrl_entry["long_press"] = _build_touch_object(
                long_press_key.strip(),
                package_id,
                device_id,
                command_names,
            )
        controls = [ctrl_entry]
        widget_id = str(spec.get("id") or f"{key}_{widget_seq}")
    else:
        custom_map = spec.get("controls_map")
        controls = []
        for ctrl_id in prefill_controls:
            mapped_key = (
                str(custom_map[ctrl_id]).strip()
                if isinstance(custom_map, Mapping) and ctrl_id in custom_map
                else ctrl_id
            )
            controls.append({
                "id": ctrl_id,
                "touch": _build_touch_object(
                    mapped_key,
                    package_id,
                    device_id,
                    command_names,
                ),
            })
        widget_id = str(spec.get("id") or f"{wtype}_{widget_seq}")

    widget_dict: dict[str, Any] = {
        "id": widget_id,
        "type": wtype,
        "customizable": True,
        "hidden": bool(spec.get("hidden", False)),
        "controls": controls,
        "x": 0,
        "y": 0,
    }
    return widget_dict, width, height, x, y


def build_custom_ui_payload(
    component_id: str,
    *,
    widgets: Sequence[Mapping[str, Any]] | None = None,
    screens: Sequence[Mapping[str, Any]] | None = None,
    raw_layout: Mapping[str, Any] | None = None,
    top_key: str | None = "power",
    top_icon: str = "speakers",
    existing_ui: Mapping[str, Any] | None = None,
    available_commands: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Construit et valide un payload complet `CustomDevice` pour `PUT /api/custom/{package_id}:{device_id}`.

    Supporte :
    1. Le mode expert (`raw_layout`) : accepte soit l'objet `CustomDevice` complet,
       soit le sous-dictionnaire `layout`.
    2. Le mode déclaratif (`widgets` ou `screens`) : valide les dimensions `6x12`,
       détecte les chevauchements et place automatiquement les widgets sans coordonnées `(x, y)`
       avec découpage automatique en pages (`screens`).
    """
    package_id, device_id = validate_component_id(component_id)

    # Construction de l'annuaire key -> name depuis /api/custom/{id}/commands
    command_names: dict[str, str] = {}
    if isinstance(available_commands, Sequence):
        for group in available_commands:
            if isinstance(group, Mapping):
                cmd_list = group.get("commands")
                if isinstance(cmd_list, Sequence):
                    for cmd in cmd_list:
                        if isinstance(cmd, Mapping) and cmd.get("key"):
                            command_names[str(cmd["key"])] = str(
                                cmd.get("name") or cmd["key"]
                            )
                elif group.get("key"):
                    command_names[str(group["key"])] = str(
                        group.get("name") or group["key"]
                    )

    # 1. Mode Expert : raw_layout
    if raw_layout is not None:
        if not isinstance(raw_layout, Mapping):
            raise ValueError("Le paramètre 'raw_layout' doit être un dictionnaire JSON")
        raw_copy = copy.deepcopy(dict(raw_layout))
        if "layout" in raw_copy and isinstance(raw_copy["layout"], Mapping):
            raw_copy["package_id"] = package_id
            raw_copy["device_id"] = device_id
            raw_copy.setdefault("customized", True)
            raw_copy.setdefault("custom_limit_reached", False)
            return raw_copy
        if "fragments" in raw_copy:
            return {
                "package_id": package_id,
                "device_id": device_id,
                "customized": True,
                "custom_limit_reached": False,
                "layout": raw_copy,
            }
        raise ValueError(
            "Le dictionnaire 'raw_layout' doit contenir la clé 'layout' ou 'fragments'"
        )

    if not widgets and not screens:
        raise ValueError(
            "Vous devez fournir au moins 'widgets', 'screens' ou 'raw_layout'"
        )

    # 2. Mode Déclaratif : widgets (auto-paginé) ou screens (pages explicites)
    compiled_screens: list[dict[str, Any]] = []
    widget_seq = 1

    if screens:
        for s_idx, screen_spec in enumerate(screens, start=1):
            if not isinstance(screen_spec, Mapping):
                raise ValueError(f"La page d'écran #{s_idx} doit être un dictionnaire")
            screen_widgets = screen_spec.get("widgets")
            if not isinstance(screen_widgets, Sequence) or not screen_widgets:
                raise ValueError(f"La page d'écran #{s_idx} doit contenir une liste 'widgets' non vide")

            grid = _ScreenGrid(index=s_idx)
            for w_spec in screen_widgets:
                if not isinstance(w_spec, Mapping):
                    raise ValueError("Chaque widget doit être un dictionnaire")
                w_dict, w, h, x, y = _compile_widget(
                    w_spec, widget_seq, package_id, device_id, command_names
                )
                widget_seq += 1
                if x is not None and y is not None:
                    if x < 0 or y < 0 or x + w > GRID_WIDTH or y + h > GRID_HEIGHT:
                        raise ValueError(
                            f"Widget {w_dict['id']!r} ({w}x{h}) hors des limites 6x12 à ({x}, {y})"
                        )
                    if not grid.can_fit(x, y, w, h):
                        raise ValueError(
                            f"Collision détectée pour le widget {w_dict['id']!r} ({w}x{h}) à ({x}, {y}) sur l'écran #{s_idx}"
                        )
                    place_x, place_y = x, y
                else:
                    slot = grid.find_first_slot(w, h)
                    if slot is None:
                        raise ValueError(
                            f"Espace insuffisant sur l'écran #{s_idx} pour placer le widget {w_dict['id']!r} ({w}x{h})"
                        )
                    place_x, place_y = slot

                grid.mark_occupied(place_x, place_y, w, h)
                w_dict["x"] = place_x
                w_dict["y"] = place_y
                grid.widgets.append(w_dict)

            compiled_screens.append({
                "type": "basic",
                "widgets": grid.widgets,
            })
    elif widgets:
        grids: list[_ScreenGrid] = [_ScreenGrid(index=1)]
        for w_spec in widgets:
            if not isinstance(w_spec, Mapping):
                raise ValueError("Chaque widget doit être un dictionnaire")
            w_dict, w, h, x, y = _compile_widget(
                w_spec, widget_seq, package_id, device_id, command_names
            )
            widget_seq += 1

            if x is not None and y is not None:
                if x < 0 or y < 0 or x + w > GRID_WIDTH or y + h > GRID_HEIGHT:
                    raise ValueError(
                        f"Widget {w_dict['id']!r} ({w}x{h}) hors des limites 6x12 à ({x}, {y})"
                    )
                current_grid = grids[-1]
                if not current_grid.can_fit(x, y, w, h):
                    raise ValueError(
                        f"Collision détectée pour le widget {w_dict['id']!r} ({w}x{h}) à ({x}, {y})"
                    )
                current_grid.mark_occupied(x, y, w, h)
                w_dict["x"] = x
                w_dict["y"] = y
                current_grid.widgets.append(w_dict)
            else:
                # Cherche une place sur la page courante, sinon crée une nouvelle page 6x12
                current_grid = grids[-1]
                slot = current_grid.find_first_slot(w, h)
                if slot is None:
                    current_grid = _ScreenGrid(index=len(grids) + 1)
                    grids.append(current_grid)
                    slot = current_grid.find_first_slot(w, h)
                if slot is None:
                    raise ValueError(
                        f"Impossible de placer le widget {w_dict['id']!r} ({w}x{h}) sur une grille 6x12"
                    )
                place_x, place_y = slot
                current_grid.mark_occupied(place_x, place_y, w, h)
                w_dict["x"] = place_x
                w_dict["y"] = place_y
                current_grid.widgets.append(w_dict)

        for g in grids:
            if g.widgets:
                compiled_screens.append({
                    "type": "basic",
                    "widgets": g.widgets,
                })

    # 3. Construction de l'en-tête `top` (préserve existing_ui.layout.top si présent et non surchargé)
    existing_layout = (
        existing_ui.get("layout")
        if isinstance(existing_ui, Mapping) and isinstance(existing_ui.get("layout"), Mapping)
        else None
    )
    if isinstance(existing_layout, Mapping) and isinstance(existing_layout.get("top"), Mapping) and top_key is None:
        top_section = copy.deepcopy(dict(existing_layout["top"]))
    else:
        effective_top_key = top_key.strip() if isinstance(top_key, str) and top_key.strip() else "power"
        existing_top = (
            existing_layout.get("top")
            if isinstance(existing_layout, Mapping) and isinstance(existing_layout.get("top"), Mapping)
            else {}
        )
        top_section = {
            "id": str(existing_top.get("id") or f"top_{device_id[:8]}"),
            "type": "top",
            "icon": str(existing_top.get("icon") or top_icon),
            "customizable": True,
            "hidden": False,
            "controls": [
                {
                    "touch": _build_touch_object(
                        effective_top_key,
                        package_id,
                        device_id,
                        command_names,
                    )
                }
            ],
        }

    return {
        "package_id": package_id,
        "device_id": device_id,
        "customized": True,
        "custom_limit_reached": False,
        "layout": {
            "top": top_section,
            "fragments": [
                {
                    "id": "main",
                    "name": "Main screen",
                    "screens": compiled_screens,
                }
            ],
        },
    }
