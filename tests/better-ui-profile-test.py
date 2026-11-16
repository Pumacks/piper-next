#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from piper.better_ui import BetterUiApplication  # noqa: E402


class FakeProfile:
    def __init__(self, dirty=False):
        self.dirty = dirty


class FakeDevice:
    __hash__ = None

    def __init__(self, profiles, active_profile=None):
        self.profiles = profiles
        self.active_profile = active_profile
        self.connected = None
        self.disconnected = None
        self.commit_count = 0
        self.factory_reset_callback = None
        self.name = "Fake mouse"

    def commit(self):
        self.commit_count += 1

    def factory_reset(self, callback):
        self.factory_reset_callback = callback

    def connect(self, signal, callback):
        self.connected = (signal, callback)
        return 42

    def disconnect(self, handler):
        self.disconnected = handler


class FakeButton:
    def __init__(self):
        self.sensitive = False

    def get_sensitive(self):
        return self.sensitive


class FakeNames:
    def __init__(self):
        self.cleared = []

    def clear_device(self, device):
        self.cleared.append(device)


class FakeApplication:
    def __init__(self, device=None):
        self._ratbag = None
        self._selected_device = device
        self._selected_profile = None
        self._device_drafts = {}
        self._apply_button = FakeButton()
        self._profile_signal_device = None
        self._profile_signal_handler = None
        self._resync_signal_handler = None
        self._pending_factory_reset = None
        self._profile_names = FakeNames()
        self.toasts = []
        self._draft = None
        self.apply_sensitive = None
        self.shown_device = None

    def _on_active_profile_changed(self, device, profile):
        BetterUiApplication._on_active_profile_changed(self, device, profile)

    @staticmethod
    def _make_draft(profile):
        return {"profile": profile}

    def _set_apply_sensitive(self, sensitive):
        self.apply_sensitive = sensitive
        self._apply_button.sensitive = sensitive

    def _show_device(self, device):
        self.shown_device = device

    def _watch_active_profile(self, device):
        BetterUiApplication._watch_active_profile(self, device)

    def _on_device_resync(self, device):
        BetterUiApplication._on_device_resync(self, device)

    def _select_device(self, device):
        BetterUiApplication._select_device(self, device)

    def _on_factory_reset_finished(self, device, result, error):
        BetterUiApplication._on_factory_reset_finished(self, device, result, error)

    @staticmethod
    def _current_profile(device):
        return BetterUiApplication._current_profile(device)

    def _add_toast(self, message):
        self.toasts.append(message)


class BetterUiProfileTest(unittest.TestCase):
    def test_current_profile_uses_device_active_profile(self):
        first = FakeProfile()
        active = FakeProfile()
        device = FakeDevice([first, active], active)

        self.assertIs(BetterUiApplication._current_profile(device), active)

    def test_current_profile_falls_back_to_first_slot(self):
        first = FakeProfile()
        device = FakeDevice([first], None)

        self.assertIs(BetterUiApplication._current_profile(device), first)

    def test_active_profile_change_opens_that_profile(self):
        old = FakeProfile()
        active = FakeProfile(dirty=True)
        device = FakeDevice([old, active], active)
        app = FakeApplication(device)

        BetterUiApplication._on_active_profile_changed(app, device, active)

        self.assertIs(app._selected_profile, active)
        self.assertEqual(app._draft, {"profile": active})
        self.assertTrue(app.apply_sensitive)
        self.assertIs(app.shown_device, device)

    def test_active_profile_watch_moves_to_new_device(self):
        first = FakeDevice([FakeProfile()])
        second = FakeDevice([FakeProfile()])
        app = FakeApplication()

        BetterUiApplication._watch_active_profile(app, first)
        BetterUiApplication._watch_active_profile(app, second)

        self.assertEqual(first.connected[0], "active-profile-changed")
        self.assertEqual(first.disconnected, 42)
        self.assertEqual(second.connected[0], "active-profile-changed")
        self.assertIs(app._profile_signal_device, second)
        self.assertEqual(app._profile_signal_handler, 42)

    def test_successful_profile_switch_commits_device(self):
        old = FakeProfile()
        active = FakeProfile(dirty=True)
        device = FakeDevice([old, active], active)
        app = FakeApplication(device)

        BetterUiApplication._on_profile_switch_finished(app, device, active, None)

        self.assertEqual(device.commit_count, 1)
        self.assertIs(app._selected_profile, active)

    def test_factory_reset_reloads_device_and_forgets_local_names(self):
        old = FakeProfile()
        active = FakeProfile()
        device = FakeDevice([old, active], active)
        app = FakeApplication(device)
        app._ratbag = type("Ratbag", (), {"devices": [device]})()
        BetterUiApplication._select_device(app, device)
        app._selected_profile = old
        app._draft = {"profile": old, "pending_name": "stale"}
        app._set_apply_sensitive(True)

        BetterUiApplication._start_factory_reset(app, device)

        self.assertIs(app._pending_factory_reset, device)
        self.assertFalse(app.apply_sensitive)
        self.assertIsNotNone(device.factory_reset_callback)

        # ratbagd emits Resync before the method returns
        BetterUiApplication._on_device_resync(app, device)
        self.assertEqual(app.toasts, [])

        device.factory_reset_callback(0, None)

        self.assertIsNone(app._pending_factory_reset)
        self.assertEqual(app._profile_names.cleared, [device])
        self.assertIs(app._selected_profile, active)
        self.assertEqual(app._draft, {"profile": active})
        self.assertEqual(app.toasts, ["Onboard profiles restored to factory defaults"])
        self.assertEqual(device.commit_count, 0)

    def test_failed_factory_reset_keeps_local_names(self):
        profile = FakeProfile()
        device = FakeDevice([profile], profile)
        app = FakeApplication(device)
        app._ratbag = type("Ratbag", (), {"devices": [device]})()
        BetterUiApplication._select_device(app, device)

        BetterUiApplication._start_factory_reset(app, device)
        device.factory_reset_callback(None, RuntimeError("no such method"))

        self.assertIsNone(app._pending_factory_reset)
        self.assertEqual(app._profile_names.cleared, [])
        self.assertEqual(len(app.toasts), 1)
        self.assertIn("ratbagd", app.toasts[0])

    def test_resync_reloads_the_selected_device(self):
        first = FakeProfile()
        second = FakeProfile()
        device = FakeDevice([first, second], second)
        app = FakeApplication(device)
        app._ratbag = type("Ratbag", (), {"devices": [device]})()
        BetterUiApplication._select_device(app, device)
        app._selected_profile = first
        app._draft = {"profile": first, "pending_name": "stale"}

        BetterUiApplication._on_device_resync(app, device)

        self.assertIs(app._selected_profile, second)
        self.assertEqual(app._draft, {"profile": second})
        self.assertEqual(app.toasts, ["Settings were reloaded from the mouse"])

    def test_switching_mice_keeps_their_drafts_separate(self):
        first_profile = FakeProfile()
        second_profile = FakeProfile()
        first = FakeDevice([first_profile], first_profile)
        second = FakeDevice([second_profile], second_profile)
        app = FakeApplication()
        app._ratbag = type("Ratbag", (), {"devices": [first, second]})()

        BetterUiApplication._select_device(app, first)
        app._draft["pending_name"] = "First mouse draft"
        app._set_apply_sensitive(True)
        BetterUiApplication._select_device(app, second)

        self.assertIs(app.shown_device, second)
        self.assertIs(app._selected_profile, second_profile)
        self.assertEqual(app._draft, {"profile": second_profile})
        self.assertFalse(app.apply_sensitive)
        self.assertEqual(first.commit_count, 0)
        self.assertEqual(second.commit_count, 0)

        BetterUiApplication._select_device(app, first)

        self.assertIs(app.shown_device, first)
        self.assertIs(app._selected_profile, first_profile)
        self.assertEqual(app._draft["pending_name"], "First mouse draft")
        self.assertTrue(app.apply_sensitive)


if __name__ == "__main__":
    unittest.main()
