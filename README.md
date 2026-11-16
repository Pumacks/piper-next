# Piper Next

> A modern GTK4/libadwaita interface for configuring gaming mice through
> [libratbag](https://github.com/libratbag/libratbag) and `ratbagd`.

Piper Next is a community fork of
[Piper](https://github.com/libratbag/piper). It keeps Piper's reliable device
backend while redesigning the configuration experience around one focused,
responsive workspace.

The mouse stays visible, its controls are easier to find, profiles are easier
to manage, and common changes no longer require jumping between separate
pages.

> [!IMPORTANT]
> This branch is actively migrating Piper from GTK3 to GTK4. The modern UI can
> be launched from the build tree with `piper-better-ui.devel` or installed for
> the current user with `install-user.sh`; the regular `piper` launcher remains
> available as a compatibility interface.

## Preview

| Main configuration workspace | Profile and virtual-profile menu |
| --- | --- |
| _Screenshot placeholder — add `docs/screenshots/main-workspace.png`_ | _Screenshot placeholder — add `docs/screenshots/profile-menu.png`_ |

| Button highlighting and macros | Lighting and advanced settings |
| --- | --- |
| _Screenshot placeholder — add `docs/screenshots/button-highlighting.png`_ | _Screenshot placeholder — add `docs/screenshots/lighting.png`_ |

When screenshots are available, replace the placeholder text with:

```md
![Piper Next main workspace](docs/screenshots/main-workspace.png)
```

## What this fork adds

Compared with the classic upstream Piper interface this fork provides:

| Area | Classic Piper | Piper Next |
| --- | --- | --- |
| Interface | GTK3 with separate configuration pages | GTK4 and libadwaita with one unified workspace |
| Mouse overview | Device view tied to individual pages | Persistent, larger mouse illustration beside all settings |
| Button discovery | Physical button mapping in the classic button page | Hover or keyboard-focus a setting to highlight that button on the mouse |
| Button assignment | Dialog-oriented assignment flow | Compact dropdowns plus dedicated keyboard-key and macro capture |
| Macros | Key-sequence capture | Press/release recording, timing events, and a readable multi-line assignment preview |
| Profiles | Onboard profile management | Header-bar switching, inline renaming, and persistent local names |
| Virtual profiles | Not part of the classic workflow | Searchable, model-specific local snapshots with create, load, and confirmed delete actions |
| DPI and advanced settings | Separate pages | DPI stages, polling rate, debounce, and angle snapping in the same workspace |
| Lighting | Separate LED dialog | Inline effect, color, brightness, and speed controls |
| Device artwork | Upstream device artwork | Additional artwork and mapping for devices developed in this fork, including the Logitech G502 X Plus |

The available controls still depend on what the mouse and its libratbag driver
support. Piper cannot expose a feature that `ratbagd` does not report.

## Using Piper Next

### 1. Choose a profile

Open the profile menu in the top-left of the window and select an onboard
profile. Selecting it activates that profile.

Each profile row also provides two actions:

- **Edit** changes the profile name inline. Press **Enter** to save it or
  **Escape** to cancel.
- **Left/right arrows** open the virtual-profile chooser for that exact
  onboard slot.

Profile names are stored locally, so useful names remain available even when a
mouse cannot store names in firmware. On supported devices the name is also
written to the hardware when changes are applied.

### 2. Configure buttons

Button assignments appear first in the settings column. Hover over a row—or
focus it with the keyboard—to highlight the corresponding physical button on
the mouse illustration.

Use a button's dropdown to assign:

- another mouse button;
- a special action such as DPI or profile switching;
- a keyboard key;
- a recorded macro; or
- no action.

For macros, choose **Record a macro**, enter the sequence, then select
**Use assignment**. The recorded sequence is shown below the button row.

### 3. Configure DPI, lighting, and advanced behavior

The remaining groups are arranged below Button Assignments:

- **DPI stages** — choose supported values and select the active stage;
- **Lighting** — configure effects, color, brightness, and speed;
- **Advanced** — adjust polling rate, debounce time, and angle snapping when
  supported.

### 4. Work with virtual profiles

Virtual profiles are local, model-specific snapshots. They do not consume an
additional onboard profile slot.

Open the left/right-arrows menu on an onboard profile row to:

- search saved virtual profiles;
- create one with the **+** button;
- load one into that onboard slot; or
- delete one after a confirmation prompt.

Loading a virtual profile replaces the selected slot's settings and name in
the editor. The mouse is not written until **Apply** is pressed.

### 5. Apply changes

Most edits are staged first. Press **Apply** in the window header to write
pending changes to the device.

Profile activation happens immediately. Local profile names and virtual-profile
library changes are also saved immediately.

## Requirements

Piper is a frontend. A working `ratbagd` service and a device supported by
libratbag are required.

Build and runtime dependencies include:

- Python 3;
- Meson and Ninja;
- PyGObject;
- GTK 4.10 or newer;
- libadwaita 1.4 or newer;
- librsvg and Cairo Python bindings;
- `lxml` and `evdev`; and
- libratbag/`ratbagd` with D-Bus API version 2.

Package names vary by distribution. Check your distribution's Piper or
libratbag build instructions when a dependency cannot be found.

Supported devices are determined by libratbag. See the
[libratbag device database](https://github.com/libratbag/libratbag/tree/master/data/devices)
for the upstream list.

## Logitech G502 X LIGHTSPEED and other mice that need a patched ratbagd

Some newer Logitech mice are not handled by the libratbag that distributions
ship. The G502 X LIGHTSPEED (wireless `usb:046d:409f`, wired `usb:046d:c098`)
is one of them: it reports onboard-profile format `0x0a` and uses the HID++
features 0x2202 (Extended Adjustable DPI) and 0x8061 (Extended Adjustable
Report Rate), so stock `ratbagd` logs `Profile layout not supported: 0x0a` and
never lists the mouse.

This repository carries the required libratbag changes as patches in
`tools/libratbag-patches/`. The helper below builds libratbag with them and
installs only a second daemon binary, `/usr/local/libexec/ratbagd-piper`, plus
the patched device database under `/usr/local/share/libratbag`. A systemd
drop-in points the existing `ratbagd.service` at that binary; files owned by
your distribution package are not modified.

```sh
git clone https://github.com/libratbag/libratbag.git ~/Applications/libratbag
tools/install-ratbagd-piper.sh            # or: tools/install-ratbagd-piper.sh /path/to/libratbag
ratbagctl list
```

The build runs as your user; only the final copy and the service restart use
`sudo`. Re-run the script after updating the libratbag checkout or the
patches. To go back to the packaged daemon, remove
`/etc/systemd/system/ratbagd.service.d/piper-override.conf` and run
`sudo systemctl daemon-reload && sudo systemctl restart ratbagd`.

The patches also make `ratbagd` rescan for wireless mice that wake up after
the daemon started, which is why a sleeping G502 can take up to 30 seconds to
appear.

They also teach libratbag to store real multi-step macros in the mouse's
onboard macro memory, including the device-side flow control commands
(*wait for release*, *repeat while held*, *repeat until pressed again*). Piper
Next uses this for **Hold a key until pressed again…** in a button's
assignment list: one press holds a keyboard key or a mouse button (click the
box in the dialog with it), the next press releases it. Recorded macros may
contain mouse clicks as well. Stock libratbag only writes macros that boil
down to a single key with modifiers.

They also add a `FactoryReset` D-Bus method. Piper Next shows it as
**Onboard memory → Factory reset** on the device page: the mouse's onboard
profiles are overwritten with the factory (ROM) defaults, unapplied changes
are dropped and local profile names for that mouse are forgotten. Virtual
profiles in your library are untouched. With a stock `ratbagd` the button
reports that the daemon lacks the method.

## Build and run the modern GTK4 interface

Clone this fork:

```sh
git clone https://github.com/Pumacks/piper.git
cd piper
```

Configure and build it:

```sh
meson setup builddir --prefix=/usr
meson compile -C builddir
```

Launch the modern interface directly from the source tree:

```sh
./builddir/piper-better-ui.devel
```

`ratbagd` must be available on the system D-Bus. On most distributions it is
D-Bus/systemd activated automatically after libratbag is installed.

After pulling new changes, rebuild with:

```sh
meson compile -C builddir
```

If Meson asks for reconfiguration:

```sh
meson setup --reconfigure builddir --prefix=/usr
```

## Install Piper Next for the current user

The included helper builds and installs the modern GTK4 interface under
`~/.local` without requiring `sudo`:

```sh
./install-user.sh
```

This installs the application launcher, icon, resources, translations, Python
modules, and these two executables:

- `~/.local/bin/piper-better-ui` — the modern GTK4 interface used by the
  application-menu entry;
- `~/.local/bin/piper` — the GTK3 compatibility interface.

Close any running Piper window, then open **Piper** from the application menu.
The modern interface can also be started directly:

```sh
~/.local/bin/piper-better-ui
```

After pulling changes from the repository, run `./install-user.sh` again to
rebuild and update the installed application.

If Piper does not appear in the application menu immediately, log out and back
in so the desktop session reloads user-local application entries.

## Switch profiles from Steam

The user-local installation includes `~/.local/bin/piper-profile`, a small
command-line profile switcher designed for game launchers. List the available
onboard and virtual profile names with:

```sh
~/.local/bin/piper-profile --list
```

The easiest way to get the launch option is Piper Next: open the virtual
profile menu of a slot and press the copy icon next to a virtual profile. The
clipboard then holds a ready-made line such as

```sh
/home/you/.local/bin/piper-profile --device 'usb:046d:409f:0' --profile 'DayZ' --virtual --restore 'Desktop' -- %command%
```

Paste it into the game's **Properties → Launch Options** field in Steam. The
pieces:

- `--profile NAME` loads the profile when the game starts. Onboard profiles
  can also be selected by their one-based slot number. When an onboard and a
  virtual profile share a name the onboard one wins; `--virtual` picks the
  virtual one. Virtual profiles replace the active onboard slot; `--slot N`
  chooses another slot.
- `--restore NAME` switches back to that profile when the game exits. The copy
  button fills it in automatically when a virtual profile is called
  "Desktop" (or "Default").
- `--device TEXT` is required when more than one mouse is connected (for
  example a G502 and a G502 X); any substring of the name, model or ID works.
  The copy button inserts the model of the mouse you are editing.
- `--wait SECONDS` (default 45) keeps retrying while ratbagd starts or a
  wireless mouse is still waking up. The game is started immediately and the
  profile is switched alongside it, so a sleeping mouse never delays the
  launch. Add `--strict` to launch the game only after a successful switch.

Profile errors are printed to Steam's launch log but do not prevent the game
from starting.

## Development

Run all repository tests:

```sh
meson test -C builddir --print-errorlogs
```

Run the focused format and lint checks:

```sh
ninja -C builddir python-black-check
ninja -C builddir python-ruff-check
```

Useful project locations:

- `piper/better_ui.py` — GTK4/libadwaita application;
- `piper/ratbagd.py` — D-Bus device model;
- `piper/virtualprofiles.py` — virtual-profile storage and validation;
- `piper/profilenames.py` — persistent local profile names;
- `data/svgs/` — model-specific device illustrations and button regions; and
- `tests/` — device-independent regression and repository checks.

## Troubleshooting

### No supported mouse found

- Confirm that the device is supported by libratbag.
- Make sure `ratbagd` is installed and available on the system bus.
- Avoid running another Piper instance that may already be using the device.

Useful checks include:

```sh
ratbagd --version
ratbagctl list
```

### The mouse illustration is missing

The device can still be configured, but hover highlighting requires a matching
SVG. Device images live in `data/svgs/` and are selected through
`data/svgs/svg-lookup.ini`.

### A setting is unavailable

Capabilities come from the mouse's libratbag driver. Firmware revisions and
connection modes can expose different functionality.

### Changes disappeared

Remember to press **Apply**. Loading a virtual profile only stages its snapshot
in the selected onboard slot.

## Upstream and contributing

This project builds on the work of the
[Piper](https://github.com/libratbag/piper) and
[libratbag](https://github.com/libratbag/libratbag) contributors.

When reporting a problem, include:

- the mouse model and connection type;
- the output of `ratbagd --version`;
- the Piper terminal output;
- steps to reproduce the problem; and
- a screenshot for visual or layout issues.

Changes should follow the repository style and include regression tests where
possible.

## License

Piper is licensed under the GNU General Public License v2.0 or later. See
[COPYING](COPYING) for the complete license text.
