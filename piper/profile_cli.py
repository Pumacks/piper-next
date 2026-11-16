# SPDX-License-Identifier: GPL-2.0-or-later

"""Command-line profile switching for launchers such as Steam."""

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from gi.repository import GLib

from .profilenames import ProfileNameStore
from .ratbagd import (
    Ratbagd,
    RatbagdDBusTimeoutError,
    RatbagdIncompatibleError,
    RatbagdProfile,
    RatbagdUnavailableError,
    RatbagError,
)
from .virtualprofiles import (
    VirtualProfileError,
    VirtualProfileStore,
    apply_virtual_profile,
)


DEFAULT_WAIT_SECONDS = 45.0
RETRY_INTERVAL_SECONDS = 2.0
_UNSET = object()


class ProfileCliError(Exception):
    """A user-facing profile selection or activation error."""


def _profile_name(device, profile, names: ProfileNameStore) -> str:
    return (
        names.get(device, profile)
        or profile.name
        or f"Profile {profile.index + 1}"
    )


def select_device(devices, selector: Optional[str] = None):
    if not devices:
        raise ProfileCliError("No supported mouse was found")
    if selector:
        needle = selector.casefold()
        matches = [
            device
            for device in devices
            if any(
                needle in str(value).casefold()
                for value in (device.name, device.model, device.id)
            )
        ]
        if not matches:
            raise ProfileCliError(f"No device matches {selector!r}")
        if len(matches) > 1:
            raise ProfileCliError(f"More than one device matches {selector!r}")
        return matches[0]
    if len(devices) > 1:
        choices = ", ".join(f"{device.name} ({device.model})" for device in devices)
        raise ProfileCliError(
            f"More than one mouse is available: {choices}. Use --device to choose one"
        )
    return devices[0]


def _matching_onboard_profiles(device, selector: str, names: ProfileNameStore):
    if selector.isdecimal():
        index = int(selector) - 1
        return [profile for profile in device.profiles if profile.index == index]
    needle = selector.casefold()
    return [
        profile
        for profile in device.profiles
        if _profile_name(device, profile, names).casefold() == needle
    ]


def _matching_virtual_profiles(device, selector: str, store: VirtualProfileStore):
    needle = selector.casefold()
    return [
        profile
        for profile in store.list_for_model(device.model)
        if isinstance(profile.get("name"), str)
        and profile["name"].casefold() == needle
    ]


def activate_profile(
    device,
    selector: str,
    names: ProfileNameStore,
    virtual_profiles: VirtualProfileStore,
    *,
    virtual_only: bool = False,
    slot: Optional[int] = None,
) -> str:
    onboard = [] if virtual_only else _matching_onboard_profiles(
        device, selector, names
    )
    virtual = _matching_virtual_profiles(device, selector, virtual_profiles)
    if len(onboard) > 1 or len(virtual) > 1:
        raise ProfileCliError(
            f"Profile {selector!r} is ambiguous; rename it or use --virtual"
        )

    if onboard:
        profile = onboard[0]
        changed = False
        if profile.disabled:
            if RatbagdProfile.CAP_DISABLE not in profile.capabilities:
                raise ProfileCliError(f"Profile {selector!r} cannot be enabled")
            profile.disabled = False
            changed = True
        if not profile.is_active:
            profile.set_active()
            changed = True
        if changed or profile.dirty:
            device.commit()
        return f"Activated onboard profile {_profile_name(device, profile, names)!r}"

    if not virtual:
        raise ProfileCliError(f"Profile {selector!r} was not found")
    if slot is None:
        target = device.active_profile or device.profiles[0]
    else:
        if not 1 <= slot <= len(device.profiles):
            raise ProfileCliError(
                f"Slot must be between 1 and {len(device.profiles)}"
            )
        target = device.profiles[slot - 1]
    virtual_profile = virtual[0]
    try:
        apply_virtual_profile(virtual_profile, device.model, target)
        names.set(device, target, virtual_profile["name"])
        if not target.is_active:
            target.set_active()
        device.commit()
    except (
        GLib.Error,
        KeyError,
        OSError,
        RatbagdDBusTimeoutError,
        RatbagError,
        ValueError,
        VirtualProfileError,
    ) as error:
        raise ProfileCliError(
            f"Could not load virtual profile {selector!r}: {error}"
        ) from error
    return (
        f"Loaded virtual profile {virtual_profile['name']!r} into slot "
        f"{target.index + 1}"
    )


def list_profiles(device, names, virtual_profiles) -> str:
    lines = [f"Device: {device.name} ({device.model})", "Onboard profiles:"]
    for profile in device.profiles:
        marker = "*" if profile.is_active else " "
        lines.append(
            f"  {marker} {profile.index + 1}: {_profile_name(device, profile, names)}"
        )
    lines.append("Virtual profiles:")
    virtual = virtual_profiles.list_for_model(device.model)
    if virtual:
        lines.extend(f"    {profile['name']}" for profile in virtual)
    else:
        lines.append("    (none)")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="piper-profile",
        description="Switch a Piper profile, optionally before launching a command.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--profile", help="Onboard slot number or profile name")
    action.add_argument("--list", action="store_true", help="List available profiles")
    parser.add_argument(
        "--virtual", action="store_true", help="Only match a virtual profile"
    )
    parser.add_argument("--slot", type=int, help="Onboard slot for a virtual profile")
    parser.add_argument("--device", help="Substring of the device name, model, or ID")
    parser.add_argument(
        "--restore",
        metavar="PROFILE",
        help="Profile to switch back to after the command exits",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_WAIT_SECONDS,
        metavar="SECONDS",
        help=(
            "Keep retrying for this long when ratbagd or the mouse is not "
            "ready yet, for example because a wireless mouse is still asleep "
            f"(default: {DEFAULT_WAIT_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Do not launch the command when profile switching fails",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


# Errors that may clear up by themselves: ratbagd is still starting, the
# wireless mouse has not answered yet, or a D-Bus call timed out.
_TRANSIENT_ERRORS = (
    ProfileCliError,
    RatbagdDBusTimeoutError,
    RatbagdUnavailableError,
    RatbagError,
    GLib.Error,
)
_FATAL_ERRORS = (RatbagdIncompatibleError, VirtualProfileError)


def _open_device(ratbagd_api_version: int, selector: Optional[str]):
    ratbag = Ratbagd(ratbagd_api_version)
    return select_device(ratbag.devices, selector)


def switch_with_retry(
    selector: str,
    args,
    *,
    ratbagd_api_version: int,
    config_dir: Path,
    deadline: float,
    should_stop=lambda: False,
    monotonic=time.monotonic,
    sleep=time.sleep,
    open_device=None,
    virtual_only=None,
    slot=_UNSET,
) -> str:
    """Activate ``selector`` and retry transient failures until ``deadline``.

    ``should_stop`` is polled between attempts so the loop ends early when
    the launched game has already exited. Returns the activation message or
    raises the last error.
    """
    open_device = open_device or (
        lambda: _open_device(ratbagd_api_version, args.device)
    )
    attempt = 0
    while True:
        attempt += 1
        try:
            device = open_device()
            names = ProfileNameStore()
            virtual_profiles = VirtualProfileStore(
                config_dir / "virtual_profiles.json"
            )
            virtual_profiles.migrate_g502_profiles()
            return activate_profile(
                device,
                selector,
                names,
                virtual_profiles,
                virtual_only=args.virtual if virtual_only is None else virtual_only,
                slot=args.slot if slot is _UNSET else slot,
            )
        except _FATAL_ERRORS:
            raise
        except _TRANSIENT_ERRORS as error:
            remaining = deadline - monotonic()
            if remaining <= 0 or should_stop():
                raise
            if attempt == 1:
                print(
                    f"piper-profile: {error}; retrying for up to "
                    f"{remaining:.0f}s",
                    file=sys.stderr,
                )
            sleep(min(RETRY_INTERVAL_SECONDS, max(remaining, 0.1)))


def _run_command(command) -> int:
    """Run the game and return its exit code, forwarding termination signals."""
    try:
        child = subprocess.Popen(command)
    except OSError as error:
        print(
            f"piper-profile: could not launch {command[0]!r}: {error}",
            file=sys.stderr,
        )
        return 127
    return _wait_for_child(child)


def _switch(
    selector,
    args,
    ratbagd_api_version,
    config_dir,
    deadline,
    should_stop,
    *,
    restoring=False,
):
    try:
        message = switch_with_retry(
            selector,
            args,
            ratbagd_api_version=ratbagd_api_version,
            config_dir=config_dir,
            deadline=deadline,
            should_stop=should_stop,
            # The desktop profile may be onboard or virtual; let the normal
            # name matching decide instead of the game's --virtual/--slot.
            virtual_only=False if restoring else None,
            slot=None if restoring else _UNSET,
        )
    except (_FATAL_ERRORS, _TRANSIENT_ERRORS) as error:
        print(f"piper-profile: {error}", file=sys.stderr)
        return False
    print(message, file=sys.stderr)
    return True


def main(ratbagd_api_version: int, argv=None) -> int:
    args = _parser().parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    config_dir = Path(GLib.get_user_config_dir()) / "piper"

    if args.list:
        try:
            device = _open_device(ratbagd_api_version, args.device)
            names = ProfileNameStore()
            virtual_profiles = VirtualProfileStore(
                config_dir / "virtual_profiles.json"
            )
            virtual_profiles.migrate_g502_profiles()
            print(list_profiles(device, names, virtual_profiles))
        except (_FATAL_ERRORS, _TRANSIENT_ERRORS) as error:
            print(f"piper-profile: {error}", file=sys.stderr)
            return 1
        return 0

    if not command:
        deadline = time.monotonic() + args.wait
        return 0 if _switch(args.profile, args, ratbagd_api_version, config_dir, deadline, lambda: False) else 1

    if args.strict:
        # The game may only start once the profile is active.
        deadline = time.monotonic() + args.wait
        if not _switch(args.profile, args, ratbagd_api_version, config_dir, deadline, lambda: False):
            return 1
        exit_code = _run_command(command)
    else:
        # Start the game right away and switch the profile alongside it, so a
        # mouse that is still waking up does not delay or block the launch.
        try:
            child = subprocess.Popen(command)
        except OSError as error:
            print(
                f"piper-profile: could not launch {command[0]!r}: {error}",
                file=sys.stderr,
            )
            return 127
        deadline = time.monotonic() + args.wait
        _switch(
            args.profile,
            args,
            ratbagd_api_version,
            config_dir,
            deadline,
            lambda: child.poll() is not None,
        )
        exit_code = _wait_for_child(child)

    if args.restore:
        deadline = time.monotonic() + args.wait
        _switch(
            args.restore,
            args,
            ratbagd_api_version,
            config_dir,
            deadline,
            lambda: False,
            restoring=True,
        )
    return exit_code


def _wait_for_child(child) -> int:
    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except OSError:
            pass

    previous = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        return child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
