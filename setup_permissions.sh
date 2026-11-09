#!/bin/sh
# LiIon4cycler: one-time setup of a new computer so that the scripts run without sudo.
#
# - udev rule: the CP2112 (USBI2C01, USB ID 10c4:ea90) devices /dev/hidrawN and /dev/i2c-N
#   get group plugdev and mode 0660
# - i2c-dev kernel module loaded at boot (provides /dev/i2c-N), if it is not built into the kernel
# - the user is added to the group plugdev
#
# Usage:  sudo ./setup_permissions.sh [USER]     (default: the user who ran sudo)
# Afterwards log out and in again (new group), then reconnect the USB cable of the cycler.
set -eu

GROUP=plugdev
RULES=/etc/udev/rules.d/99-liion4cycler.rules
MODCONF=/etc/modules-load.d/liion4cycler.conf

if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi
USER_NAME=${1:-${SUDO_USER:-}}
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = root ]; then
    echo "Give the user name: sudo $0 USER" >&2
    exit 1
fi
id "$USER_NAME" >/dev/null

# group
if ! getent group "$GROUP" >/dev/null; then
    groupadd "$GROUP"
    echo "created group $GROUP"
fi
if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$GROUP"; then
    echo "user $USER_NAME is already in group $GROUP"
    RELOGIN=no
else
    usermod -aG "$GROUP" "$USER_NAME"
    echo "added user $USER_NAME to group $GROUP"
    RELOGIN=yes
fi

# udev rule
cat > "$RULES" <<EOF
# LiIon4cycler: CP2112 USB-I2C bridge (MLAB USBI2C01) accessible without root
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea90", GROUP="$GROUP", MODE="0660"
SUBSYSTEM=="i2c-dev", ATTR{name}=="CP2112*", GROUP="$GROUP", MODE="0660"
EOF
echo "wrote $RULES"

# i2c-dev module (/dev/i2c-N); nothing to do when it is built into the kernel
if grep -q '/i2c-dev\.ko' "/lib/modules/$(uname -r)/modules.builtin" 2>/dev/null; then
    echo "i2c-dev is built into the kernel"
else
    echo i2c-dev > "$MODCONF"
    modprobe i2c-dev
    echo "wrote $MODCONF, i2c-dev loaded"
fi

# hid_cp2112 driver (part of the standard kernel)
if ! modinfo hid_cp2112 >/dev/null 2>&1 && [ ! -d /sys/module/hid_cp2112 ]; then
    echo "WARNING: kernel driver hid_cp2112 not found - the CP2112 will not get an I2C bus" >&2
fi

# apply to already connected devices
udevadm control --reload-rules
udevadm trigger --subsystem-match=hidraw --subsystem-match=i2c-dev
udevadm settle

found=no
for d in /sys/bus/hid/devices/*; do
    grep -qi '^HID_ID=0003:000010C4:0000EA90$' "$d/uevent" 2>/dev/null || continue
    found=yes
    for n in "$d"/hidraw/hidraw* "$d"/i2c-*; do
        [ -e "$n" ] && ls -l "/dev/$(basename "$n")"
    done
done
[ "$found" = yes ] || echo "no CP2112 connected now - the rule applies when it is plugged in"

echo
if [ "$RELOGIN" = yes ]; then
    echo "Done. Log out and in again (group $GROUP), then run ./hwtest.py without sudo."
else
    echo "Done. Run ./hwtest.py without sudo."
fi
