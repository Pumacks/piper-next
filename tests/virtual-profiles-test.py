#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from piper.ratbagd import RatbagdButton, RatbagdMacro, RatbagdResolution  # noqa: E402
from piper.profilenames import ProfileNameStore  # noqa: E402
from piper.virtualprofiles import (
    VirtualProfileError,
    VirtualProfileStore,
    apply_snapshot,
    apply_virtual_profile,
    snapshot_profile,
)  # noqa: E402


class FakeResolution:
    CAP_DISABLE = RatbagdResolution.CAP_DISABLE

    def __init__(self, dpi=800):
        self.resolution = (dpi,)
        self.resolutions = [400, 800, 1600, 3200]
        self.is_active = True
        self.is_default = True
        self.is_disabled = False
        self.capabilities = [self.CAP_DISABLE]

    def set_disabled(self, value):
        self.is_disabled = value

    def set_default(self):
        self.is_default = True

    def set_active(self):
        self.is_active = True


class FakeButton:
    def __init__(self, mapping=1):
        self.action_type = RatbagdButton.ActionType.BUTTON
        self.action_types = [int(action) for action in RatbagdButton.ActionType]
        self.mapping = mapping
        self.special = None
        self.key = None
        self.macro = None

    def disable(self):
        self.action_type = RatbagdButton.ActionType.NONE


class FakeLed:
    def __init__(self):
        self.mode = 1
        self.modes = [0, 1, 2, 3]
        self.color = (10, 20, 30)
        self.brightness = 100
        self.effect_duration = 500


class FakeProfile:
    def __init__(self):
        self.index = 0
        self.name = "Onboard"
        self.report_rate = 1000
        self.report_rates = [125, 500, 1000]
        self.angle_snapping = 0
        self.debounce = 4
        self.debounces = [4, 8]
        self.resolutions = [FakeResolution()]
        self.buttons = [FakeButton()]
        self.leds = [FakeLed()]


class VirtualProfileTest(unittest.TestCase):
    def test_snapshot_can_be_applied_to_another_slot(self):
        source = FakeProfile()
        # Devices may return valid intermediate values that are absent from
        # libratbag's sampled list of suggested resolutions.
        source.resolutions[0].resolution = (2050,)
        settings = snapshot_profile(source)
        target = FakeProfile()
        target.report_rate = 125
        target.resolutions[0].resolution = (1600,)
        target.buttons[0].mapping = 2
        target.leds[0].color = (0, 0, 0)

        apply_snapshot(settings, target)

        self.assertEqual(target.report_rate, 1000)
        self.assertEqual(target.resolutions[0].resolution, (2050,))
        self.assertEqual(target.buttons[0].mapping, 1)
        self.assertEqual(target.leds[0].color, (10, 20, 30))
        self.assertEqual(target.name, "Onboard")

    def test_toggle_hold_macro_wraps_a_mouse_button_in_a_repeat(self):
        from evdev import ecodes

        macro = RatbagdMacro.toggle_hold(ecodes.BTN_LEFT)

        self.assertEqual(
            [event[0] for event in macro.keys],
            [
                RatbagdButton.Macro.KEY_PRESS,
                RatbagdButton.Macro.WAIT,
                RatbagdButton.Macro.REPEAT_UNTIL_CANCELED,
                RatbagdButton.Macro.KEY_RELEASE,
            ],
        )
        self.assertEqual(macro.keys[0][1], ecodes.BTN_LEFT)
        self.assertEqual(macro.keys[-1][1], ecodes.BTN_LEFT)
        self.assertIn("BTN_LEFT", str(macro))
        self.assertIn("⟳toggle", str(macro))

    def test_incompatible_layout_is_rejected_before_changes(self):
        source = FakeProfile()
        settings = snapshot_profile(source)
        settings["buttons"].append(settings["buttons"][0])
        target = FakeProfile()

        with self.assertRaises(VirtualProfileError):
            apply_snapshot(settings, target)

        self.assertEqual(target.report_rate, 1000)

    def test_store_filters_by_model_and_deletes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "virtual_profiles.json"
            store = VirtualProfileStore(path)
            saved = store.save("Work", "g502", FakeProfile())
            store.save("Other mouse", "other", FakeProfile())

            self.assertEqual(
                [p["name"] for p in store.list_for_model("g502")], ["Work"]
            )
            store.delete(saved["id"])
            self.assertEqual(store.list_for_model("g502"), [])
            self.assertEqual(json.loads(path.read_text())["version"], 1)

    def test_old_g502_profiles_are_copied_for_both_g502_x_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "virtual_profiles.json"
            store = VirtualProfileStore(path)
            source = store.save("Desktop", "usb:046d:407f:0", FakeProfile())
            store.migrate_g502_profiles()
            store.migrate_g502_profiles()

            wired = store.list_for_model("usb:046d:c098:0")
            wireless = store.list_for_model("usb:046d:409f:0")

            self.assertEqual(len(wired), 1)
            self.assertEqual(wired, wireless)
            self.assertNotEqual(wired[0]["id"], source["id"])
            self.assertEqual(wired[0]["migrated_from_id"], source["id"])
            self.assertEqual(len(store.list_for_model("usb:046d:407f:0")), 1)
            self.assertEqual(len(store.list_for_model("usb:046d:c08d:0")), 1)
            self.assertEqual(len(json.loads(path.read_text())["profiles"]), 2)

    def test_migration_skips_names_already_saved_on_g502_x(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VirtualProfileStore(Path(directory) / "virtual_profiles.json")
            store.save("DayZ", "usb:046d:407f:0", FakeProfile())
            native = store.save("DayZ", "usb:046d:409f:0", FakeProfile())
            store.migrate_g502_profiles()

            self.assertEqual(store.list_for_model("usb:046d:409f:0"), [native])

    def test_applied_slot_edits_reach_the_matching_virtual_profile(self):
        # Regression: launchers reload the snapshot on every game start, so
        # button changes applied to the slot must be stored in it.
        with tempfile.TemporaryDirectory() as directory:
            store = VirtualProfileStore(Path(directory) / "virtual_profiles.json")
            store.save("DayZ", "usb:046d:407f:0", FakeProfile())
            store.save("Desktop", "usb:046d:407f:0", FakeProfile())
            store.migrate_g502_profiles()
            slot = FakeProfile()
            slot.buttons[0].mapping = 5

            synced = store.sync_from_slot("dayz", "usb:046d:409f:0", slot)

            self.assertEqual(synced["name"], "DayZ")
            self.assertNotIn("migrated_from_model", synced)
            reloaded = store.find_by_name("usb:046d:c098:0", "DayZ")
            self.assertEqual(len(reloaded), 1)
            self.assertEqual(reloaded[0]["settings"]["buttons"][0]["value"], 5)
            desktop = store.find_by_name("usb:046d:c098:0", "Desktop")[0]
            self.assertEqual(desktop["settings"]["buttons"][0]["value"], 1)
            # The old G502 keeps its own copy.
            old = store.find_by_name("usb:046d:407f:0", "DayZ")[0]
            self.assertEqual(old["settings"]["buttons"][0]["value"], 1)
            # Migration must not bring the old copy back as a duplicate.
            store.migrate_g502_profiles()
            self.assertEqual(len(store.find_by_name("usb:046d:409f:0", "DayZ")), 1)

    def test_slot_without_matching_virtual_profile_is_not_synced(self):
        with tempfile.TemporaryDirectory() as directory:
            store = VirtualProfileStore(Path(directory) / "virtual_profiles.json")
            store.save("Work", "g502", FakeProfile())

            self.assertIsNone(store.sync_from_slot("Profile 1", "g502", FakeProfile()))

    def test_old_g502_profile_keeps_its_lighting(self):
        source = FakeProfile()
        source.leds[0].color = (1, 2, 3)
        saved = {"settings": snapshot_profile(source)}
        target = FakeProfile()

        apply_virtual_profile(saved, "usb:046d:407f:0", target)

        self.assertEqual(target.leds[0].color, (1, 2, 3))

    def test_migrated_g502_profile_keeps_target_lighting(self):
        source = FakeProfile()
        source.buttons[0].mapping = 2
        migrated = {
            "migrated_from_model": "usb:046d:407f:0",
            "settings": snapshot_profile(source),
        }
        target = FakeProfile()
        target.leds = []

        apply_virtual_profile(migrated, "usb:046d:409f:0", target)

        self.assertEqual(target.buttons[0].mapping, 2)
        self.assertEqual(target.leds, [])

    def test_local_profile_names_are_persistent_and_slot_specific(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile_names.json"
            store = ProfileNameStore(path)
            device = type("Device", (), {"name": "Test Mouse"})()
            first = FakeProfile()
            second = FakeProfile()
            second.index = 1

            store.set(device, first, "Gaming")
            store.set(device, second, "Desktop")

            reloaded = ProfileNameStore(path)
            self.assertEqual(reloaded.get(device, first), "Gaming")
            self.assertEqual(reloaded.get(device, second), "Desktop")


if __name__ == "__main__":
    unittest.main()
