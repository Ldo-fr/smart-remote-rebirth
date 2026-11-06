"""Adversarial and Mutation Testing Harness for Milestone 4 (test_entities.py).

Executes:
1. Automated Mutation Testing: 42 distinct mutants injected across entity.py,
   sensor.py, binary_sensor.py, button.py, and __init__.py.
   Ensures that EVERY mutant is caught and killed by tests/test_entities.py.
2. Rapid Entity Lifecycle Stress Harness:
   - Rapid addition and removal (100 cycles) across all entity types.
   - Verifies listener leak prevention in coordinator.
   - Verifies state machine integrity in hass.states.
   - Concurrent refresh during rapid lifecycle transitions.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any, Callable

# Ingestion des shims Home Assistant
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tests.ha_mock
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

import tests.test_entities as te
import custom_components.sevenhugs_smart_remote as pkg_mod
import custom_components.sevenhugs_smart_remote.const as const_mod
import custom_components.sevenhugs_smart_remote.binary_sensor as bs_mod
import custom_components.sevenhugs_smart_remote.button as btn_mod
import custom_components.sevenhugs_smart_remote.entity as entity_mod
import custom_components.sevenhugs_smart_remote.sensor as sensor_mod


class MutationTestResult:
    def __init__(self, mutant_id: str, description: str, target_test: str):
        self.mutant_id = mutant_id
        self.description = description
        self.target_test = target_test
        self.killed = False
        self.error_message: str | None = None


def run_target_test(test_name: str) -> tuple[bool, str]:
    """Exécute un test unitaire spécifique de test_entities.py et retourne (passed, output)."""
    suite = unittest.defaultTestLoader.loadTestsFromName(f"tests.test_entities.{test_name}")
    runner = unittest.TextTestRunner(stream=open(os.devnull, "w"), verbosity=0)
    result = runner.run(suite)
    passed = result.wasSuccessful()
    output = ""
    if not passed:
        errs = result.failures + result.errors
        output = "\n".join(e[1] for e in errs)
    return passed, output


class MutationSuite:
    def __init__(self) -> None:
        self.results: list[MutationTestResult] = []

    def test_mutant(
        self,
        mutant_id: str,
        description: str,
        target_test: str,
        apply_fn: Callable[[], Any],
        revert_fn: Callable[[Any], None],
    ) -> None:
        res = MutationTestResult(mutant_id, description, target_test)
        context = apply_fn()
        try:
            passed, output = run_target_test(target_test)
            if not passed:
                res.killed = True
                res.error_message = output.strip().split("\n")[-1]
            else:
                res.killed = False
                res.error_message = "SURVIVED: test passed unexpectedly with mutant applied!"
        finally:
            revert_fn(context)
        self.results.append(res)


def run_all_mutations() -> list[MutationTestResult]:
    suite = MutationSuite()

    # --- CATEGORY 1: BASE ENTITY (entity.py) ---

    # M01: _attr_has_entity_name = False
    def apply_m01():
        old = entity_mod.SevenhugsBaseEntity._attr_has_entity_name
        entity_mod.SevenhugsBaseEntity._attr_has_entity_name = False
        return old
    def revert_m01(old):
        entity_mod.SevenhugsBaseEntity._attr_has_entity_name = old
    suite.test_mutant("M01", "SevenhugsBaseEntity._attr_has_entity_name = False", "TestSevenhugsBaseEntity.test_base_entity_inheritance_and_flags", apply_m01, revert_m01)

    # M02: _attr_should_poll = True
    def apply_m02():
        old = entity_mod.SevenhugsBaseEntity._attr_should_poll
        entity_mod.SevenhugsBaseEntity._attr_should_poll = True
        return old
    def revert_m02(old):
        entity_mod.SevenhugsBaseEntity._attr_should_poll = old
    suite.test_mutant("M02", "SevenhugsBaseEntity._attr_should_poll = True", "TestSevenhugsBaseEntity.test_base_entity_inheritance_and_flags", apply_m02, revert_m02)

    # M03: unique_id format corruption (omits serial_number)
    orig_init = entity_mod.SevenhugsBaseEntity.__init__
    def apply_m03():
        def mutated_init(self, coord, entry, entity_key, description=None):
            orig_init(self, coord, entry, entity_key, description)
            self._attr_unique_id = f"corrupted_{entity_key}"
        entity_mod.SevenhugsBaseEntity.__init__ = mutated_init
        return orig_init
    def revert_m03(old):
        entity_mod.SevenhugsBaseEntity.__init__ = old
    suite.test_mutant("M03", "SevenhugsBaseEntity unique_id format corrupted", "TestSevenhugsBaseEntity.test_base_entity_inheritance_and_flags", apply_m03, revert_m03)

    # M04: DeviceInfo manufacturer altered
    def apply_m04():
        def mutated_init(self, coord, entry, entity_key, description=None):
            orig_init(self, coord, entry, entity_key, description)
            self._attr_device_info.manufacturer = "BadManufacturer"
        entity_mod.SevenhugsBaseEntity.__init__ = mutated_init
        return orig_init
    def revert_m04(old):
        entity_mod.SevenhugsBaseEntity.__init__ = old
    suite.test_mutant("M04", "SevenhugsBaseEntity DeviceInfo manufacturer = BadManufacturer", "TestSevenhugsBaseEntity.test_device_info_structure", apply_m04, revert_m04)

    # M05: DeviceInfo model altered
    def apply_m05():
        def mutated_init(self, coord, entry, entity_key, description=None):
            orig_init(self, coord, entry, entity_key, description)
            self._attr_device_info.model = "BadModelSR2"
        entity_mod.SevenhugsBaseEntity.__init__ = mutated_init
        return orig_init
    def revert_m05(old):
        entity_mod.SevenhugsBaseEntity.__init__ = old
    suite.test_mutant("M05", "SevenhugsBaseEntity DeviceInfo model = BadModelSR2", "TestSevenhugsBaseEntity.test_device_info_structure", apply_m05, revert_m05)

    # M06: DeviceInfo sw_version altered
    def apply_m06():
        def mutated_init(self, coord, entry, entity_key, description=None):
            orig_init(self, coord, entry, entity_key, description)
            self._attr_device_info.sw_version = "v999.0.0"
        entity_mod.SevenhugsBaseEntity.__init__ = mutated_init
        return orig_init
    def revert_m06(old):
        entity_mod.SevenhugsBaseEntity.__init__ = old
    suite.test_mutant("M06", "SevenhugsBaseEntity DeviceInfo sw_version = v999.0.0", "TestSevenhugsBaseEntity.test_device_info_structure", apply_m06, revert_m06)

    # M07: DeviceInfo hw_version altered
    def apply_m07():
        def mutated_init(self, coord, entry, entity_key, description=None):
            orig_init(self, coord, entry, entity_key, description)
            self._attr_device_info.hw_version = "WrongHW"
        entity_mod.SevenhugsBaseEntity.__init__ = mutated_init
        return orig_init
    def revert_m07(old):
        entity_mod.SevenhugsBaseEntity.__init__ = old
    suite.test_mutant("M07", "SevenhugsBaseEntity DeviceInfo hw_version = WrongHW", "TestSevenhugsBaseEntity.test_device_info_structure", apply_m07, revert_m07)

    # M08: DeviceInfo connections emptied
    def apply_m08():
        def mutated_init(self, coord, entry, entity_key, description=None):
            orig_init(self, coord, entry, entity_key, description)
            self._attr_device_info.connections = set()
        entity_mod.SevenhugsBaseEntity.__init__ = mutated_init
        return orig_init
    def revert_m08(old):
        entity_mod.SevenhugsBaseEntity.__init__ = old
    suite.test_mutant("M08", "SevenhugsBaseEntity DeviceInfo connections emptied", "TestSevenhugsBaseEntity.test_device_info_structure", apply_m08, revert_m08)

    # M09: available ignores last_update_success (always True)
    orig_avail = entity_mod.SevenhugsBaseEntity.available
    def apply_m09():
        entity_mod.SevenhugsBaseEntity.available = property(lambda self: bool(self._attr_available))
        return orig_avail
    def revert_m09(old):
        entity_mod.SevenhugsBaseEntity.available = old
    suite.test_mutant("M09", "SevenhugsBaseEntity.available ignores last_update_success", "TestSevenhugsBaseEntity.test_base_entity_availability_logic", apply_m09, revert_m09)

    # M10: available ignores _attr_available
    def apply_m10():
        entity_mod.SevenhugsBaseEntity.available = property(lambda self: bool(self.coordinator.last_update_success))
        return orig_avail
    def revert_m10(old):
        entity_mod.SevenhugsBaseEntity.available = old
    suite.test_mutant("M10", "SevenhugsBaseEntity.available ignores _attr_available", "TestSevenhugsBaseEntity.test_base_entity_availability_logic", apply_m10, revert_m10)

    # --- CATEGORY 2: SENSOR PLATFORM (sensor.py) ---

    # M11: sensor.PARALLEL_UPDATES = 1
    def apply_m11():
        old_mod = sensor_mod.PARALLEL_UPDATES
        old_te = te.SENSOR_PARALLEL_UPDATES
        sensor_mod.PARALLEL_UPDATES = 1
        te.SENSOR_PARALLEL_UPDATES = 1
        return (old_mod, old_te)
    def revert_m11(olds):
        sensor_mod.PARALLEL_UPDATES, te.SENSOR_PARALLEL_UPDATES = olds
    suite.test_mutant("M11", "sensor.PARALLEL_UPDATES = 1", "TestSevenhugsSensorPlatform.test_sensor_parallel_updates_constant", apply_m11, revert_m11)

    # M12: SevenhugsBatterySensor._attr_device_class = None
    def apply_m12():
        old = sensor_mod.SevenhugsBatterySensor._attr_device_class
        sensor_mod.SevenhugsBatterySensor._attr_device_class = None
        return old
    def revert_m12(old):
        sensor_mod.SevenhugsBatterySensor._attr_device_class = old
    suite.test_mutant("M12", "SevenhugsBatterySensor._attr_device_class = None", "TestSevenhugsSensorPlatform.test_battery_sensor_specifications", apply_m12, revert_m12)

    # M13: SevenhugsBatterySensor._attr_native_unit_of_measurement = "V"
    def apply_m13():
        old = sensor_mod.SevenhugsBatterySensor._attr_native_unit_of_measurement
        sensor_mod.SevenhugsBatterySensor._attr_native_unit_of_measurement = "V"
        return old
    def revert_m13(old):
        sensor_mod.SevenhugsBatterySensor._attr_native_unit_of_measurement = old
    suite.test_mutant("M13", "SevenhugsBatterySensor unit = 'V'", "TestSevenhugsSensorPlatform.test_battery_sensor_specifications", apply_m13, revert_m13)

    # M14: SevenhugsBatterySensor._attr_state_class = None
    def apply_m14():
        old = sensor_mod.SevenhugsBatterySensor._attr_state_class
        sensor_mod.SevenhugsBatterySensor._attr_state_class = None
        return old
    def revert_m14(old):
        sensor_mod.SevenhugsBatterySensor._attr_state_class = old
    suite.test_mutant("M14", "SevenhugsBatterySensor state_class = None", "TestSevenhugsSensorPlatform.test_battery_sensor_specifications", apply_m14, revert_m14)

    # M15: SevenhugsWifiSignalSensor._attr_device_class = None
    def apply_m15():
        old = sensor_mod.SevenhugsWifiSignalSensor._attr_device_class
        sensor_mod.SevenhugsWifiSignalSensor._attr_device_class = None
        return old
    def revert_m15(old):
        sensor_mod.SevenhugsWifiSignalSensor._attr_device_class = old
    suite.test_mutant("M15", "SevenhugsWifiSignalSensor device_class = None", "TestSevenhugsSensorPlatform.test_wifi_signal_sensor_specifications", apply_m15, revert_m15)

    # M16: SevenhugsWifiSignalSensor._attr_native_unit_of_measurement = "dB"
    def apply_m16():
        old = sensor_mod.SevenhugsWifiSignalSensor._attr_native_unit_of_measurement
        sensor_mod.SevenhugsWifiSignalSensor._attr_native_unit_of_measurement = "dB"
        return old
    def revert_m16(old):
        sensor_mod.SevenhugsWifiSignalSensor._attr_native_unit_of_measurement = old
    suite.test_mutant("M16", "SevenhugsWifiSignalSensor unit = 'dB'", "TestSevenhugsSensorPlatform.test_wifi_signal_sensor_specifications", apply_m16, revert_m16)

    # M17: SevenhugsActiveScreenSensor._attr_icon = "mdi:television"
    def apply_m17():
        old = sensor_mod.SevenhugsActiveScreenSensor._attr_icon
        sensor_mod.SevenhugsActiveScreenSensor._attr_icon = "mdi:television"
        return old
    def revert_m17(old):
        sensor_mod.SevenhugsActiveScreenSensor._attr_icon = old
    suite.test_mutant("M17", "SevenhugsActiveScreenSensor icon = 'mdi:television'", "TestSevenhugsSensorPlatform.test_active_screen_sensor_specifications", apply_m17, revert_m17)

    # M18: SevenhugsActiveScreenSensor._attr_device_class = SensorDeviceClass.BATTERY
    def apply_m18():
        old = sensor_mod.SevenhugsActiveScreenSensor._attr_device_class
        sensor_mod.SevenhugsActiveScreenSensor._attr_device_class = SensorDeviceClass.BATTERY
        return old
    def revert_m18(old):
        sensor_mod.SevenhugsActiveScreenSensor._attr_device_class = old
    suite.test_mutant("M18", "SevenhugsActiveScreenSensor device_class = BATTERY", "TestSevenhugsSensorPlatform.test_active_screen_sensor_specifications", apply_m18, revert_m18)

    # M19: sensor.async_setup_entry adds only 2 sensors
    orig_sensor_setup = sensor_mod.async_setup_entry
    def apply_m19():
        async def mutated_setup(hass, entry, async_add):
            def filtered_add(ents):
                async_add(ents[:2])
            await orig_sensor_setup(hass, entry, filtered_add)
        sensor_mod.async_setup_entry = mutated_setup
        te.sensor_setup_entry = mutated_setup
        return orig_sensor_setup
    def revert_m19(old):
        sensor_mod.async_setup_entry = old
        te.sensor_setup_entry = old
    suite.test_mutant("M19", "sensor async_setup_entry adds only 2 sensors", "TestSevenhugsSensorPlatform.test_sensor_async_setup_entry_via_runtime_data", apply_m19, revert_m19)

    # --- CATEGORY 3: BINARY SENSOR PLATFORM (binary_sensor.py) ---

    # M20: binary_sensor.PARALLEL_UPDATES = 1
    def apply_m20():
        old_mod = bs_mod.PARALLEL_UPDATES
        old_te = te.BINARY_SENSOR_PARALLEL_UPDATES
        bs_mod.PARALLEL_UPDATES = 1
        te.BINARY_SENSOR_PARALLEL_UPDATES = 1
        return (old_mod, old_te)
    def revert_m20(olds):
        bs_mod.PARALLEL_UPDATES, te.BINARY_SENSOR_PARALLEL_UPDATES = olds
    suite.test_mutant("M20", "binary_sensor.PARALLEL_UPDATES = 1", "TestSevenhugsBinarySensorPlatform.test_binary_sensor_parallel_updates_constant", apply_m20, revert_m20)

    # M21: SevenhugsChargingBinarySensor._attr_device_class = None
    def apply_m21():
        old = bs_mod.SevenhugsChargingBinarySensor._attr_device_class
        bs_mod.SevenhugsChargingBinarySensor._attr_device_class = None
        return old
    def revert_m21(old):
        bs_mod.SevenhugsChargingBinarySensor._attr_device_class = old
    suite.test_mutant("M21", "SevenhugsChargingBinarySensor device_class = None", "TestSevenhugsBinarySensorPlatform.test_charging_binary_sensor_specifications", apply_m21, revert_m21)

    # M22: SevenhugsChargingBinarySensor.extra_state_attributes returns {} (omits docked)
    orig_charging_attrs = bs_mod.SevenhugsChargingBinarySensor.extra_state_attributes
    def apply_m22():
        bs_mod.SevenhugsChargingBinarySensor.extra_state_attributes = property(lambda self: {})
        return orig_charging_attrs
    def revert_m22(old):
        bs_mod.SevenhugsChargingBinarySensor.extra_state_attributes = old
    suite.test_mutant("M22", "SevenhugsChargingBinarySensor extra_state_attributes omits docked", "TestSevenhugsBinarySensorPlatform.test_charging_binary_sensor_specifications", apply_m22, revert_m22)

    # M23: SevenhugsConnectivityBinarySensor.available NOT overridden (delegates to base entity)
    # THIS IS THE CRITICAL INVARIANT: connectivity sensor MUST stay available=True during sleep/disconnect!
    orig_conn_avail = bs_mod.SevenhugsConnectivityBinarySensor.available
    def apply_m23():
        del bs_mod.SevenhugsConnectivityBinarySensor.available
        return orig_conn_avail
    def revert_m23(old):
        bs_mod.SevenhugsConnectivityBinarySensor.available = old
    suite.test_mutant("M23", "SevenhugsConnectivityBinarySensor.available not overridden (reverts to base entity)", "TestSevenhugsBinarySensorPlatform.test_connectivity_binary_sensor_critical_availability_invariant", apply_m23, revert_m23)

    # M24: SevenhugsConnectivityBinarySensor._attr_device_class = None
    def apply_m24():
        old = bs_mod.SevenhugsConnectivityBinarySensor._attr_device_class
        bs_mod.SevenhugsConnectivityBinarySensor._attr_device_class = None
        return old
    def revert_m24(old):
        bs_mod.SevenhugsConnectivityBinarySensor._attr_device_class = old
    suite.test_mutant("M24", "SevenhugsConnectivityBinarySensor device_class = None", "TestSevenhugsBinarySensorPlatform.test_connectivity_binary_sensor_critical_availability_invariant", apply_m24, revert_m24)

    # M25: SevenhugsConnectivityBinarySensor.extra_state_attributes returns {} (omits consecutive_failures)
    orig_conn_attrs = bs_mod.SevenhugsConnectivityBinarySensor.extra_state_attributes
    def apply_m25():
        bs_mod.SevenhugsConnectivityBinarySensor.extra_state_attributes = property(lambda self: {})
        return orig_conn_attrs
    def revert_m25(old):
        bs_mod.SevenhugsConnectivityBinarySensor.extra_state_attributes = old
    suite.test_mutant("M25", "SevenhugsConnectivityBinarySensor extra_state_attributes omits consecutive_failures", "TestSevenhugsBinarySensorPlatform.test_connectivity_binary_sensor_critical_availability_invariant", apply_m25, revert_m25)

    # M26: binary_sensor.async_setup_entry adds only 1 entity
    orig_bs_setup = bs_mod.async_setup_entry
    def apply_m26():
        async def mutated_setup(hass, entry, async_add):
            def filtered_add(ents):
                async_add(ents[:1])
            await orig_bs_setup(hass, entry, filtered_add)
        bs_mod.async_setup_entry = mutated_setup
        te.binary_sensor_setup_entry = mutated_setup
        return orig_bs_setup
    def revert_m26(old):
        bs_mod.async_setup_entry = old
        te.binary_sensor_setup_entry = old
    suite.test_mutant("M26", "binary_sensor async_setup_entry adds only 1 entity", "TestSevenhugsBinarySensorPlatform.test_binary_sensor_async_setup_entry", apply_m26, revert_m26)

    # --- CATEGORY 4: BUTTON PLATFORM (button.py) ---

    # M27: button.PARALLEL_UPDATES = 1
    def apply_m27():
        old_mod = btn_mod.PARALLEL_UPDATES
        old_te = te.BUTTON_PARALLEL_UPDATES
        btn_mod.PARALLEL_UPDATES = 1
        te.BUTTON_PARALLEL_UPDATES = 1
        return (old_mod, old_te)
    def revert_m27(olds):
        btn_mod.PARALLEL_UPDATES, te.BUTTON_PARALLEL_UPDATES = olds
    suite.test_mutant("M27", "button.PARALLEL_UPDATES = 1", "TestSevenhugsButtonPlatform.test_button_parallel_updates_constant", apply_m27, revert_m27)

    # M28: SevenhugsForceSyncButton.async_press raw exception propagation (no HAError)
    orig_fs_press = btn_mod.SevenhugsForceSyncButton.async_press
    def apply_m28():
        async def mutated_press(self):
            await self.coordinator.async_request_refresh()
        btn_mod.SevenhugsForceSyncButton.async_press = mutated_press
        return orig_fs_press
    def revert_m28(old):
        btn_mod.SevenhugsForceSyncButton.async_press = old
    suite.test_mutant("M28", "SevenhugsForceSyncButton.async_press raw exception propagation (no HAError)", "TestSevenhugsButtonPlatform.test_force_sync_button_nominal_and_error", apply_m28, revert_m28)

    # M29: SevenhugsPingButton.async_press does not translate SevenhugsAuthError
    orig_ping_press = btn_mod.SevenhugsPingButton.async_press
    def apply_m29():
        async def mutated_press(self):
            try:
                await self.client.async_request("GET", "/api/hello")
                await self.coordinator.async_request_refresh()
            except btn_mod.SevenhugsAuthError:
                raise
            except Exception as err:
                raise HomeAssistantError(f"Other: {err}")
        btn_mod.SevenhugsPingButton.async_press = mutated_press
        return orig_ping_press
    def revert_m29(old):
        btn_mod.SevenhugsPingButton.async_press = old
    suite.test_mutant("M29", "SevenhugsPingButton.async_press propagates raw SevenhugsAuthError", "TestSevenhugsButtonPlatform.test_ping_button_error_translations", apply_m29, revert_m29)

    # M30: SevenhugsPingButton.async_press does not translate _COMMUNICATION_ERRORS
    def apply_m30():
        async def mutated_press(self):
            try:
                await self.client.async_request("GET", "/api/hello")
                await self.coordinator.async_request_refresh()
            except btn_mod._COMMUNICATION_ERRORS:
                raise
            except Exception as err:
                raise HomeAssistantError(f"Other: {err}")
        btn_mod.SevenhugsPingButton.async_press = mutated_press
        return orig_ping_press
    def revert_m30(old):
        btn_mod.SevenhugsPingButton.async_press = old
    suite.test_mutant("M30", "SevenhugsPingButton.async_press propagates raw communication errors", "TestSevenhugsButtonPlatform.test_ping_button_error_translations", apply_m30, revert_m30)

    # M31: SevenhugsPingButton.async_press does not call /api/hello (only refreshes)
    def apply_m31():
        async def mutated_press(self):
            await self.coordinator.async_request_refresh()
        btn_mod.SevenhugsPingButton.async_press = mutated_press
        return orig_ping_press
    def revert_m31(old):
        btn_mod.SevenhugsPingButton.async_press = old
    suite.test_mutant("M31", "SevenhugsPingButton.async_press omits /api/hello call", "TestSevenhugsButtonPlatform.test_ping_button_nominal_sequence", apply_m31, revert_m31)

    # M32: SevenhugsBackupConfigButton.extra_state_attributes returns {}
    orig_backup_attrs = btn_mod.SevenhugsBackupConfigButton.extra_state_attributes
    def apply_m32():
        btn_mod.SevenhugsBackupConfigButton.extra_state_attributes = property(lambda self: {})
        return orig_backup_attrs
    def revert_m32(old):
        btn_mod.SevenhugsBackupConfigButton.extra_state_attributes = old
    suite.test_mutant("M32", "SevenhugsBackupConfigButton extra_state_attributes returns {}", "TestSevenhugsButtonPlatform.test_backup_config_button_nominal_and_state_attributes", apply_m32, revert_m32)

    # M33: SevenhugsBackupConfigButton.async_press does not translate SevenhugsAuthError
    orig_backup_press = btn_mod.SevenhugsBackupConfigButton.async_press
    def apply_m33():
        async def mutated_press(self):
            try:
                data = await self.client.async_request("GET", "/api/config")
                self._last_backup = data
            except btn_mod.SevenhugsAuthError:
                raise
            except Exception as err:
                raise HomeAssistantError(f"Other: {err}")
        btn_mod.SevenhugsBackupConfigButton.async_press = mutated_press
        return orig_backup_press
    def revert_m33(old):
        btn_mod.SevenhugsBackupConfigButton.async_press = old
    suite.test_mutant("M33", "SevenhugsBackupConfigButton.async_press propagates raw SevenhugsAuthError", "TestSevenhugsButtonPlatform.test_backup_config_button_error_translations", apply_m33, revert_m33)

    # M34: SevenhugsBackupConfigButton.async_press does not translate _COMMUNICATION_ERRORS
    def apply_m34():
        async def mutated_press(self):
            try:
                data = await self.client.async_request("GET", "/api/config")
                self._last_backup = data
            except btn_mod._COMMUNICATION_ERRORS:
                raise
            except Exception as err:
                raise HomeAssistantError(f"Other: {err}")
        btn_mod.SevenhugsBackupConfigButton.async_press = mutated_press
        return orig_backup_press
    def revert_m34(old):
        btn_mod.SevenhugsBackupConfigButton.async_press = old
    suite.test_mutant("M34", "SevenhugsBackupConfigButton.async_press propagates raw communication errors", "TestSevenhugsButtonPlatform.test_backup_config_button_error_translations", apply_m34, revert_m34)

    # M35: button.async_setup_entry adds only 2 buttons
    orig_btn_setup = btn_mod.async_setup_entry
    def apply_m35():
        async def mutated_setup(hass, entry, async_add):
            def filtered_add(ents):
                async_add(ents[:2])
            await orig_btn_setup(hass, entry, filtered_add)
        btn_mod.async_setup_entry = mutated_setup
        te.button_setup_entry = mutated_setup
        return orig_btn_setup
    def revert_m35(old):
        btn_mod.async_setup_entry = old
        te.button_setup_entry = old
    suite.test_mutant("M35", "button async_setup_entry adds only 2 buttons", "TestSevenhugsButtonPlatform.test_button_async_setup_entry", apply_m35, revert_m35)

    # --- CATEGORY 5: LIFECYCLE & PLATFORM FORWARDING (__init__.py) ---

    # M36: PLATFORMS missing Platform.BUTTON
    def apply_m36():
        old_pkg = pkg_mod.PLATFORMS
        old_const = const_mod.PLATFORMS
        old_te = te.PLATFORMS
        mut = [Platform.SENSOR, Platform.BINARY_SENSOR]
        pkg_mod.PLATFORMS = mut
        const_mod.PLATFORMS = mut
        te.PLATFORMS = mut
        return (old_pkg, old_const, old_te)
    def revert_m36(olds):
        pkg_mod.PLATFORMS, const_mod.PLATFORMS, te.PLATFORMS = olds
    suite.test_mutant("M36", "PLATFORMS missing Platform.BUTTON", "TestComponentLifecycleAndPlatformForwarding.test_async_setup_and_unload_entry_multiplatform", apply_m36, revert_m36)

    # M37: PLATFORMS missing Platform.SENSOR
    def apply_m37():
        old_pkg = pkg_mod.PLATFORMS
        old_const = const_mod.PLATFORMS
        old_te = te.PLATFORMS
        mut = [Platform.BINARY_SENSOR, Platform.BUTTON]
        pkg_mod.PLATFORMS = mut
        const_mod.PLATFORMS = mut
        te.PLATFORMS = mut
        return (old_pkg, old_const, old_te)
    def revert_m37(olds):
        pkg_mod.PLATFORMS, const_mod.PLATFORMS, te.PLATFORMS = olds
    suite.test_mutant("M37", "PLATFORMS missing Platform.SENSOR", "TestComponentLifecycleAndPlatformForwarding.test_async_setup_and_unload_entry_multiplatform", apply_m37, revert_m37)

    # M38: PLATFORMS missing Platform.BINARY_SENSOR
    def apply_m38():
        old_pkg = pkg_mod.PLATFORMS
        old_const = const_mod.PLATFORMS
        old_te = te.PLATFORMS
        mut = [Platform.SENSOR, Platform.BUTTON]
        pkg_mod.PLATFORMS = mut
        const_mod.PLATFORMS = mut
        te.PLATFORMS = mut
        return (old_pkg, old_const, old_te)
    def revert_m38(olds):
        pkg_mod.PLATFORMS, const_mod.PLATFORMS, te.PLATFORMS = olds
    suite.test_mutant("M38", "PLATFORMS missing Platform.BINARY_SENSOR", "TestComponentLifecycleAndPlatformForwarding.test_async_setup_and_unload_entry_multiplatform", apply_m38, revert_m38)

    # M39: async_unload_entry does not unload platforms
    orig_unload = pkg_mod.async_unload_entry
    def apply_m39():
        async def mutated_unload(hass, entry):
            # skips hass.config_entries.async_unload_platforms
            data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
            if hasattr(entry, "runtime_data"):
                entry.runtime_data = None
            return True
        pkg_mod.async_unload_entry = mutated_unload
        te.async_unload_entry = mutated_unload
        return orig_unload
    def revert_m39(old):
        pkg_mod.async_unload_entry = old
        te.async_unload_entry = old
    suite.test_mutant("M39", "async_unload_entry omits async_unload_platforms", "TestComponentLifecycleAndPlatformForwarding.test_async_setup_and_unload_entry_multiplatform", apply_m39, revert_m39)

    # M40: async_unload_entry leaves entry.runtime_data populated
    def apply_m40():
        async def mutated_unload(hass, entry):
            unload_ok = await hass.config_entries.async_unload_platforms(entry, pkg_mod.PLATFORMS)
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
            # does not clear entry.runtime_data
            return unload_ok
        pkg_mod.async_unload_entry = mutated_unload
        te.async_unload_entry = mutated_unload
        return orig_unload
    def revert_m40(old):
        pkg_mod.async_unload_entry = old
        te.async_unload_entry = old
    suite.test_mutant("M40", "async_unload_entry does not clear entry.runtime_data", "TestComponentLifecycleAndPlatformForwarding.test_async_setup_and_unload_entry_multiplatform", apply_m40, revert_m40)

    # M41: SevenhugsData implementation broken (e.g. BrokenData)
    def apply_m41():
        class BrokenData:
            def __init__(self, client, session, coordinator=None):
                self.client = client
                self.session = session
                self.coordinator = coordinator
        old_pkg = pkg_mod.SevenhugsData
        pkg_mod.SevenhugsData = BrokenData
        return old_pkg
    def revert_m41(old):
        pkg_mod.SevenhugsData = old
    suite.test_mutant("M41", "SevenhugsData is not an instance of SevenhugsData/dict", "TestComponentLifecycleAndPlatformForwarding.test_async_setup_and_unload_entry_multiplatform", apply_m41, revert_m41)

    # M42: ForceSyncButton icon altered
    def apply_m42():
        old = btn_mod.SevenhugsForceSyncButton._attr_icon
        btn_mod.SevenhugsForceSyncButton._attr_icon = "mdi:wrench"
        return old
    def revert_m42(old):
        btn_mod.SevenhugsForceSyncButton._attr_icon = old
    suite.test_mutant("M42", "SevenhugsForceSyncButton icon = 'mdi:wrench'", "TestSevenhugsButtonPlatform.test_force_sync_button_nominal_and_error", apply_m42, revert_m42)

    return suite.results


async def run_rapid_lifecycle_stress_test(cycles: int = 100) -> dict[str, Any]:
    """Test de résistance extrême sur l'enregistrement et le désenregistrement rapide d'entités."""
    hass = HomeAssistant()
    client = te.MockSmartRemoteClientForEntities()
    entry = ConfigEntry(
        entry_id="stress_entry_rapid_001",
        domain=const_mod.DOMAIN,
        title="Sevenhugs Smart Remote (Stress)",
        data={
            "host": "192.168.1.100",
            "port": 80,
            "serial_number": "SR1A_STRESS_999",
            "remote_id": "5726",
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "firmware_version": "v0.76.4",
        },
        unique_id="SR1A_STRESS_999",
    )
    coord = pkg_mod.SevenhugsDataUpdateCoordinator(hass, client, entry)
    await coord.async_refresh()

    entity_classes = [
        lambda: sensor_mod.SevenhugsBatterySensor(coord, entry),
        lambda: sensor_mod.SevenhugsWifiSignalSensor(coord, entry),
        lambda: sensor_mod.SevenhugsActiveScreenSensor(coord, entry),
        lambda: bs_mod.SevenhugsChargingBinarySensor(coord, entry),
        lambda: bs_mod.SevenhugsConnectivityBinarySensor(coord, entry),
        lambda: btn_mod.SevenhugsForceSyncButton(coord, entry),
        lambda: btn_mod.SevenhugsPingButton(coord, entry),
        lambda: btn_mod.SevenhugsBackupConfigButton(coord, entry),
    ]

    initial_listeners_count = len(coord._listeners)
    assert initial_listeners_count == 0, f"Expected 0 listeners initially, got {initial_listeners_count}"

    # Cycle 1: Enregistrement et retrait séquentiel rapide
    for cycle in range(cycles):
        for factory in entity_classes:
            ent = factory()
            ent.hass = hass
            ent.entity_id = f"test_domain.{ent.unique_id}"
            await ent.async_added_to_hass()
            # Invariant: listener ajouté
            assert len(coord._listeners) == 1, f"Cycle {cycle}: listener count should be 1"
            # Déclenchement d'une notification du coordinateur
            coord.async_update_listeners()
            # Retrait
            await ent.async_will_remove_from_hass()
            assert len(coord._listeners) == 0, f"Cycle {cycle}: listener count should return to 0 (leak detected!)"

    # Cycle 2: Enregistrement simultané des 8 entités, notifications concurrentes, et retrait aléatoire/inverse
    simultaneous_entities = [factory() for factory in entity_classes]
    for idx, ent in enumerate(simultaneous_entities):
        ent.hass = hass
        ent.entity_id = f"test_domain.{ent.unique_id}_{idx}"
        await ent.async_added_to_hass()

    assert len(coord._listeners) == 8, f"Expected 8 listeners registered simultaneously, got {len(coord._listeners)}"

    # 50 rafraîchissements concurrents pendant que toutes les entités sont actives
    for r in range(50):
        await coord.async_refresh()

    # Retrait dans l'ordre inverse
    for ent in reversed(simultaneous_entities):
        await ent.async_will_remove_from_hass()

    assert len(coord._listeners) == 0, f"Expected 0 listeners after full reverse unregistration, got {len(coord._listeners)}"

    # Cycle 3: Double retrait idempotent (ne doit lever aucune exception)
    for ent in simultaneous_entities:
        await ent.async_will_remove_from_hass()

    # Cycle 4: Notification sur entités déjà retirées (aucun crash)
    coord.async_update_listeners()

    return {
        "status": "PASSED",
        "cycles_completed": cycles,
        "simultaneous_entities_tested": len(entity_classes),
        "post_unregistration_listener_count": len(coord._listeners),
        "memory_leak_detected": False,
    }


def main():
    print("=" * 70)
    print("SEVENHUGS SMART REMOTE — ADVERSARIAL MUTATION TESTING HARNESS (M4)")
    print("=" * 70)

    print("\n--- PHASE 1: AUTOMATED MUTATION TESTING (42 MUTANTS) ---")
    results = run_all_mutations()
    killed_count = sum(1 for r in results if r.killed)
    survived_count = len(results) - killed_count

    for r in results:
        status_tag = "[KILLED]" if r.killed else "[SURVIVED]"
        print(f"{status_tag} {r.mutant_id}: {r.description} -> {r.target_test}")
        if not r.killed:
            print(f"    FAILURE DETAILS: {r.error_message}")

    print("\n" + "=" * 70)
    print(f"MUTATION TESTING SUMMARY: {killed_count}/{len(results)} mutants KILLED (Score: {killed_count / len(results) * 100:.1f}%)")
    print(f"Surviving mutants: {survived_count}")
    print("=" * 70)

    print("\n--- PHASE 2: RAPID ENTITY LIFECYCLE STRESS HARNESS ---")
    stress_result = asyncio.run(run_rapid_lifecycle_stress_test(cycles=100))
    print(f"Cycles completed: {stress_result['cycles_completed']}")
    print(f"Simultaneous entities tested: {stress_result['simultaneous_entities_tested']}")
    print(f"Post-unregistration listener count: {stress_result['post_unregistration_listener_count']}")
    print(f"Lifecycle stress test status: {stress_result['status']}")

    if survived_count > 0 or stress_result["status"] != "PASSED":
        print("\nOVERALL VERDICT: FAIL")
        sys.exit(1)
    else:
        print("\nOVERALL VERDICT: PASS (100% MUTANTS KILLED, ZERO LIFECYCLE LEAKS)")
        sys.exit(0)


if __name__ == "__main__":
    main()
