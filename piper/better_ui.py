# SPDX-License-Identifier: GPL-2.0-or-later

"""GTK 4/libadwaita foundation for Piper's next-generation interface.

This module intentionally has no dependency on the existing GTK 3 page
widgets. GTK 3 and GTK 4 cannot share a process, so the device model is reused
first while individual configuration pages are ported incrementally.
"""

import re
import shlex
import shutil
from gettext import gettext as _
from pathlib import Path
from typing import Optional

import cairo
import gi
from evdev import ecodes

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Rsvg", "2.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango, Rsvg  # noqa

from .ratbagd import (
    Ratbagd,
    RatbagdButton,
    RatbagdDBusTimeoutError,
    RatbagdDevice,
    RatbagdIncompatibleError,
    RatbagdLed,
    RatbagdMacro,
    RatbagError,
    RatbagCapabilityError,
    RatbagdProfile,
    RatbagdUnavailableError,
    N_,
    evcode_to_str,
)
from .profilenames import ProfileNameStore
from .svg import get_svg
from .virtualprofiles import (
    VirtualProfileError,
    VirtualProfileStore,
    apply_virtual_profile,
)


class MousePreview(Gtk.DrawingArea):
    """Scalable mouse artwork with a model-specific highlighted control."""

    def __init__(self, device: RatbagdDevice) -> None:
        super().__init__()
        self._handle: Optional[Rsvg.Handle] = None
        self._highlight_element: Optional[str] = None
        try:
            svg_data = get_svg(device.model)
            assert svg_data is not None
            self._handle = Rsvg.Handle.new_from_data(svg_data)
        except (FileNotFoundError, GLib.Error):
            pass

        self.set_content_width(360)
        self.set_content_height(440)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)

    @property
    def available(self) -> bool:
        return self._handle is not None and self._handle.has_sub("#Device")

    def highlight_button(self, button_index: Optional[int]) -> None:
        element = None if button_index is None else f"#button{button_index}"
        if self._handle is not None and element is not None:
            if not self._handle.has_sub(element):
                element = None
        if element != self._highlight_element:
            self._highlight_element = element
            self.queue_draw()

    def _draw(self, _area, cr: cairo.Context, width: int, height: int) -> None:
        if not self.available:
            return
        assert self._handle is not None
        svg_width = self._handle.props.width
        svg_height = self._handle.props.height
        scale = min(width / svg_width, height / svg_height) * 0.9
        x = (width - svg_width * scale) / 2
        y = (height - svg_height * scale) / 2

        cr.save()
        cr.translate(x, y)
        cr.scale(scale, scale)
        self._handle.render_cairo_sub(cr, id="#Device")
        if self._highlight_element is not None:
            surface = cr.get_target().create_similar(
                cairo.CONTENT_COLOR_ALPHA, svg_width, svg_height
            )
            mask = cairo.Context(surface)
            self._handle.render_cairo_sub(mask, id=self._highlight_element)
            found, accent = self.get_style_context().lookup_color("accent_color")
            if found:
                cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.72)
            else:
                cr.set_source_rgba(0.21, 0.52, 0.89, 0.72)
            cr.mask_surface(surface, 0, 0)
        cr.restore()


class ButtonCaptureDialog(Adw.Window):
    """Capture one keyboard key or an ordered key press/release macro."""

    _XORG_KEYCODE_OFFSET = 8
    _MAX_MACRO_EVENTS = 128

    def __init__(
        self,
        parent,
        capture_macro: bool,
        on_saved,
        on_cancelled,
        toggle_hold: bool = False,
    ) -> None:
        super().__init__(transient_for=parent, modal=True)
        self._capture_macro = capture_macro
        self._toggle_hold = toggle_hold
        self._on_saved = on_saved
        self._on_cancelled = on_cancelled
        self._saved = False
        self._key: Optional[int] = None
        self._events = []
        self._pressed_keys = set()
        self._last_event_time: Optional[int] = None

        if toggle_hold:
            title = _("Hold a key until pressed again")
        elif capture_macro:
            title = _("Record a macro")
        else:
            title = _("Assign a keyboard key")
        self.set_title(title)
        self.set_default_size(520, 340)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda _button: self.close())
        header.pack_start(cancel)
        self._save_button = Gtk.Button(label=_("Use assignment"))
        self._save_button.add_css_class("suggested-action")
        self._save_button.set_sensitive(False)
        self._save_button.connect("clicked", self._on_save_clicked)
        header.pack_end(self._save_button)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(30)
        content.set_margin_bottom(30)
        content.set_margin_start(30)
        content.set_margin_end(30)
        if toggle_hold:
            instruction_text = _(
                "Press the keyboard key, or click the box below with the "
                "mouse button, that the macro key should hold."
            )
        elif capture_macro:
            instruction_text = _(
                "Type the key sequence (mouse clicks on the box below are "
                "recorded too), then click Use assignment."
            )
        else:
            instruction_text = _("Press the keyboard key you want to assign.")
        instruction = Gtk.Label(label=instruction_text, wrap=True)
        instruction.add_css_class("title-3")
        content.append(instruction)

        preview_frame = Gtk.Frame()
        self._preview = Gtk.Label(
            label=_("Waiting for keyboard input…"),
            wrap=True,
            # a selectable label would open its own menu on right click
            selectable=not (capture_macro or toggle_hold),
            margin_top=24,
            margin_bottom=24,
            margin_start=18,
            margin_end=18,
        )
        preview_frame.set_child(self._preview)
        content.append(preview_frame)
        if capture_macro or toggle_hold:
            clicks = Gtk.GestureClick()
            clicks.set_button(0)
            clicks.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            clicks.connect("pressed", self._on_mouse_pressed)
            clicks.connect("released", self._on_mouse_released)
            preview_frame.add_controller(clicks)

        clear = Gtk.Button(label=_("Clear recording"), halign=Gtk.Align.CENTER)
        clear.connect("clicked", self._on_clear_clicked)
        content.append(clear)
        toolbar.set_content(content)
        self.set_content(toolbar)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        keys.connect("key-released", self._on_key_released)
        self.add_controller(keys)
        self.connect("close-request", self._on_close_request)

    @classmethod
    def _evdev_keycode(cls, hardware_keycode: int) -> Optional[int]:
        if not cls._XORG_KEYCODE_OFFSET <= hardware_keycode <= 255:
            return None
        keycode = hardware_keycode - cls._XORG_KEYCODE_OFFSET
        try:
            evcode_to_str(keycode)
        except KeyError:
            return None
        return keycode

    def _on_key_pressed(self, _controller, _keyval, keycode, _state) -> bool:
        key = self._evdev_keycode(keycode)
        if key is None:
            self._preview.set_label(_("That key cannot be stored by this device."))
            return True
        if key in self._pressed_keys:
            return True
        self._pressed_keys.add(key)

        if self._capture_macro:
            self._append_macro_event(RatbagdButton.Macro.KEY_PRESS, key)
        elif self._key is None:
            self._key = key
        self._update_preview()
        return True

    # GDK button numbers to evdev button codes, as used in ratbag macros.
    _GDK_BUTTONS = {
        1: ecodes.BTN_LEFT,
        2: ecodes.BTN_MIDDLE,
        3: ecodes.BTN_RIGHT,
        8: ecodes.BTN_SIDE,
        9: ecodes.BTN_EXTRA,
    }

    def _on_mouse_pressed(self, gesture, _n_press, _x, _y) -> None:
        button = self._GDK_BUTTONS.get(gesture.get_current_button())
        if button is None:
            self._preview.set_label(_("That mouse button cannot be stored by this device."))
            return
        if button in self._pressed_keys:
            return
        self._pressed_keys.add(button)
        if self._capture_macro:
            self._append_macro_event(RatbagdButton.Macro.KEY_PRESS, button)
        else:
            self._key = button
        self._update_preview()

    def _on_mouse_released(self, gesture, _n_press, _x, _y) -> None:
        button = self._GDK_BUTTONS.get(gesture.get_current_button())
        if button is None:
            return
        self._pressed_keys.discard(button)
        if self._capture_macro:
            self._append_macro_event(RatbagdButton.Macro.KEY_RELEASE, button)
            self._update_preview()

    def _on_key_released(self, _controller, _keyval, keycode, _state) -> None:
        key = self._evdev_keycode(keycode)
        if key is None:
            return
        self._pressed_keys.discard(key)
        if self._capture_macro:
            self._append_macro_event(RatbagdButton.Macro.KEY_RELEASE, key)
            self._update_preview()

    def _append_macro_event(self, event_type, key: int) -> None:
        if len(self._events) >= self._MAX_MACRO_EVENTS:
            self._preview.set_label(_("The maximum macro length has been reached."))
            return

        now = GLib.get_monotonic_time()
        if self._last_event_time is not None:
            delay = min(round((now - self._last_event_time) / 1000), 60000)
            if delay >= 10 and len(self._events) < self._MAX_MACRO_EVENTS - 1:
                self._events.append((RatbagdButton.Macro.WAIT, delay))
        self._events.append((event_type, key))
        self._last_event_time = now

    def _update_preview(self) -> None:
        if self._capture_macro:
            if not self._events:
                text = _("Waiting for keyboard input…")
            else:
                text = str(RatbagdMacro.from_ratbag(self._events))
            self._save_button.set_sensitive(bool(self._events))
        else:
            if self._key is None and self._toggle_hold:
                text = _("Waiting for a keyboard key or mouse click…")
            elif self._key is None:
                text = _("Waiting for keyboard input…")
            elif self._toggle_hold:
                text = _("Press once: hold {key}. Press again: release it.").format(
                    key=evcode_to_str(self._key)
                )
            else:
                macro = RatbagdMacro()
                macro.append(RatbagdButton.Macro.KEY_PRESS, self._key)
                macro.append(RatbagdButton.Macro.KEY_RELEASE, self._key)
                text = str(macro)
            self._save_button.set_sensitive(self._key is not None)
        self._preview.set_label(text)

    def _on_clear_clicked(self, _button) -> None:
        self._key = None
        self._events.clear()
        self._pressed_keys.clear()
        self._last_event_time = None
        self._update_preview()

    def _on_save_clicked(self, _button) -> None:
        if self._capture_macro:
            if not self._events:
                return
            value = RatbagdMacro.from_ratbag(self._events)
            action_type = RatbagdButton.ActionType.MACRO
        else:
            if self._key is None:
                return
            value = self._key
            action_type = RatbagdButton.ActionType.KEY
        self._saved = True
        self.close()
        self._on_saved(action_type, value)

    def _on_close_request(self, _window) -> bool:
        if not self._saved:
            self._on_cancelled()
        return False


class BetterUiApplication(Adw.Application):
    """An experimental GTK 4/libadwaita shell backed by ratbagd."""

    _CAPTURE_KEY = object()
    _CAPTURE_MACRO = object()
    _CAPTURE_TOGGLE_HOLD = object()

    def __init__(self, ratbagd_api_version: int) -> None:
        super().__init__(
            application_id="org.freedesktop.Piper.BetterUi",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self._required_ratbagd_version = ratbagd_api_version
        self._ratbag: Optional[Ratbagd] = None
        self._window: Optional[Adw.ApplicationWindow] = None
        self._toast_overlay: Optional[Adw.ToastOverlay] = None
        self._apply_button: Optional[Gtk.Button] = None
        self._profile_button: Optional[Gtk.MenuButton] = None
        self._device_dropdown: Optional[Gtk.DropDown] = None
        self._device_options = []
        self._updating_device_dropdown = False
        self._device_drafts = {}
        self._selected_device: Optional[RatbagdDevice] = None
        self._selected_profile: Optional[RatbagdProfile] = None
        self._profile_signal_device: Optional[RatbagdDevice] = None
        self._resync_signal_handler: Optional[int] = None
        self._pending_factory_reset: Optional[RatbagdDevice] = None
        self._profile_signal_handler: Optional[int] = None
        self._battery_device: Optional[RatbagdDevice] = None
        self._battery_proxy: Optional[Gio.DBusProxy] = None
        self._battery_signal_handler: Optional[int] = None
        self._battery_retry_source: Optional[int] = None
        self._battery_retry_count = 0
        self._battery_icon: Optional[Gtk.Image] = None
        self._battery_label: Optional[Gtk.Label] = None
        self._battery_indicator_box: Optional[Gtk.Box] = None
        self._draft = {}
        self._virtual_profiles = VirtualProfileStore(
            Path(GLib.get_user_config_dir()) / "piper" / "virtual_profiles.json"
        )
        try:
            # Make profiles saved from the older G502 models available to the
            # G502 X as well; a broken library is reported when it is opened.
            self._virtual_profiles.migrate_g502_profiles()
        except VirtualProfileError:
            pass
        self._profile_names = ProfileNameStore()
        self._content_page = Adw.NavigationPage.new(
            self._status_page(
                _("Connect a supported mouse"),
                _("Piper Next will display its profiles here."),
            ),
            _("Piper Next"),
        )

    def do_activate(self) -> None:
        if self._window is None:
            self._window = self._build_window()
            self._connect_ratbagd()
        elif self._ratbag is not None:
            # A second launch activates the existing application instance.
            # Re-read the device state so the profile shown is the one the
            # mouse is currently using.
            self._refresh_devices()
        self._window.present()

    def _build_window(self) -> Adw.ApplicationWindow:
        window = Adw.ApplicationWindow(application=self)
        window.set_default_size(1160, 760)
        window.set_icon_name("org.freedesktop.Piper")

        toolbar_view = Adw.ToolbarView()
        header_bar = Adw.HeaderBar()
        header_bar.set_title_widget(Adw.WindowTitle(title="Piper Next"))
        self._device_dropdown = Gtk.DropDown()
        self._device_dropdown.set_visible(False)
        self._device_dropdown.set_tooltip_text(_("Choose a mouse to configure"))
        self._device_dropdown.connect("notify::selected", self._on_device_selected)
        header_bar.pack_start(self._device_dropdown)
        self._profile_button = Gtk.MenuButton()
        self._profile_button.set_visible(False)
        self._profile_button.set_tooltip_text(_("Switch profile"))
        header_bar.pack_start(self._profile_button)
        self._apply_button = Gtk.Button(label=_("Apply"))
        self._apply_button.add_css_class("suggested-action")
        self._apply_button.set_sensitive(False)
        self._apply_button.connect("clicked", self._on_apply_clicked)
        header_bar.pack_end(self._apply_button)
        toolbar_view.add_top_bar(header_bar)

        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._content_page)
        toolbar_view.set_content(self._toast_overlay)
        window.set_content(toolbar_view)
        return window

    def _connect_ratbagd(self) -> None:
        try:
            self._ratbag = Ratbagd(self._required_ratbagd_version)
        except RatbagdUnavailableError:
            self._show_error(
                _("Cannot connect to ratbagd"),
                _("Make sure ratbagd is running and your user can access it."),
            )
            return
        except RatbagdIncompatibleError as error:
            self._show_error(
                _("Incompatible ratbagd version"),
                _("Piper and libratbag need compatible D-Bus API versions."),
            )
            return

        self._ratbag.connect("device-added", self._on_devices_changed)
        self._ratbag.connect("device-removed", self._on_devices_changed)
        self._ratbag.connect("daemon-disappeared", self._on_daemon_disappeared)
        self._refresh_devices()

    def _refresh_devices(self) -> None:
        if self._ratbag is None or not self._ratbag.devices:
            self._selected_device = None
            self._selected_profile = None
            self._device_drafts.clear()
            self._refresh_device_dropdown()
            self._watch_active_profile(None)
            self._watch_battery(None)
            self._set_apply_sensitive(False)
            if self._profile_button is not None:
                self._profile_button.set_visible(False)
            self._content_page.set_child(
                self._status_page(
                    _("No supported mouse found"),
                    _("Connect a device supported by libratbag."),
                )
            )
            return

        device = self._selected_device
        if device not in self._ratbag.devices:
            device = self._ratbag.devices[0]
        self._device_drafts = {
            saved_id: state
            for saved_id, state in self._device_drafts.items()
            if any(state[0] is current for current in self._ratbag.devices)
        }
        self._select_device(device)
        self._refresh_device_dropdown()

    def _refresh_device_dropdown(self) -> None:
        if self._device_dropdown is None:
            return
        self._device_options = list(self._ratbag.devices) if self._ratbag else []
        labels = [f"{device.name} ({device.model})" for device in self._device_options]
        for index, label in enumerate(labels):
            if labels.count(label) > 1:
                labels[index] = f"{label} — {self._device_options[index].id}"
        self._updating_device_dropdown = True
        try:
            self._device_dropdown.set_model(Gtk.StringList.new(labels))
            if self._selected_device in self._device_options:
                self._device_dropdown.set_selected(
                    self._device_options.index(self._selected_device)
                )
            self._device_dropdown.set_visible(len(self._device_options) > 1)
        finally:
            self._updating_device_dropdown = False

    def _on_device_selected(self, dropdown, _pspec) -> None:
        if self._updating_device_dropdown:
            return
        index = dropdown.get_selected()
        if index < len(self._device_options):
            self._select_device(self._device_options[index])

    def _select_device(self, device: RatbagdDevice) -> None:
        previous = self._selected_device
        if (
            previous is not None
            and previous is not device
            and self._ratbag is not None
            and previous in self._ratbag.devices
        ):
            self._device_drafts[id(previous)] = (
                previous,
                self._selected_profile,
                self._draft,
                self._apply_button.get_sensitive() if self._apply_button else False,
            )
        self._selected_device = device
        self._watch_active_profile(device)
        if previous is not device or self._selected_profile not in device.profiles:
            saved = self._device_drafts.get(id(device))
            if saved is not None and saved[0] is device and saved[1] in device.profiles:
                self._selected_profile, self._draft, pending = saved[1:]
            else:
                self._selected_profile = self._current_profile(device)
                self._draft = self._make_draft(self._selected_profile)
                pending = False
            self._set_apply_sensitive(
                pending or any(profile.dirty for profile in device.profiles)
            )
        self._show_device(device)

    @staticmethod
    def _current_profile(device) -> RatbagdProfile:
        """Return the profile the device is using, with a safe fallback."""
        return device.active_profile or device.profiles[0]

    def _watch_active_profile(self, device: Optional[RatbagdDevice]) -> None:
        if device is self._profile_signal_device:
            return
        if self._profile_signal_device is not None:
            if self._resync_signal_handler is not None:
                self._profile_signal_device.disconnect(self._resync_signal_handler)
            if self._profile_signal_handler is not None:
                self._profile_signal_device.disconnect(self._profile_signal_handler)
        self._profile_signal_device = device
        self._profile_signal_handler = None
        self._resync_signal_handler = None
        if device is not None:
            self._resync_signal_handler = device.connect(
                "resync", self._on_device_resync
            )
            self._profile_signal_handler = device.connect(
                "active-profile-changed", self._on_active_profile_changed
            )

    def _on_device_resync(self, device) -> None:
        """ratbagd re-read the device: drop local drafts and show its state."""
        self._device_drafts.pop(id(device), None)
        if device is not self._selected_device:
            return
        self._selected_profile = None
        self._select_device(device)
        if self._pending_factory_reset is not device:
            self._add_toast(_("Settings were reloaded from the mouse"))

    def _on_active_profile_changed(self, device, profile) -> None:
        if device is not self._selected_device:
            return
        self._selected_profile = profile
        self._draft = self._make_draft(profile)
        self._set_apply_sensitive(any(item.dirty for item in device.profiles))
        self._show_device(device)

    def _device_page(self, device: RatbagdDevice) -> Gtk.Paned:
        page = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        page.add_tick_callback(self._center_paned_on_first_frame)

        preview_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        preview_panel.set_margin_top(24)
        preview_panel.set_margin_bottom(24)
        preview_panel.set_margin_start(24)
        preview_panel.set_margin_end(24)
        device_heading = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=12
        )
        device_name = Gtk.Label(label=device.name, xalign=0, hexpand=True)
        device_name.add_css_class("title-2")
        device_heading.append(device_name)
        device_heading.append(self._battery_indicator(device))
        device_model = Gtk.Label(label=device.model, xalign=0)
        device_model.add_css_class("dim-label")
        device_model.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        preview_panel.append(device_heading)
        preview_panel.append(device_model)

        mouse_preview = MousePreview(device)
        if mouse_preview.available:
            preview_panel.append(mouse_preview)
            hint = Gtk.Label(
                label=_("Hover a button setting to locate it on the mouse."),
                wrap=True,
            )
            hint.add_css_class("dim-label")
            preview_panel.append(hint)
        else:
            preview_panel.append(
                self._status_page(
                    _("Mouse illustration unavailable"),
                    _("Button assignments can still be edited on the right."),
                )
            )
        page.set_start_child(preview_panel)
        page.set_resize_start_child(False)

        preferences = Adw.PreferencesPage()
        preferences.set_title(_("Device settings"))
        preferences.set_vexpand(True)
        assert self._selected_profile is not None
        if self._selected_profile.buttons:
            preferences.add(self._buttons_group(mouse_preview))
        if self._selected_profile.resolutions:
            preferences.add(self._resolution_group())
        if self._selected_profile.leds:
            preferences.add(self._leds_group())
        advanced = self._advanced_group()
        if advanced is not None:
            preferences.add(advanced)
        preferences.add(self._onboard_memory_group(device))
        page.set_end_child(preferences)
        page.set_resize_end_child(True)
        page.set_shrink_end_child(False)
        return page

    def _battery_indicator(self, device: RatbagdDevice) -> Gtk.Box:
        indicator = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            valign=Gtk.Align.CENTER,
        )
        icon = Gtk.Image.new_from_icon_name("battery-missing-symbolic")
        label = Gtk.Label(label=_("Unknown"))
        label.add_css_class("dim-label")
        indicator.append(icon)
        indicator.append(label)
        self._battery_icon = icon
        self._battery_label = label
        self._battery_indicator_box = indicator
        self._watch_battery(device)
        self._update_battery_indicator()
        return indicator

    def _watch_battery(self, device: Optional[RatbagdDevice]) -> None:
        if device is self._battery_device and self._battery_proxy is not None:
            return
        if self._battery_retry_source is not None:
            GLib.Source.remove(self._battery_retry_source)
            self._battery_retry_source = None
        if self._battery_proxy is not None and self._battery_signal_handler is not None:
            self._battery_proxy.disconnect(self._battery_signal_handler)
        self._battery_device = device
        self._battery_proxy = (
            None if device is None else self._find_upower_battery(device)
        )
        self._battery_signal_handler = None
        if self._battery_proxy is not None:
            self._battery_signal_handler = self._battery_proxy.connect(
                "g-properties-changed", self._on_battery_properties_changed
            )
        elif device is not None:
            self._battery_retry_count = 0
            self._battery_retry_source = GLib.timeout_add_seconds(
                2, self._retry_battery, device
            )

    def _retry_battery(self, device: RatbagdDevice) -> bool:
        if device is not self._battery_device:
            self._battery_retry_source = None
            return False
        self._battery_retry_count += 1
        proxy = self._find_upower_battery(device)
        if proxy is None and self._battery_retry_count < 5:
            return True
        self._battery_retry_source = None
        if proxy is not None:
            self._battery_proxy = proxy
            self._battery_signal_handler = proxy.connect(
                "g-properties-changed", self._on_battery_properties_changed
            )
            self._update_battery_indicator()
        return False

    @staticmethod
    def _find_upower_battery(device) -> Optional[Gio.DBusProxy]:
        try:
            upower = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM,
                Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                None,
                "org.freedesktop.UPower",
                "/org/freedesktop/UPower",
                "org.freedesktop.UPower",
                None,
            )
            result = upower.call_sync(
                "EnumerateDevices",
                None,
                Gio.DBusCallFlags.NO_AUTO_START,
                2000,
                None,
            )
            paths = result.unpack()[0]
            for path in paths:
                proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SYSTEM,
                    Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                    None,
                    "org.freedesktop.UPower",
                    path,
                    "org.freedesktop.UPower.Device",
                    None,
                )
                model = BetterUiApplication._proxy_property(proxy, "Model", "")
                device_type = BetterUiApplication._proxy_property(proxy, "Type", 0)
                present = BetterUiApplication._proxy_property(proxy, "IsPresent", False)
                if present and device_type in (5, 6, 12) and (
                    BetterUiApplication._device_names_match(model, device.name)
                ):
                    return proxy
        except GLib.Error:
            pass
        return None

    @staticmethod
    def _proxy_property(proxy, name, default):
        value = proxy.get_cached_property(name)
        return default if value is None else value.unpack()

    @staticmethod
    def _device_names_match(first: str, second: str) -> bool:
        ignored = {"gaming", "logitech", "mouse", "wireless"}

        def meaningful_words(name):
            return {
                word
                for word in re.findall(r"[a-z0-9]+", name.casefold())
                if word not in ignored
            }

        first_words = meaningful_words(first)
        second_words = meaningful_words(second)
        return bool(first_words and second_words) and (
            first_words <= second_words or second_words <= first_words
        )

    @staticmethod
    def _battery_icon_name(percentage: int, charging: bool) -> str:
        level = min(100, max(0, percentage // 10 * 10))
        suffix = "-charging" if charging else ""
        return f"battery-level-{level}{suffix}-symbolic"

    @staticmethod
    def _battery_snapshot(proxy) -> tuple[str, str, str]:
        if proxy is None:
            return (
                "battery-missing-symbolic",
                _("Unknown"),
                _("Battery level unavailable"),
            )
        percentage = round(
            max(
                0.0,
                min(
                    100.0,
                    BetterUiApplication._proxy_property(
                        proxy, "Percentage", 0.0
                    ),
                ),
            )
        )
        battery_state = BetterUiApplication._proxy_property(proxy, "State", 0)
        icon = BetterUiApplication._battery_icon_name(
            percentage, battery_state == 1
        )
        states = {
            1: _("charging"),
            2: _("discharging"),
            4: _("fully charged"),
            5: _("waiting to charge"),
            6: _("waiting to discharge"),
        }
        state = states.get(battery_state, _("unknown"))
        return icon, _("{}%").format(percentage), _("Battery: {}% ({})").format(
            percentage, state
        )

    def _on_battery_properties_changed(self, *_args) -> None:
        self._update_battery_indicator()

    def _update_battery_indicator(self) -> None:
        if (
            self._battery_icon is None
            or self._battery_label is None
            or self._battery_indicator_box is None
        ):
            return
        icon, label, tooltip = self._battery_snapshot(self._battery_proxy)
        self._battery_icon.set_from_icon_name(icon)
        self._battery_label.set_label(label)
        self._battery_indicator_box.set_tooltip_text(tooltip)

    def _refresh_profile_menu(self, device) -> None:
        assert self._profile_button is not None
        assert self._selected_profile is not None
        self._profile_button.set_label(
            self._profile_display_name(device, self._selected_profile)
        )
        self._profile_button.set_visible(True)

        popover = Gtk.Popover()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(8)
        content.set_margin_end(8)
        heading = Gtk.Label(label=_("Profiles"), xalign=0)
        heading.add_css_class("heading")
        content.append(heading)
        profiles = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        profiles.add_css_class("boxed-list")
        profiles.connect("row-activated", self._on_profile_row_activated, device)
        for profile in device.profiles:
            profiles.append(self._profile_menu_row(device, profile))
        content.append(profiles)
        popover.set_child(content)
        self._profile_button.set_popover(popover)

    def _profile_menu_row(self, device, profile) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.profile = profile  # type: ignore[attr-defined]
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        content.set_margin_start(8)
        content.set_margin_end(8)

        name = self._profile_display_name(device, profile)
        name_stack = Gtk.Stack(hexpand=True)
        label = Gtk.Label(label=name, xalign=0, hexpand=True)
        if profile.disabled:
            label.add_css_class("dim-label")
        entry = Gtk.Entry(text=name, hexpand=True, activates_default=True)
        entry.set_max_length(64)
        entry.connect(
            "activate",
            self._on_profile_name_entry_activated,
            device,
            profile,
            label,
            name_stack,
        )
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_profile_name_key_pressed, name_stack)
        entry.add_controller(keys)
        name_stack.add_named(label, "label")
        name_stack.add_named(entry, "entry")
        name_stack.set_visible_child_name("label")
        content.append(name_stack)

        edit = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        edit.add_css_class("flat")
        edit.set_tooltip_text(_("Rename profile"))
        edit.connect("clicked", self._on_profile_edit_clicked, name_stack, entry)
        content.append(edit)
        virtual = self._profile_virtual_menu(device, profile)
        content.append(virtual)
        row.set_child(content)
        return row

    def _profile_virtual_menu(self, device, profile) -> Gtk.MenuButton:
        button = Gtk.MenuButton()
        button.set_icon_name("object-flip-horizontal-symbolic")
        button.add_css_class("flat")
        button.set_tooltip_text(_("Replace this slot with a virtual profile"))
        popover = Gtk.Popover()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(8)
        content.set_margin_end(8)
        heading = Gtk.Label(label=_("Choose a virtual profile"), xalign=0)
        heading.add_css_class("heading")
        content.append(heading)
        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        search = Gtk.SearchEntry(
            placeholder_text=_("Search virtual profiles"), hexpand=True
        )
        search_row.append(search)
        create = Gtk.Button.new_from_icon_name("list-add-symbolic")
        create.set_tooltip_text(_("Create virtual profile from this slot"))
        create.connect(
            "clicked", self._on_create_virtual_profile_clicked, device, profile
        )
        search_row.append(create)
        content.append(search_row)
        choices = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        choices.add_css_class("boxed-list")
        choices.set_filter_func(
            lambda row: search.get_text().casefold() in row.virtual_name.casefold()
        )
        search.connect("search-changed", lambda _entry: choices.invalidate_filter())
        choices.connect(
            "row-activated",
            self._on_virtual_profile_choice_activated,
            device,
            profile,
            popover,
        )
        try:
            virtual_profiles = self._virtual_profiles.list_for_model(device.model)
        except VirtualProfileError:
            virtual_profiles = []
        choice_count = 0
        for virtual_profile in virtual_profiles:
            name = virtual_profile.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            choice = Gtk.ListBoxRow()
            choice.virtual_name = name  # type: ignore[attr-defined]
            choice.virtual_profile = virtual_profile  # type: ignore[attr-defined]
            choice_content = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=6,
                margin_top=4,
                margin_bottom=4,
                margin_start=8,
                margin_end=4,
            )
            choice_content.append(Gtk.Label(label=name, xalign=0, hexpand=True))
            copy = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
            copy.add_css_class("flat")
            copy.set_tooltip_text(_("Copy Steam launch option"))
            copy.connect(
                "clicked",
                self._on_copy_launch_option_clicked,
                device,
                virtual_profile,
            )
            choice_content.append(copy)
            delete = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            delete.add_css_class("flat")
            delete.set_tooltip_text(_("Delete virtual profile"))
            delete.connect(
                "clicked",
                self._on_confirm_delete_virtual_profile_clicked,
                device,
                virtual_profile,
            )
            choice_content.append(delete)
            choice.set_child(choice_content)
            choices.append(choice)
            choice_count += 1
        if choice_count == 0:
            empty = Gtk.Label(label=_("No virtual profiles saved"))
            empty.add_css_class("dim-label")
            content.append(empty)
        else:
            scroller = Gtk.ScrolledWindow(
                min_content_width=300, max_content_height=320
            )
            scroller.set_propagate_natural_height(True)
            scroller.set_child(choices)
            content.append(scroller)
        popover.set_child(content)
        button.set_popover(popover)
        return button

    _DESKTOP_PROFILE_NAMES = ("desktop", "default")

    @staticmethod
    def _steam_launch_option(
        device,
        virtual_profile: dict,
        virtual_profiles: list,
        multiple_devices: bool,
        executable: str = "piper-profile",
    ) -> str:
        """Build the Steam launch option that loads ``virtual_profile`` for a
        game and switches back to the desktop profile when the game exits."""
        parts = [shlex.quote(executable)]
        if multiple_devices:
            parts += ["--device", shlex.quote(device.model)]
        parts += ["--profile", shlex.quote(virtual_profile["name"]), "--virtual"]
        restore = None
        for candidate in virtual_profiles:
            candidate_name = candidate.get("name")
            if (
                isinstance(candidate_name, str)
                and candidate_name.casefold()
                in BetterUiApplication._DESKTOP_PROFILE_NAMES
                and candidate is not virtual_profile
            ):
                restore = candidate_name
                break
        if restore is not None:
            parts += ["--restore", shlex.quote(restore)]
        parts += ["--", "%command%"]
        return " ".join(parts)

    @staticmethod
    def _piper_profile_executable() -> str:
        found = shutil.which("piper-profile")
        if found:
            return found
        local = Path.home() / ".local" / "bin" / "piper-profile"
        if local.exists():
            return str(local)
        return "piper-profile"

    def _on_copy_launch_option_clicked(self, _button, device, virtual_profile) -> None:
        try:
            virtual_profiles = self._virtual_profiles.list_for_model(device.model)
        except VirtualProfileError:
            virtual_profiles = []
        text = self._steam_launch_option(
            device,
            virtual_profile,
            virtual_profiles,
            multiple_devices=bool(self._ratbag and len(self._ratbag.devices) > 1),
            executable=self._piper_profile_executable(),
        )
        display = Gdk.Display.get_default()
        if display is None:
            self._add_toast(_("No display to copy to"))
            return
        display.get_clipboard().set(text)
        self._add_toast(_("Steam launch option copied"))

    @staticmethod
    def _center_paned_on_first_frame(paned, _frame_clock) -> bool:
        width = paned.get_width()
        if width <= 1:
            return True
        paned.set_position(width // 2)
        return False

    def _buttons_group(self, preview: MousePreview) -> Adw.PreferencesGroup:
        assert self._selected_profile is not None
        group = Adw.PreferencesGroup(
            title=_("Button assignments"),
            description=_(
                "Point at a row to highlight the matching physical button."
            ),
        )
        for button in self._selected_profile.buttons:
            actions = self._button_actions(button)
            if actions:
                row = Adw.ComboRow(
                    title=self._button_title(
                        button.index,
                        self._selected_device.model if self._selected_device else None,
                    )
                )
                row.set_model(Gtk.StringList.new([action[2] for action in actions]))
                row.set_selected(self._selected_button_action(button, actions))
                row.connect(
                    "notify::selected", self._on_button_action_changed, button, actions
                )
                if button.action_type == RatbagdButton.ActionType.MACRO:
                    macro_text = str(button.macro)
                    row.set_subtitle(_("Macro: {}").format(macro_text))
                    row.set_subtitle_lines(2)
                    row.set_tooltip_text(macro_text)
            else:
                row = Adw.ActionRow(
                    title=self._button_title(button.index),
                    subtitle=_("Not configurable"),
                )
            motion = Gtk.EventControllerMotion()
            motion.connect(
                "enter", self._on_button_hover_enter, button.index, preview
            )
            motion.connect("leave", self._on_button_hover_leave, preview)
            row.add_controller(motion)
            focus = Gtk.EventControllerFocus()
            focus.connect("enter", self._on_button_focus_enter, button.index, preview)
            focus.connect("leave", self._on_button_focus_leave, preview)
            row.add_controller(focus)
            group.add(row)
        return group

    @staticmethod
    def _button_title(index: int, model: Optional[str] = None) -> str:
        family = BetterUiApplication._button_family(model)
        if family is not None:
            description = family.get(index)
            if description is not None:
                return _(description)
            return _("Button {}").format(index + 1)
        description = RatbagdButton.BUTTON_DESCRIPTION.get(index)
        if description is not None:
            return _(description)
        return _("Button {}").format(index + 1)

    # Button order of the Logitech G502 family as reported by the firmware
    # (verified against the G502 X LIGHTSPEED factory profile: index 4 is the
    # thumb "sniper" DPI shift, 6/7 are the wheel tilts, 9/10 the DPI pair).
    _G502_BUTTON_NAMES = {
        0: N_("Left button"),
        1: N_("Right button"),
        2: N_("Wheel click"),
        3: N_("Thumb back button (G4)"),
        4: N_("Thumb sniper button (G6, DPI shift)"),
        5: N_("Thumb forward button (G5)"),
        6: N_("Wheel tilt left"),
        7: N_("Wheel tilt right"),
        8: N_("Top button behind the wheel (G9)"),
        9: N_("DPI up button (G8)"),
        10: N_("DPI down button (G7)"),
    }
    _BUTTON_FAMILIES = {
        # G502 X, G502 X LIGHTSPEED, G502 X PLUS, G502 HERO
        "usb:046d:c099": _G502_BUTTON_NAMES,
        "usb:046d:c098": _G502_BUTTON_NAMES,
        "usb:046d:409f": _G502_BUTTON_NAMES,
        "usb:046d:c095": _G502_BUTTON_NAMES,
        "usb:046d:4099": _G502_BUTTON_NAMES,
        "usb:046d:c08b": _G502_BUTTON_NAMES,
        "usb:046d:c08d": _G502_BUTTON_NAMES,
        "usb:046d:407f": _G502_BUTTON_NAMES,
    }

    @staticmethod
    def _button_family(model: Optional[str]) -> Optional[dict]:
        if not model:
            return None
        return BetterUiApplication._BUTTON_FAMILIES.get(
            ":".join(model.split(":")[:3]).lower()
        )

    @staticmethod
    def _on_button_hover_enter(_motion, _x, _y, index, preview) -> None:
        preview.highlight_button(index)

    @staticmethod
    def _on_button_hover_leave(_motion, preview) -> None:
        preview.highlight_button(None)

    @staticmethod
    def _on_button_focus_enter(_focus, index, preview) -> None:
        preview.highlight_button(index)

    @staticmethod
    def _on_button_focus_leave(_focus, preview) -> None:
        preview.highlight_button(None)

    @staticmethod
    def _button_actions(button: RatbagdButton) -> list:
        actions = []
        if button.action_type == RatbagdButton.ActionType.KEY:
            actions.append(
                (
                    RatbagdButton.ActionType.KEY,
                    button.key,
                    _("Keyboard key: {}").format(evcode_to_str(button.key)),
                )
            )
        elif button.action_type == RatbagdButton.ActionType.MACRO:
            actions.append(
                (
                    RatbagdButton.ActionType.MACRO,
                    button.macro,
                    _("Macro"),
                )
            )
        if RatbagdButton.ActionType.KEY in button.action_types:
            actions.append(
                (
                    RatbagdButton.ActionType.KEY,
                    BetterUiApplication._CAPTURE_KEY,
                    _("Assign a keyboard key…"),
                )
            )
        if RatbagdButton.ActionType.MACRO in button.action_types:
            actions.append(
                (
                    RatbagdButton.ActionType.MACRO,
                    BetterUiApplication._CAPTURE_MACRO,
                    _("Record a macro…"),
                )
            )
            actions.append(
                (
                    RatbagdButton.ActionType.MACRO,
                    BetterUiApplication._CAPTURE_TOGGLE_HOLD,
                    _("Hold a key until pressed again…"),
                )
            )
        if RatbagdButton.ActionType.BUTTON in button.action_types:
            actions.extend(
                (
                    RatbagdButton.ActionType.BUTTON,
                    index + 1,
                    _("Mouse button {}").format(index + 1),
                )
                for index in range(16)
            )
        if RatbagdButton.ActionType.SPECIAL in button.action_types:
            actions.extend(
                (RatbagdButton.ActionType.SPECIAL, special, _(description))
                for special, description in RatbagdButton.SPECIAL_DESCRIPTION.items()
                if special
                not in (
                    RatbagdButton.ActionSpecial.INVALID,
                    RatbagdButton.ActionSpecial.UNKNOWN,
                )
            )
        if RatbagdButton.ActionType.NONE in button.action_types:
            actions.append((RatbagdButton.ActionType.NONE, None, _("Disabled")))
        return actions

    @staticmethod
    def _selected_button_action(button: RatbagdButton, actions: list) -> int:
        value = None
        if button.action_type == RatbagdButton.ActionType.BUTTON:
            value = button.mapping
        elif button.action_type == RatbagdButton.ActionType.SPECIAL:
            value = button.special
        elif button.action_type == RatbagdButton.ActionType.KEY:
            value = button.key
        elif button.action_type == RatbagdButton.ActionType.MACRO:
            value = button.macro
        for index, (action_type, action_value, _label) in enumerate(actions):
            if action_type == button.action_type and action_value == value:
                return index
        return 0

    def _resolution_group(self) -> Adw.PreferencesGroup:
        assert self._selected_profile is not None
        group = Adw.PreferencesGroup(
            title=_("DPI stages"),
            description=_("Choose from the DPI values supported by this mouse."),
        )
        active_index = self._draft["active_resolution"]
        for index, resolution in enumerate(self._selected_profile.resolutions):
            dpi_values = list(resolution.resolutions)
            current_value = self._draft["resolutions"][index]
            # A few devices, including the G502, can report a valid current
            # DPI that is missing from libratbag's advertised suggestions.
            # Keep it selectable instead of failing device discovery.
            if current_value not in dpi_values:
                dpi_values.append(current_value)
                dpi_values.sort()
            dpi_row = Adw.ComboRow(title=_("DPI stage {}").format(index + 1))
            dpi_row.set_model(Gtk.StringList.new([str(value) for value in dpi_values]))
            dpi_row.set_selected(dpi_values.index(current_value))
            if index == active_index:
                dpi_row.set_subtitle(_("Active DPI"))
            dpi_row.connect("notify::selected", self._on_dpi_changed, index, dpi_values)

            active_button = Gtk.Button(label=_("Use"))
            active_button.set_valign(Gtk.Align.CENTER)
            active_button.set_sensitive(index != active_index)
            active_button.connect("clicked", self._on_active_dpi_clicked, index)
            dpi_row.add_suffix(active_button)
            if (
                resolution.CAP_DISABLE in resolution.capabilities
                and not resolution.is_active
            ):
                enabled = Gtk.Switch(valign=Gtk.Align.CENTER)
                enabled.set_active(not self._draft["resolution_disabled"][index])
                enabled.set_sensitive(not resolution.is_default)
                enabled.connect(
                    "notify::active", self._on_resolution_enabled_changed, index
                )
                dpi_row.add_suffix(enabled)
            group.add(dpi_row)
        return group

    def _leds_group(self) -> Adw.PreferencesGroup:
        assert self._selected_profile is not None
        group = Adw.PreferencesGroup(
            title=_("Lighting"),
            description=_("Configure each lighting zone without leaving this page."),
        )
        for index, led in enumerate(self._selected_profile.leds):
            led_draft = self._draft["leds"][index]
            expander = Adw.ExpanderRow(title=_("Lighting zone {}").format(index + 1))

            modes = list(led.modes)
            mode_row = Adw.ComboRow(title=_("Effect"))
            mode_row.set_model(
                Gtk.StringList.new(
                    [_(RatbagdLed.LED_DESCRIPTION[mode]) for mode in modes]
                )
            )
            mode_row.set_selected(modes.index(led_draft["mode"]))
            expander.add_row(mode_row)

            color_row = Adw.ActionRow(title=_("Color"))
            color_button = Gtk.ColorDialogButton.new(Gtk.ColorDialog.new())
            red, green, blue = led_draft["color"]
            color_button.set_rgba(
                Gdk.RGBA(red / 255.0, green / 255.0, blue / 255.0, 1.0)
            )
            color_button.set_valign(Gtk.Align.CENTER)
            color_button.connect("notify::rgba", self._on_led_color_changed, index)
            color_row.add_suffix(color_button)
            expander.add_row(color_row)

            brightness_row = Adw.ActionRow(title=_("Brightness"))
            brightness = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 255, 1)
            brightness.set_value(led_draft["brightness"])
            brightness.set_size_request(180, -1)
            brightness.set_valign(Gtk.Align.CENTER)
            brightness.set_draw_value(True)
            brightness.connect("value-changed", self._on_led_brightness_changed, index)
            brightness_row.add_suffix(brightness)
            expander.add_row(brightness_row)

            duration_row = Adw.ActionRow(
                title=_("Effect speed"),
                subtitle=_("Lower values animate faster"),
            )
            duration = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0, 10000, 100
            )
            duration.set_value(led_draft["effect_duration"])
            duration.set_size_request(180, -1)
            duration.set_valign(Gtk.Align.CENTER)
            duration.set_draw_value(True)
            duration.connect("value-changed", self._on_led_duration_changed, index)
            duration_row.add_suffix(duration)
            expander.add_row(duration_row)
            mode_row.connect(
                "notify::selected",
                self._on_led_mode_changed,
                index,
                modes,
                color_row,
                brightness_row,
                duration_row,
            )
            self._update_led_controls(
                led_draft["mode"], color_row, brightness_row, duration_row
            )
            group.add(expander)
        return group

    def _advanced_group(self) -> Optional[Adw.PreferencesGroup]:
        assert self._selected_profile is not None
        profile = self._selected_profile
        if (
            not profile.report_rates
            and not profile.debounces
            and profile.angle_snapping == -1
        ):
            return None

        group = Adw.PreferencesGroup(title=_("Advanced"))
        if profile.report_rates:
            rate_row = Adw.ComboRow(title=_("Polling rate"))
            rate_row.set_model(
                Gtk.StringList.new([str(rate) for rate in profile.report_rates])
            )
            rate_row.set_selected(
                profile.report_rates.index(self._draft["report_rate"])
            )
            rate_row.connect("notify::selected", self._on_report_rate_changed)
            group.add(rate_row)
        if profile.debounces:
            debounce_row = Adw.ComboRow(title=_("Debounce time"))
            debounce_row.set_model(
                Gtk.StringList.new(
                    [_("{} ms").format(value) for value in profile.debounces]
                )
            )
            debounce_row.set_selected(profile.debounces.index(self._draft["debounce"]))
            debounce_row.connect("notify::selected", self._on_debounce_changed)
            group.add(debounce_row)
        if profile.angle_snapping != -1:
            angle_row = Adw.SwitchRow(title=_("Angle snapping"))
            angle_row.set_active(self._draft["angle_snapping"] == 1)
            angle_row.connect("notify::active", self._on_angle_snapping_changed)
            group.add(angle_row)
        return group

    def _onboard_memory_group(self, device: RatbagdDevice) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=_("Onboard memory"))
        row = Adw.ActionRow(
            title=_("Factory reset"),
            subtitle=_(
                "Replace every onboard profile with the defaults the mouse "
                "shipped with. Virtual profiles in your library are kept."
            ),
        )
        button = Gtk.Button(label=_("Reset…"), valign=Gtk.Align.CENTER)
        button.add_css_class("destructive-action")
        button.set_sensitive(self._pending_factory_reset is None)
        button.connect("clicked", self._on_factory_reset_clicked, device)
        row.add_suffix(button)
        row.set_activatable_widget(button)
        group.add(row)
        return group

    def _on_factory_reset_clicked(self, _button, device) -> None:
        assert self._window is not None
        dialog = Adw.MessageDialog(
            transient_for=self._window,
            heading=_("Reset onboard profiles?"),
            body=_(
                "All onboard profiles of {device} are overwritten with the "
                "factory defaults and unapplied changes are discarded. Local "
                "profile names for this mouse are removed; virtual profiles "
                "are not affected."
            ).format(device=device.name),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reset", _("Reset to factory defaults"))
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_factory_reset_response, device)
        dialog.present()

    def _on_factory_reset_response(self, _dialog, response, device) -> None:
        if response != "reset":
            return
        self._start_factory_reset(device)

    def _start_factory_reset(self, device) -> None:
        if self._pending_factory_reset is not None:
            return
        self._pending_factory_reset = device
        self._set_apply_sensitive(False)
        device.factory_reset(
            lambda result, error: self._on_factory_reset_finished(
                device, result, error
            )
        )

    def _on_factory_reset_finished(self, device, result, error) -> None:
        if self._pending_factory_reset is device:
            self._pending_factory_reset = None
        if error is not None:
            if isinstance(error, RatbagCapabilityError):
                message = _("This mouse does not support a factory reset")
            elif isinstance(error, RatbagError):
                message = _("The mouse rejected the factory reset: {}").format(error)
            elif isinstance(error, RatbagdDBusTimeoutError):
                message = _("The factory reset timed out; the mouse may be asleep")
            else:
                message = _(
                    "The running ratbagd has no factory reset. "
                    "Rebuild it with tools/install-ratbagd-piper.sh."
                )
            self._add_toast(message)
            if device is self._selected_device:
                self._show_device(device)
            return
        self._profile_names.clear_device(device)
        self._device_drafts.pop(id(device), None)
        if device is self._selected_device:
            self._selected_profile = None
            self._select_device(device)
        self._add_toast(_("Onboard profiles restored to factory defaults"))

    def _profile_display_name(self, device, profile) -> str:
        return (
            self._profile_names.get(device, profile)
            or profile.name
            or _("Profile {}").format(profile.index + 1)
        )

    def _set_profile_name_from_virtual(self, device, profile, name: str) -> None:
        self._profile_names.set(device, profile, name)
        if RatbagdProfile.CAP_WRITABLE_NAME in profile.capabilities:
            profile.name = name
        if profile is self._selected_profile:
            self._draft["name"] = name

    def _make_draft(self, profile: RatbagdProfile) -> dict:
        assert self._selected_device is not None
        active_index = 0
        for index, resolution in enumerate(profile.resolutions):
            if resolution.is_active:
                active_index = index
                break
        return {
            "name": self._profile_display_name(self._selected_device, profile),
            "resolutions": [
                resolution.resolution[0] for resolution in profile.resolutions
            ],
            "resolution_disabled": [
                resolution.is_disabled for resolution in profile.resolutions
            ],
            "active_resolution": active_index,
            "report_rate": profile.report_rate,
            "debounce": profile.debounce,
            "angle_snapping": profile.angle_snapping,
            "leds": [
                {
                    "mode": led.mode,
                    "color": tuple(led.color),
                    "brightness": led.brightness,
                    "effect_duration": led.effect_duration,
                }
                for led in profile.leds
            ],
        }

    def _on_devices_changed(self, *_args) -> None:
        self._refresh_devices()

    def _on_profile_row_activated(self, _listbox, row, device) -> None:
        profile = row.profile  # type: ignore[attr-defined]
        if profile.disabled:
            if RatbagdProfile.CAP_DISABLE not in profile.capabilities:
                self._add_toast(_("This profile cannot be enabled"))
                return
            try:
                profile.disabled = False
            except (GLib.Error, RatbagError, ValueError):
                self._add_toast(_("Could not enable profile"))
                return
            self._set_apply_sensitive(True)
        self._selected_profile = profile
        self._draft = self._make_draft(profile)
        if self._profile_button is not None:
            popover = self._profile_button.get_popover()
            if popover is not None:
                popover.popdown()
        if profile.is_active:
            if profile.dirty:
                try:
                    device.commit()
                except (GLib.Error, RatbagdDBusTimeoutError, RatbagError):
                    self._add_toast(_("Could not apply profile switch"))
                    return
            self._set_apply_sensitive(any(item.dirty for item in device.profiles))
            self._show_device(device)
            return

        profile.set_active_async(
            lambda _result, error: self._on_profile_switch_finished(
                device, profile, error
            )
        )

    def _on_profile_switch_finished(self, device, profile, error) -> None:
        if error is None:
            try:
                device.commit()
            except (GLib.Error, RatbagdDBusTimeoutError, RatbagError) as commit_error:
                error = commit_error
        if error is not None:
            self._selected_profile = self._current_profile(device)
            self._add_toast(_("Could not switch profile"))
        else:
            self._selected_profile = profile
            self._add_toast(_("Profile switched"))
        self._draft = self._make_draft(self._selected_profile)
        self._set_apply_sensitive(any(item.dirty for item in device.profiles))
        self._show_device(device)

    @staticmethod
    def _on_profile_edit_clicked(_button, stack, entry) -> None:
        stack.set_visible_child_name("entry")
        entry.grab_focus()
        entry.select_region(0, -1)

    @staticmethod
    def _on_profile_name_key_pressed(_controller, keyval, _keycode, _state, stack):
        if keyval != Gdk.KEY_Escape:
            return False
        stack.set_visible_child_name("label")
        return True

    def _on_profile_name_entry_activated(
        self, entry, device, profile, label, stack
    ) -> None:
        name = entry.get_text().strip()
        if not name:
            self._add_toast(_("Profile name cannot be empty"))
            return
        try:
            self._profile_names.set(device, profile, name)
            if RatbagdProfile.CAP_WRITABLE_NAME in profile.capabilities:
                profile.name = name
                self._set_apply_sensitive(True)
        except (GLib.Error, OSError, RatbagError, ValueError):
            self._add_toast(_("Could not rename profile"))
            return
        if profile is self._selected_profile:
            self._draft["name"] = name
            if self._profile_button is not None:
                self._profile_button.set_label(name)
        label.set_text(name)
        stack.set_visible_child_name("label")
        self._add_toast(_("Profile renamed"))

    def _on_create_virtual_profile_clicked(self, _button, device, profile) -> None:
        assert self._window is not None
        dialog = Adw.MessageDialog(
            transient_for=self._window,
            heading=_("Create virtual profile"),
            body=_("Save the current settings from this onboard slot."),
        )
        entry = Gtk.Entry(
            text=self._profile_display_name(device, profile),
            placeholder_text=_("Virtual profile name"),
            activates_default=True,
        )
        entry.set_max_length(64)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("create", _("Create"))
        dialog.set_close_response("cancel")
        dialog.set_default_response("create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect(
            "response",
            self._on_create_virtual_profile_response,
            entry,
            device,
            profile,
        )
        dialog.present()

    def _on_create_virtual_profile_response(
        self, _dialog, response, entry, device, profile
    ) -> None:
        if response != "create":
            return
        name = entry.get_text().strip()
        if not name:
            self._add_toast(_("Enter a virtual profile name"))
            return
        try:
            self._virtual_profiles.save(name, device.model, profile)
        except VirtualProfileError as error:
            self._add_toast(str(error))
            return
        self._add_toast(_("Virtual profile created"))
        self._refresh_profile_menu(device)

    def _on_confirm_delete_virtual_profile_clicked(
        self, _button, device, virtual_profile
    ) -> None:
        assert self._window is not None
        name = virtual_profile.get("name", _("this virtual profile"))
        dialog = Adw.MessageDialog(
            transient_for=self._window,
            heading=_("Delete virtual profile?"),
            body=_("‘{}’ will be permanently removed.").format(name),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect(
            "response",
            self._on_delete_virtual_profile_response,
            device,
            virtual_profile,
        )
        dialog.present()

    def _on_delete_virtual_profile_response(
        self, _dialog, response, device, virtual_profile
    ) -> None:
        if response != "delete":
            return
        try:
            self._virtual_profiles.delete(virtual_profile["id"])
        except (KeyError, VirtualProfileError) as error:
            self._add_toast(str(error))
            return
        self._add_toast(_("Virtual profile deleted"))
        self._refresh_profile_menu(device)

    def _on_virtual_profile_choice_activated(
        self, _listbox, row, device, profile, popover
    ) -> None:
        self._on_load_virtual_into_profile_clicked(
            None, device, profile, row.virtual_profile, popover
        )

    def _on_load_virtual_into_profile_clicked(
        self, _button, device, profile, virtual_profile, popover
    ) -> None:
        try:
            virtual_name = virtual_profile["name"]
            if not isinstance(virtual_name, str) or not virtual_name.strip():
                raise VirtualProfileError("This virtual profile has an invalid name")
            apply_virtual_profile(virtual_profile, device.model, profile)
            self._set_profile_name_from_virtual(device, profile, virtual_name)
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            VirtualProfileError,
            GLib.Error,
        ) as error:
            self._add_toast(str(error))
            return
        popover.popdown()
        if profile is self._selected_profile:
            self._draft = self._make_draft(profile)
            self._show_device(device)
        else:
            self._refresh_profile_menu(device)
        self._set_apply_sensitive(True)
        self._add_toast(
            _("Virtual profile loaded into {}; press Apply to write it").format(
                self._profile_display_name(device, profile)
            )
        )

    def _on_dpi_changed(self, row, _pspec, index: int, dpi_values: list) -> None:
        selected = row.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION:
            return
        self._draft["resolutions"][index] = dpi_values[selected]
        self._set_apply_sensitive(True)

    def _on_resolution_enabled_changed(self, switch, _pspec, index: int) -> None:
        self._draft["resolution_disabled"][index] = not switch.get_active()
        self._set_apply_sensitive(True)

    def _on_button_action_changed(self, dropdown, _pspec, button, actions) -> None:
        selected = dropdown.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION:
            return
        action_type, value, _label = actions[selected]
        if value in (self._CAPTURE_KEY, self._CAPTURE_MACRO, self._CAPTURE_TOGGLE_HOLD):
            self._open_button_capture(
                button,
                value is self._CAPTURE_MACRO,
                toggle_hold=value is self._CAPTURE_TOGGLE_HOLD,
            )
            return
        try:
            if action_type == RatbagdButton.ActionType.BUTTON:
                button.mapping = value
            elif action_type == RatbagdButton.ActionType.SPECIAL:
                button.special = value
            elif action_type == RatbagdButton.ActionType.KEY:
                button.key = value
            elif action_type == RatbagdButton.ActionType.MACRO:
                button.macro = value
            elif action_type == RatbagdButton.ActionType.NONE:
                button.disable()
        except (GLib.Error, RatbagError, ValueError):
            self._add_toast(_("Could not change button assignment"))
            return
        if action_type != RatbagdButton.ActionType.MACRO:
            dropdown.set_subtitle("")
            dropdown.set_tooltip_text(None)
        self._set_apply_sensitive(True)

    def _open_button_capture(
        self, button, capture_macro: bool, toggle_hold: bool = False
    ) -> None:
        assert self._window is not None
        dialog = ButtonCaptureDialog(
            self._window,
            capture_macro,
            lambda action_type, value: self._on_button_capture_saved(
                button, action_type, value, toggle_hold
            ),
            self._refresh_selected_device,
            toggle_hold=toggle_hold,
        )
        dialog.present()

    def _on_button_capture_saved(
        self, button, action_type, value, toggle_hold: bool = False
    ) -> None:
        try:
            if toggle_hold and action_type == RatbagdButton.ActionType.KEY:
                button.macro = RatbagdMacro.toggle_hold(value)
            elif action_type == RatbagdButton.ActionType.KEY:
                button.key = value
            else:
                button.macro = value
        except (GLib.Error, RatbagError, ValueError):
            self._add_toast(_("Could not change button assignment"))
            self._refresh_selected_device()
            return
        self._set_apply_sensitive(True)
        self._refresh_selected_device()

    def _refresh_selected_device(self) -> None:
        if self._selected_device is not None:
            self._show_device(self._selected_device)

    def _on_led_mode_changed(
        self,
        row,
        _pspec,
        index: int,
        modes: list,
        color_row,
        brightness_row,
        duration_row,
    ) -> None:
        selected = row.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION:
            return
        mode = modes[selected]
        self._draft["leds"][index]["mode"] = mode
        self._update_led_controls(mode, color_row, brightness_row, duration_row)
        self._set_apply_sensitive(True)

    @staticmethod
    def _update_led_controls(mode, color_row, brightness_row, duration_row) -> None:
        color_row.set_sensitive(
            mode in (RatbagdLed.Mode.ON, RatbagdLed.Mode.BREATHING)
        )
        brightness_row.set_sensitive(mode != RatbagdLed.Mode.OFF)
        duration_row.set_sensitive(
            mode in (RatbagdLed.Mode.CYCLE, RatbagdLed.Mode.BREATHING)
        )

    def _on_led_color_changed(self, button, _pspec, index: int) -> None:
        color = button.get_rgba()
        self._draft["leds"][index]["color"] = (
            round(color.red * 255),
            round(color.green * 255),
            round(color.blue * 255),
        )
        self._set_apply_sensitive(True)

    def _on_led_brightness_changed(self, scale, index: int) -> None:
        self._draft["leds"][index]["brightness"] = round(scale.get_value())
        self._set_apply_sensitive(True)

    def _on_led_duration_changed(self, scale, index: int) -> None:
        self._draft["leds"][index]["effect_duration"] = round(scale.get_value())
        self._set_apply_sensitive(True)

    def _on_active_dpi_clicked(self, _button, index: int) -> None:
        self._draft["active_resolution"] = index
        self._draft["resolution_disabled"][index] = False
        self._set_apply_sensitive(True)
        assert self._selected_device is not None
        self._show_device(self._selected_device)

    @staticmethod
    def _apply_resolution_draft(profile, draft) -> None:
        for value, resolution in zip(draft["resolutions"], profile.resolutions):
            current = resolution.resolution
            updated = (value,) if len(current) == 1 else (value, value)
            if updated != current:
                resolution.resolution = updated

        if not profile.resolutions:
            return
        active_index = draft["active_resolution"]
        active = profile.resolutions[active_index]
        if active.is_disabled:
            active.set_disabled(False)
        if not active.is_active:
            active.set_active()

        for index, (disabled, resolution) in enumerate(
            zip(draft["resolution_disabled"], profile.resolutions)
        ):
            # Active and default stages must remain available. In particular,
            # Logitech HID++ devices reject commits with a disabled default.
            desired = disabled and index != active_index and not resolution.is_default
            if desired != resolution.is_disabled:
                resolution.set_disabled(desired)

    @staticmethod
    def _apply_led_draft(profile, draft) -> None:
        for values, led in zip(draft["leds"], profile.leds):
            mode = values["mode"]
            if led.mode != mode:
                led.mode = mode
            if mode == RatbagdLed.Mode.OFF:
                continue
            if led.brightness != values["brightness"]:
                led.brightness = values["brightness"]
            if mode in (RatbagdLed.Mode.ON, RatbagdLed.Mode.BREATHING):
                if tuple(led.color) != values["color"]:
                    led.color = values["color"]
            if mode in (RatbagdLed.Mode.CYCLE, RatbagdLed.Mode.BREATHING):
                if led.effect_duration != values["effect_duration"]:
                    led.effect_duration = values["effect_duration"]

    def _on_report_rate_changed(self, row, _pspec) -> None:
        assert self._selected_profile is not None
        self._draft["report_rate"] = self._selected_profile.report_rates[
            row.get_selected()
        ]
        self._set_apply_sensitive(True)

    def _on_debounce_changed(self, row, _pspec) -> None:
        assert self._selected_profile is not None
        self._draft["debounce"] = self._selected_profile.debounces[row.get_selected()]
        self._set_apply_sensitive(True)

    def _on_angle_snapping_changed(self, row, _pspec) -> None:
        self._draft["angle_snapping"] = 1 if row.get_active() else 0
        self._set_apply_sensitive(True)

    def _show_device(self, device: RatbagdDevice) -> None:
        self._refresh_profile_menu(device)
        self._content_page.set_child(self._device_page(device))
        self._content_page.set_title(device.name)

    def _on_apply_clicked(self, _button) -> None:
        assert self._selected_device is not None
        assert self._selected_profile is not None
        profile = self._selected_profile
        try:
            name = self._draft["name"].strip()
            if not name:
                self._add_toast(_("Profile name cannot be empty"))
                return
            self._profile_names.set(self._selected_device, profile, name)
            if (
                RatbagdProfile.CAP_WRITABLE_NAME in profile.capabilities
                and name != profile.name
            ):
                profile.name = name
            self._apply_resolution_draft(profile, self._draft)
            if profile.report_rates:
                profile.report_rate = self._draft["report_rate"]
            if profile.debounces:
                profile.debounce = self._draft["debounce"]
            if profile.angle_snapping != -1:
                profile.angle_snapping = self._draft["angle_snapping"]
            self._apply_led_draft(profile, self._draft)
            self._selected_device.commit()
        except (GLib.Error, OSError, RatbagError, ValueError):
            self._add_toast(_("Could not apply changes"))
            return

        self._set_apply_sensitive(False)
        self._add_toast(_("Changes applied"))
        self._show_device(self._selected_device)

    def _set_apply_sensitive(self, sensitive: bool) -> None:
        if self._apply_button is not None:
            self._apply_button.set_sensitive(sensitive)

    def _add_toast(self, title: str) -> None:
        if self._toast_overlay is not None:
            self._toast_overlay.add_toast(Adw.Toast.new(title))

    def _on_daemon_disappeared(self, *_args) -> None:
        self._show_error(
            _("ratbagd disconnected"),
            _("Restart Piper after ratbagd is available again."),
        )

    def _show_error(self, title: str, description: str) -> None:
        if self._profile_button is not None:
            self._profile_button.set_visible(False)
        self._content_page.set_child(self._status_page(title, description))
        self._content_page.set_title(_("Piper Next"))

    @staticmethod
    def _status_page(title: str, description: str) -> Adw.StatusPage:
        page = Adw.StatusPage()
        page.set_icon_name("org.freedesktop.Piper-symbolic")
        page.set_title(title)
        page.set_description(description)
        return page
