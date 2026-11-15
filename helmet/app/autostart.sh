#!/bin/sh
# Installed as /etc/init.d/S99zhelmet-app; only this App's normal lifecycle is used.
self=$(readlink -f "$0") || exit 1
app=$(dirname "$self") || exit 1
root=$(dirname "$(dirname "$app")") || exit 1
case "$1" in
    start|stop|status)
        exec /usr/bin/python3 "$app/boot.py" "$1" --root "$root"
        ;;
    restart|reload)
        /usr/bin/python3 "$app/boot.py" stop --root "$root" || exit 1
        exec /usr/bin/python3 "$app/boot.py" start --root "$root"
        ;;
    *)
        echo "Usage: $0 {start|stop|status|restart}"
        exit 1
        ;;
esac
