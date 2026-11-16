#!/bin/sh
#
# Build libratbag with the patches from tools/libratbag-patches and install
# the resulting ratbagd as /usr/local/libexec/ratbagd-piper. The system
# ratbagd.service is pointed at that binary (and at the patched device
# database) through a systemd drop-in. Files owned by the distribution
# package under /usr are left untouched, so the package manager can still
# verify and update them.
#
# Usage: tools/install-ratbagd-piper.sh [path-to-libratbag-checkout]
#        LIBRATBAG_DIR=... tools/install-ratbagd-piper.sh
#
# Only the installation steps run through sudo; the build runs as your user.

set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
libratbag_dir=${1:-${LIBRATBAG_DIR:-$HOME/Applications/libratbag}}
patch_dir="$repo_dir/tools/libratbag-patches"

install_libexec=/usr/local/libexec/ratbagd-piper
install_data_dir=/usr/local/share/libratbag
dropin_dir=/etc/systemd/system/ratbagd.service.d
dropin="$dropin_dir/piper-override.conf"

if [ ! -d "$libratbag_dir/.git" ]; then
    cat >&2 <<MSG
No libratbag checkout found at: $libratbag_dir

Clone upstream libratbag first, for example:
    git clone https://github.com/libratbag/libratbag.git "$libratbag_dir"
or pass the path of an existing checkout as the first argument.
MSG
    exit 1
fi

for tool in meson ninja git; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo >&2 "$tool is required but not installed."
        exit 1
    }
done

echo "Using libratbag checkout: $libratbag_dir"

# Apply the patches this fork needs. A patch that already is part of the
# checkout (applied earlier or merged upstream) is skipped.
for patch in "$patch_dir"/*.patch; do
    [ -e "$patch" ] || continue
    if git -C "$libratbag_dir" apply --check --reverse "$patch" >/dev/null 2>&1; then
        echo "already applied: $(basename "$patch")"
        continue
    fi
    if ! git -C "$libratbag_dir" apply --check "$patch" >/dev/null 2>&1; then
        # Later patches can change the same lines, preventing a reverse
        # apply even though this patch is already in the checkout's history.
        patch_id=$(git patch-id --stable < "$patch" | awk 'NR == 1 { print $1 }')
        if [ -n "$patch_id" ] && git -C "$libratbag_dir" log -p --pretty=medium |
            git patch-id --stable |
            awk -v wanted="$patch_id" '$1 == wanted { found = 1 } END { exit !found }'; then
            echo "already applied in history: $(basename "$patch")"
            continue
        fi
        echo >&2 "Patch does not apply to this libratbag checkout: $(basename "$patch")"
        echo >&2 "Rebase the patches in $patch_dir or use a matching checkout."
        exit 1
    fi
    echo "applying: $(basename "$patch")"
    if ! git -C "$libratbag_dir" am --quiet "$patch" 2>/dev/null; then
        git -C "$libratbag_dir" am --abort >/dev/null 2>&1 || true
        git -C "$libratbag_dir" apply "$patch"
    fi
done

build_dir="$libratbag_dir/builddir"
if [ ! -f "$build_dir/build.ninja" ]; then
    meson setup "$build_dir" "$libratbag_dir" -Dtests=false -Ddocumentation=false
fi
ninja -C "$build_dir"

ratbagd_bin="$build_dir/ratbagd"
[ -x "$ratbagd_bin" ] || { echo >&2 "ratbagd was not built at $ratbagd_bin"; exit 1; }

tmp_dropin=$(mktemp)
trap 'rm -f "$tmp_dropin"' EXIT
cat > "$tmp_dropin" <<CONF
# Installed by piper/tools/install-ratbagd-piper.sh
[Service]
ExecStart=
ExecStart=$install_libexec
Environment=LIBRATBAG_DATA_DIR=$install_data_dir
CONF

echo
echo "Installing (sudo will ask for your password):"
echo "  $ratbagd_bin -> $install_libexec"
echo "  $libratbag_dir/data/devices/*.device -> $install_data_dir/"
echo "  systemd drop-in -> $dropin"

sudo install -D -m 0755 "$ratbagd_bin" "$install_libexec"
sudo install -d -m 0755 "$install_data_dir"
sudo install -m 0644 "$libratbag_dir"/data/devices/*.device "$install_data_dir"/
sudo install -D -m 0644 "$tmp_dropin" "$dropin"

# An earlier manual workaround edited the packaged device file in place and
# kept a backup next to it. Restore the packaged file now that the patched
# database lives under /usr/local.
for backup in /usr/share/libratbag/*.device.piper-backup; do
    [ -e "$backup" ] || continue
    original=${backup%.piper-backup}
    echo "restoring packaged file: $original"
    sudo mv -f "$backup" "$original"
done

sudo systemctl daemon-reload
sudo systemctl restart ratbagd.service

echo
echo "ratbagd-piper is installed and running. Devices seen by ratbagd:"
sleep 2
ratbagctl list || true
