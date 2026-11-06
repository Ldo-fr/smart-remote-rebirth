"""Banc d'essais de mutation contradictoire (Mutation Testing Harness) pour test_coordinator.py.

Auteur: Challenger M4.1 (EMPIRICAL CHALLENGER)
Rôle: critic, specialist

Ce script applique systématiquement des mutations ciblées au fichier
`custom_components/smart_remote_rebirth/coordinator.py`, exécute la suite de tests
`tests/test_coordinator.py`, vérifie empiriquement que chaque mutant est bien détecté et tué,
puis restaure immédiatement le fichier original avec vérification cryptographique SHA256.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGET_FILE = PROJECT_ROOT / "custom_components" / "smart_remote_rebirth" / "coordinator.py"


@dataclass
class Mutation:
    id: str
    name: str
    category: str
    target_str: str
    replacement_str: str
    expected_to_kill: str


MUTATIONS: list[Mutation] = [
    # --- 1. SLEEP RESILIENCE THRESHOLD MUTANTS ---
    Mutation(
        id="MUT-01",
        name="Failure threshold lowered to 2 (SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 2)",
        category="Sleep Threshold",
        target_str="SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 3",
        replacement_str="SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 2",
        expected_to_kill="test_sleep_resilience_two_failures_returns_cache_silently",
    ),
    Mutation(
        id="MUT-02",
        name="Failure threshold raised to 4 (SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 4)",
        category="Sleep Threshold",
        target_str="SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 3",
        replacement_str="SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 4",
        expected_to_kill="test_sleep_resilience_third_failure_raises_update_failed_and_marks_unavailable",
    ),
    Mutation(
        id="MUT-03",
        name="Failure threshold lowered to 1 (no sleep tolerance)",
        category="Sleep Threshold",
        target_str="SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 3",
        replacement_str="SLEEP_CONSECUTIVE_FAILURES_THRESHOLD = 1",
        expected_to_kill="test_sleep_resilience_two_failures_returns_cache_silently",
    ),

    # --- 2. CACHE RETENTION & COLD START MUTANTS ---
    Mutation(
        id="MUT-04",
        name="Cache not returned on tolerated failure 1 (returns empty dict)",
        category="Cache Retention",
        target_str="return dict(self._cached_data)",
        replacement_str="return {}",
        expected_to_kill="test_sleep_resilience_two_failures_returns_cache_silently",
    ),
    Mutation(
        id="MUT-05",
        name="Cache not returned on tolerated failure 1 (returns None)",
        category="Cache Retention",
        target_str="return dict(self._cached_data)",
        replacement_str="return None",
        expected_to_kill="test_sleep_resilience_two_failures_returns_cache_silently",
    ),
    Mutation(
        id="MUT-06",
        name="Cold start failure does not raise UpdateFailed (swallows error and returns empty cache)",
        category="Cold Start",
        target_str='raise UpdateFailed(\n                    f"Impossible de joindre la télécommande Sevenhugs (démarrage initial): {err}"\n                ) from err',
        replacement_str="return {}",
        expected_to_kill="test_cold_start_network_failure_raises_update_failed_immediately",
    ),
    Mutation(
        id="MUT-07",
        name="cached_data property returns mutable internal reference instead of copy",
        category="Cache Immutability",
        target_str='    @property\n    def cached_data(self) -> dict[str, Any]:\n        """Retourne une copie du dictionnaire de cache en mémoire."""\n        return dict(self._cached_data)',
        replacement_str='    @property\n    def cached_data(self) -> dict[str, Any]:\n        """Retourne une copie du dictionnaire de cache en mémoire."""\n        return self._cached_data',
        expected_to_kill="test_cached_data_immutability",
    ),
    Mutation(
        id="MUT-08",
        name="Property fallback to _cached_data when self.data is None removed",
        category="Cache Fallback",
        target_str="source = self.data if self.data is not None else self._cached_data",
        replacement_str="source = self.data or {}",
        expected_to_kill="test_properties_fallback_to_cache_when_data_is_none",
    ),

    # --- 3. POLLING INTERVAL & NAME RESOLUTION MUTANTS ---
    Mutation(
        id="MUT-09",
        name="Polling interval ignores entry.options",
        category="Polling Interval",
        target_str="""        polling_interval = entry.options.get(
            CONF_POLLING_INTERVAL,
            getattr(entry, "data", {}).get(CONF_POLLING_INTERVAL, DEFAULT_COORDINATOR_UPDATE_INTERVAL),
        )""",
        replacement_str="""        polling_interval = getattr(entry, "data", {}).get(CONF_POLLING_INTERVAL, DEFAULT_COORDINATOR_UPDATE_INTERVAL)""",
        expected_to_kill="test_polling_interval_options_and_fallbacks",
    ),
    Mutation(
        id="MUT-10",
        name="Polling interval ignores entry.data fallback",
        category="Polling Interval",
        target_str="""        polling_interval = entry.options.get(
            CONF_POLLING_INTERVAL,
            getattr(entry, "data", {}).get(CONF_POLLING_INTERVAL, DEFAULT_COORDINATOR_UPDATE_INTERVAL),
        )""",
        replacement_str="""        polling_interval = entry.options.get(CONF_POLLING_INTERVAL, DEFAULT_COORDINATOR_UPDATE_INTERVAL)""",
        expected_to_kill="test_polling_interval_options_and_fallbacks",
    ),
    Mutation(
        id="MUT-11",
        name="Polling interval does not validate <= 0 or invalid types (allows <= 0)",
        category="Polling Interval",
        target_str="if not isinstance(polling_interval, (int, float)) or polling_interval <= 0:",
        replacement_str="if False:",
        expected_to_kill="test_polling_interval_options_and_fallbacks",
    ),
    Mutation(
        id="MUT-12",
        name="Name resolution ignores unique_id and uses entry_id directly",
        category="Name Resolution",
        target_str='unique_id = entry.unique_id or getattr(entry, "data", {}).get(CONF_SERIAL_NUMBER, entry.entry_id)',
        replacement_str="unique_id = entry.entry_id",
        expected_to_kill="test_coordinator_name_resolution_hierarchy",
    ),

    # --- 4. EVENT WAKE-UP MUTANTS ---
    Mutation(
        id="MUT-13",
        name="async_handle_wake_up does not reset self._consecutive_failures to 0",
        category="Event Wake-Up",
        target_str="self._consecutive_failures = 0\n        await self.async_request_refresh()",
        replacement_str="await self.async_request_refresh()",
        expected_to_kill="test_async_wake_up_resets_failures_and_forces_refresh",
    ),
    Mutation(
        id="MUT-14",
        name="async_handle_wake_up does not trigger async_request_refresh",
        category="Event Wake-Up",
        target_str="self._consecutive_failures = 0\n        await self.async_request_refresh()",
        replacement_str="self._consecutive_failures = 0",
        expected_to_kill="test_async_wake_up_resets_failures_and_forces_refresh",
    ),
    Mutation(
        id="MUT-15",
        name="async_wake_up alias broken (does not delegate to async_handle_wake_up)",
        category="Event Wake-Up",
        target_str="await self.async_handle_wake_up(reason=reason)",
        replacement_str="pass",
        expected_to_kill="test_async_wake_up_resets_failures_and_forces_refresh",
    ),

    # --- 5. AUTH ERROR & RECOVERY MUTANTS ---
    Mutation(
        id="MUT-16",
        name="SevenhugsAuthError translated to UpdateFailed instead of ConfigEntryAuthFailed",
        category="Auth Translation",
        target_str='raise ConfigEntryAuthFailed(f"Session Sevenhugs invalide ou expirée: {err}") from err',
        replacement_str='raise UpdateFailed(f"Session Sevenhugs invalide ou expirée: {err}") from err',
        expected_to_kill="test_auth_error_translates_to_config_entry_auth_failed",
    ),
    Mutation(
        id="MUT-17",
        name="SevenhugsAuthError treated as network sleep error instead of auth failure",
        category="Auth Translation",
        target_str="except SevenhugsAuthError as err:",
        replacement_str="except SevenhugsAuthError_DISABLED as err:",
        expected_to_kill="test_auth_error_translates_to_config_entry_auth_failed",
    ),
    Mutation(
        id="MUT-18",
        name="Recovery on successful update does not reset consecutive_failures to 0",
        category="Recovery",
        target_str="self._consecutive_failures = 0\n            self._cached_data.update(data)",
        replacement_str="self._cached_data.update(data)",
        expected_to_kill="test_recovery_resets_failures_and_restores_connectivity",
    ),
    Mutation(
        id="MUT-19",
        name="is_connected property ignores last_update_success (always reports True if failures < 3)",
        category="Connectivity Invariant",
        target_str="""    @property
    def is_connected(self) -> bool:
        \"\"\"Retourne True si la télécommande est connectée ou en veille tolérée.\"\"\"
        if not self.last_update_success:
            return False
        return self._consecutive_failures < SLEEP_CONSECUTIVE_FAILURES_THRESHOLD""",
        replacement_str="""    @property
    def is_connected(self) -> bool:
        return self._consecutive_failures < SLEEP_CONSECUTIVE_FAILURES_THRESHOLD""",
        expected_to_kill="test_sleep_resilience_third_failure_raises_update_failed_and_marks_unavailable",
    ),
    Mutation(
        id="MUT-20",
        name="is_connected uses <= threshold instead of < threshold",
        category="Connectivity Invariant",
        target_str="return self._consecutive_failures < SLEEP_CONSECUTIVE_FAILURES_THRESHOLD",
        replacement_str="return self._consecutive_failures <= SLEEP_CONSECUTIVE_FAILURES_THRESHOLD",
        expected_to_kill="test_sleep_resilience_third_failure_raises_update_failed_and_marks_unavailable",
    ),

    # --- 6. TELEMETRY CLAMPING & PARSING MUTANTS ---
    Mutation(
        id="MUT-21",
        name="Battery clamping upper bound 100 removed (allows > 100)",
        category="Telemetry Clamping",
        target_str="battery = max(0, min(100, int(raw_battery)))",
        replacement_str="battery = max(0, int(raw_battery))",
        expected_to_kill="test_battery_clamping_and_fault_tolerance",
    ),
    Mutation(
        id="MUT-22",
        name="Battery clamping lower bound 0 removed (allows < 0)",
        category="Telemetry Clamping",
        target_str="battery = max(0, min(100, int(raw_battery)))",
        replacement_str="battery = min(100, int(raw_battery))",
        expected_to_kill="test_battery_clamping_and_fault_tolerance",
    ),
    Mutation(
        id="MUT-23",
        name="Battery invalid type fallback to 100 instead of cached battery",
        category="Telemetry Clamping",
        target_str='battery = self._cached_data.get("battery", 100)',
        replacement_str="battery = 100",
        expected_to_kill="test_battery_clamping_and_fault_tolerance",
    ),
    Mutation(
        id="MUT-24",
        name="Wi-Fi RSSI fallback removed (wifi_signal only)",
        category="Telemetry Normalization",
        target_str='raw_wifi = raw_data.get("wifi_signal")\n            if raw_wifi is None:\n                raw_wifi = raw_data.get("rssi")',
        replacement_str='raw_wifi = raw_data.get("wifi_signal")',
        expected_to_kill="test_wifi_signal_and_rssi_fallback",
    ),
    Mutation(
        id="MUT-25",
        name="Charging fallback to docked removed",
        category="Telemetry Normalization",
        target_str='charging = bool(raw_data.get("charging", raw_data.get("docked", False)))',
        replacement_str='charging = bool(raw_data.get("charging", False))',
        expected_to_kill="test_charging_and_docked_state_matrix",
    ),
    Mutation(
        id="MUT-26",
        name="Active screen fallback to current_screen removed",
        category="Telemetry Normalization",
        target_str="""            active_screen = str(
                raw_data.get("active_screen")
                or raw_data.get("current_screen")
                or self._cached_data.get("active_screen", "Home")
            )""",
        replacement_str="""            active_screen = str(
                raw_data.get("active_screen")
                or self._cached_data.get("active_screen", "Home")
            )""",
        expected_to_kill="test_active_screen_normalization_and_fallback",
    ),
    Mutation(
        id="MUT-27",
        name="Active screen default 'Home' replaced with 'Unknown'",
        category="Telemetry Normalization",
        target_str='or self._cached_data.get("active_screen", "Home")',
        replacement_str='or self._cached_data.get("active_screen", "Unknown")',
        expected_to_kill="test_active_screen_normalization_and_fallback",
    ),
]


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def run_mutation_test() -> dict:
    original_content = TARGET_FILE.read_text(encoding="utf-8")
    original_sha = compute_sha256(TARGET_FILE)

    results = []
    killed_count = 0
    survived_count = 0

    print("=" * 80)
    print("STARTING EMPIRICAL MUTATION TESTING AGAINST tests/test_coordinator.py")
    print(f"Target: {TARGET_FILE}")
    print(f"Baseline SHA256: {original_sha}")
    print(f"Total mutations in matrix: {len(MUTATIONS)}")
    print("=" * 80)

    try:
        for idx, m in enumerate(MUTATIONS, 1):
            if m.target_str not in original_content:
                print(f"[{idx:02d}/{len(MUTATIONS):02d}] {m.id} ERROR: target string not found!")
                print(f"Target: {m.target_str[:60]!r}")
                sys.exit(1)

            # Apply mutation
            mutated_content = original_content.replace(m.target_str, m.replacement_str, 1)
            TARGET_FILE.write_text(mutated_content, encoding="utf-8")

            t0 = time.perf_counter()
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "unittest", "tests/test_coordinator.py"],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                elapsed = time.perf_counter() - t0
                test_output = proc.stderr + proc.stdout
                is_killed = (proc.returncode != 0)

            except subprocess.TimeoutExpired:
                elapsed = time.perf_counter() - t0
                is_killed = True
                test_output = "TIMEOUT EXPIRED (Hanging mutant killed)"
            finally:
                # Restore original immediately
                TARGET_FILE.write_text(original_content, encoding="utf-8")
                current_sha = compute_sha256(TARGET_FILE)
                if current_sha != original_sha:
                    print(f"FATAL: Restoration integrity failure for {m.id}!")
                    sys.exit(1)

            if is_killed:
                killed_count += 1
                status = "KILLED"
                # Find which test failed
                failed_line = "Failed test"
                for line in test_output.splitlines():
                    if line.startswith("FAIL:") or line.startswith("ERROR:"):
                        failed_line = line.strip()
                        break
                print(f"  [{idx:02d}/{len(MUTATIONS):02d}] {m.id} [{status}] ({elapsed:.3f}s) — {m.name}")
                print(f"         Detector: {failed_line[:85]}")
            else:
                survived_count += 1
                status = "SURVIVED"
                print(f"  [{idx:02d}/{len(MUTATIONS):02d}] {m.id} [{status}] ({elapsed:.3f}s) — {m.name} ⚠️ BLINDSPOT!")

            results.append({
                "mutation": m,
                "status": status,
                "elapsed": elapsed,
                "output": test_output,
            })

    finally:
        # Final safety restore
        TARGET_FILE.write_text(original_content, encoding="utf-8")

    final_sha = compute_sha256(TARGET_FILE)
    assert final_sha == original_sha, "Target file altered after mutation testing run!"

    kill_rate = (killed_count / len(MUTATIONS)) * 100
    print("=" * 80)
    print(f"MUTATION TESTING SUMMARY: {killed_count}/{len(MUTATIONS)} KILLED ({kill_rate:.1f}%)")
    print(f"SURVIVED: {survived_count}")
    print(f"File SHA256 integrity verified: {final_sha}")
    print("=" * 80)

    return {
        "total": len(MUTATIONS),
        "killed": killed_count,
        "survived": survived_count,
        "kill_rate": kill_rate,
        "results": results,
    }


if __name__ == "__main__":
    res = run_mutation_test()
    if res["survived"] > 0:
        sys.exit(2)
    sys.exit(0)
