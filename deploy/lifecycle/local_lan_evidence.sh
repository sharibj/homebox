#!/bin/sh
# Production always reads /sys. One explicit synthetic root is reserved for tests.
set -eu
case $# in
  0) root=/sys ;;
  1) root=$1 ;;
  *) exit 2 ;;
esac
case "$root" in /*) ;; *) exit 2 ;; esac
root=$(readlink -f -- "$root") || exit 2
[ "$root" != / ] && [ -d "$root/devices" ] && [ -d "$root/bus" ] \
  && [ -d "$root/class/net" ] || exit 2

for net in "$root"/class/net/*; do
  [ -d "$net" ] || continue
  name=${net##*/}
  case "$name" in ''|[!a-zA-Z0-9_]*|*[!a-zA-Z0-9_.:-]*) continue ;; esac
  index=$(cat -- "$net/ifindex" 2>/dev/null) || continue
  case "$index" in ''|0*|*[!0-9]*) continue ;; esac
  interface=$(readlink -f -- "$net" 2>/dev/null) || continue
  device=$(readlink -f -- "$net/device" 2>/dev/null) || continue
  driver=$(readlink -f -- "$net/device/driver" 2>/dev/null) || continue
  bus=$(readlink -f -- "$net/device/subsystem" 2>/dev/null) || continue
  [ -d "$interface" ] && [ -d "$device" ] && [ -d "$driver" ] && [ -d "$bus" ] || continue
  case "$interface" in "$root"/devices/*/net/"$name") ;; *) continue ;; esac
  case "$device" in "$root"/devices/*) ;; *) continue ;; esac
  case "$interface" in "$device"/*) ;; *) continue ;; esac
  case "$bus" in "$root"/bus/*) ;; *) continue ;; esac
  bus_name=${bus#"$root"/bus/}
  case "$bus_name" in ''|*/*) continue ;; esac
  case "$driver" in "$bus"/drivers/*) ;; *) continue ;; esac
  driver_name=${driver#"$bus"/drivers/}
  case "$driver_name" in ''|*/*) continue ;; esac

  interface=/sys/${interface#"$root"/}
  device=/sys/${device#"$root"/}
  driver=/sys/${driver#"$root"/}
  bus=/sys/${bus#"$root"/}
  case "$interface/$device/$driver/$bus" in
    *virtual*|*hwsim*|*netdevsim*|*virtio*|*vmbus*|*xen*|*dummy_hcd*) continue ;;
  esac
  # A record must stay on one line, with tabs used only as field separators.
  case "$interface$device$driver$bus" in
    *"	"*|*"
"*) continue ;;
  esac
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$index" "$interface" "$device" "$driver" "$bus"
done
