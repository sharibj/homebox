#!/bin/sh
set -eu
set -f
umask 077

fail() {
    printf '%s\n' 'Dual Caddy configuration failed validation.' >&2
    exit 1
}

ipv4() {
    case "$1" in ''|*[!0-9.]*|.*|*.|*..*) return 1 ;; esac
    saved_ifs=$IFS
    IFS=.
    set -- $1
    IFS=$saved_ifs
    [ "$#" -eq 4 ] || return 1
    for octet do
        case "$octet" in 0|[1-9]|[1-9][0-9]|[1-9][0-9][0-9]) ;; *) return 1 ;; esac
        [ "$octet" -le 255 ] || return 1
    done
}

lan=${PROXY_LOCAL_LAN_IP:-}
tail=${PROXY_LAN_IP:-}
ipv4 "$lan" && ipv4 "$tail" && [ "$lan" != "$tail" ] || fail
case "$lan" in
    10.*|192.168.*) ;;
    172.*)
        second=${lan#*.}
        second=${second%%.*}
        [ "$second" -ge 16 ] && [ "$second" -le 31 ] || fail
        ;;
    *) fail ;;
esac
[ "$#" -le 1 ] || fail
input=${1:-/etc/caddy/Caddyfile}
runtime=$(mktemp -d /tmp/homebox-caddy-dual.XXXXXX)
# Keep Caddy's automatic config snapshot on tmpfs, not the persistent /config.
export XDG_CONFIG_HOME="$runtime"
caddy adapt --pretty --adapter caddyfile --config "$input" > "$runtime/adapted.json"
awk -v lan="$lan" '
function fail() {
    bad = 1
    print "Unexpected dual Caddy adapter output." > "/dev/stderr"
    exit 1
}
function server(first, last,    i, n, start, stop, port, policies, listen, count) {
    listen = 0
    policies = 0
    for (i = first + 1; i < last; i++) {
        if (line[i] == t5 "\"listen\": [") {
            if (++listen != 1) fail()
            count = 0
            for (n = i + 1; n < last && line[n] != t5 "],"; n++) {
                if (line[n] !~ /^\t\t\t\t\t\t"[^"\\]+",?$/) fail()
                count++
                if (line[n] == t6 "\":14443\"") port = 14443
                if (line[n] == t6 "\":18443\"") port = 18443
            }
            if (n == last || count == 0) fail()
            if (port && count != 1) fail()
        }
        if (line[i] == t5 "\"tls_connection_policies\": [") {
            policies++
            start = i
            for (stop = i + 1; stop < last && line[stop] != t5 "],"; stop++) {}
            if (stop == last) fail()
        }
    }
    if (listen != 1) fail()
    if (!port) return
    if (++found[port] != 1 || policies != 1 || stop != start + 4 ||
            line[start + 1] != t6 "{" ||
            line[start + 2] !~ /^\t\t\t\t\t\t\t"default_sni": "[A-Za-z0-9.:-]+"$/ ||
            line[start + 3] != t6 "}") fail()
    line[start + 2] = t7 "\"default_sni\": \"" lan "\""
    changes++
}
BEGIN {
    t4 = "\t\t\t\t"
    t5 = t4 "\t"
    t6 = t5 "\t"
    t7 = t6 "\t"
}
{
    bytes += length($0) + 1
    if (NR > 20000 || bytes > 1048576) fail()
    line[NR] = $0
}
END {
    if (bad) exit 1
    for (i = 1; i <= NR; i++) {
        if (line[i] == "\t\t\t\"servers\": {") {
            if (++groups != 1) fail()
            inside = 1
        } else if (inside && line[i] ~ /^\t\t\t\t"[^"\\]+": \{$/) {
            if (first) fail()
            first = i
        } else if (inside && line[i] ~ /^\t\t\t\t\},?$/) {
            if (!first) fail()
            server(first, i)
            first = 0
        } else if (inside && line[i] ~ /^\t\t\t\},?$/) {
            if (first) fail()
            inside = 0
        }
    }
    if (groups != 1 || inside || first || changes != 2 || found[14443] != 1 || found[18443] != 1) fail()
    for (i = 1; i <= NR; i++) print line[i]
}
' "$runtime/adapted.json" > "$runtime/run.json"
caddy validate --config "$runtime/run.json"
exec caddy run --config "$runtime/run.json"
