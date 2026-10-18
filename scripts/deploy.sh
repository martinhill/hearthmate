#!/bin/zsh
# Install or update the firmware on the device over the CircuitPython web workflow (wwshell).
#
#   scripts/deploy.sh             show what differs, ask, then upload the changed files
#   scripts/deploy.sh -n          only show what differs (no uploads)
#   scripts/deploy.sh -y          upload without asking
#   scripts/deploy.sh --libs      also install the libraries in requirements.txt (circup)
#   scripts/deploy.sh --no-reload upload only; run the new code later (HA Reload button)
#   scripts/deploy.sh FILE...     limit to these files
#
# Needs uv; wwshell and circup come from the repo's pyproject.toml.
#
# The firmware files are code.py and the repo modules it imports, directly or indirectly, so a
# new module is picked up without editing this script. code.py goes last. Files on the device
# that aren't part of the firmware are listed, never deleted. settings.toml (credentials) is
# never uploaded; the script only warns if the device lacks one.
#
# After the uploads it sends the device the reload command over MQTT (mosquitto_pub, with broker
# credentials in ~/.config/mosquitto_pub) and waits for the device to report whether it resumed
# the burn. The firmware turns autoreload off, so the device reloads once, with every file in
# place; it saves the controller state first and resumes from it (burn_state.py). Firmware older
# than that reloads on each upload and starts in idle.
#
# Configuration comes from the repo's .env (copy .env.example); the environment takes precedence:
#   CP_HOST                      device hostname (as in http://CP_HOST/cp/version.json)
#   CIRCUP_WEBWORKFLOW_PASSWORD  CIRCUITPY_WEB_API_PASSWORD from the device's settings.toml
#   HEARTHMATE_TOPIC             the device's MQTT_TOPIC, for the reload command
#   HEARTHMATE_MQTT_HOST         broker host (optional if -h is in ~/.config/mosquitto_pub)
set -eu
setopt pipefail

repo=${0:A:h:h}
if [[ -f $repo/.env ]]; then
    # Load .env without overriding variables already set in the environment
    while IFS='=' read -r key value; do
        value=${value%\"}; value=${value#\"}; value=${value%\'}; value=${value#\'}
        [[ -z ${(P)key:-} ]] && export "$key=$value"
    done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$repo/.env")
fi

dry_run=0 assume_yes=0 libs=0 reload=1
files=()
while (( $# )); do
    case $1 in
        -n|--dry-run) dry_run=1 ;;
        -y|--yes) assume_yes=1 ;;
        --libs) libs=1 ;;
        --no-reload) reload=0 ;;
        -h|--help) sed -n '2,/^set -eu/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
        -*) print -u2 "Unknown option: $1"; exit 2 ;;
        *) files+=(${1:t}) ;;
    esac
    shift
done

host=${CP_HOST:?Set CP_HOST in .env to the device hostname}
: ${CIRCUP_WEBWORKFLOW_PASSWORD:?Set CIRCUP_WEBWORKFLOW_PASSWORD in .env to the web workflow password}
# wwshell and circup come from the repo's uv project (pyproject.toml)
uvrun=(uv run --quiet --project "$repo")

ww() {
    # wwshell prints the password in its "Found device at http://:PASSWORD@host" line
    $uvrun wwshell --host "$host" "$@" 2>&1 | sed -E 's#//[^@/]*@#//#'
}

# code.py plus every repo module reachable from it through imports
firmware=(${(f)"$(cd "$repo" && python3 - <<'EOF'
import ast, os

seen, todo = [], ["code"]
while todo:
    name = todo.pop()
    if name in seen or not os.path.exists(name + ".py"):
        continue
    seen.append(name)
    with open(name + ".py") as source:
        tree = ast.parse(source.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            todo += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            todo.append(node.module.split(".")[0])
for name in sorted(seen[1:]) + ["code"]:
    print(name + ".py")
EOF
)"})
(( $#files )) || files=($firmware)
for file in $files; do
    [[ -f $repo/$file ]] || { print -u2 "No such file in the repo: $file"; exit 2; }
done

listing=$(ww ls /)
[[ $listing == *$'Size\tName'* ]] || { print -u2 "$listing"; exit 1; }
# Files (not directories) in the listing, after its "Size<TAB>Name" header
device_files=(${(f)"$(print -r -- "$listing" \
    | awk -F'\t' 'listed && $2 !~ /\/$/ { print $2 } $0 == "Size\tName" { listed = 1 }')"})

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

changed=()
for file in $files; do
    if (( ! ${device_files[(Ie)$file]} )); then
        print "new      $file"
        changed+=($file)
        continue
    fi
    ww get "$file" "$tmp/" >/dev/null
    if [[ ! -f $tmp/$file ]]; then
        print -u2 "Could not download $file"; exit 1
    elif cmp -s "$repo/$file" "$tmp/$file"; then
        print "same     $file"
    else
        print "changed  $file ($(diff "$tmp/$file" "$repo/$file" | grep -c '^[<>]') lines)"
        changed+=($file)
    fi
done
for file in $device_files; do
    if [[ $file == *.py && $file != settings.toml ]] && (( ! ${firmware[(Ie)$file]} )); then
        print "extra    $file (on the device only, left alone)"
    fi
done
(( ${device_files[(Ie)settings.toml]} )) || print "warning: the device has no settings.toml; create it from the README"

if (( dry_run )); then
    (( libs )) && print "would run: circup --host $host install -r requirements.txt"
    exit 0
fi
if (( ! $#changed && ! libs )); then
    print "\nThe device is up to date."
    exit 0
fi
if (( ! assume_yes )); then
    (( reload )) && print "\nThe device reloads after the uploads and resumes the burn."
    read -q "?Upload ${#changed} file(s)$( (( libs )) && print ' and libraries') to $host? [y/N] " \
        || { print; exit 1; }
    print
fi

if (( libs )); then
    # circup reads the password from CIRCUP_WEBWORKFLOW_PASSWORD
    (cd "$repo" && $uvrun circup --host "$host" install -r requirements.txt 2>&1 | sed -E 's#//[^@/]*@#//#')
fi
for file in $changed; do
    # put's optional LOCATION is a directory prefix joined to the file name: none means the root
    out=$(ww put --overwrite "$repo/$file")
    # Check the upload by reading it back
    rm -f "$tmp/$file"
    ww get "$file" "$tmp/" >/dev/null
    if ! cmp -s "$repo/$file" "$tmp/$file"; then
        print -u2 "Upload of $file failed:\n$out"; exit 1
    fi
    print "uploaded $file"
done

(( reload )) || { print "Not reloaded: press Reload in Home Assistant to run the new code."; exit 0; }

# Reload once, now that every file is in place, and wait for the device to say how it came back
topic=${HEARTHMATE_TOPIC:?Set HEARTHMATE_TOPIC in .env to the device MQTT_TOPIC}
broker=(${HEARTHMATE_MQTT_HOST:+-h $HEARTHMATE_MQTT_HOST})
mosquitto_pub=${commands[mosquitto_pub]:-}
mosquitto_sub=${commands[mosquitto_sub]:-}
if [[ -n $mosquitto_sub ]]; then
    $mosquitto_sub $broker -t "$topic/log" -W 90 >"$tmp/log" 2>/dev/null &
    sub_pid=$!
    sleep 1
fi
if [[ -z $mosquitto_pub ]] || ! $mosquitto_pub $broker -t "$topic/command" -m reload 2>/dev/null; then
    [[ -n ${sub_pid:-} ]] && kill $sub_pid 2>/dev/null
    print "Could not send the reload command (needs mosquitto_pub and ~/.config/mosquitto_pub)."
    print "Press Reload in Home Assistant to run the new code."
    exit 0
fi
print "reload requested"
[[ -n ${sub_pid:-} ]] || exit 0
pattern='Resumed |Starting in idle|Unknown command: reload'
line=""
for i in {1..90}; do
    line=$(grep -m1 -E "$pattern" "$tmp/log" || true)
    [[ -n $line ]] && break
    kill -0 $sub_pid 2>/dev/null || break
    sleep 1
done
kill $sub_pid 2>/dev/null || true
if [[ $line == *"Unknown command: reload"* ]]; then
    print "The device ran firmware without the reload command; the uploads reloaded it instead."
elif [[ -n $line ]]; then
    # e.g. "... INFO __main__: Resumed temp_control after 14s (SUPERVISOR_RELOAD)"
    print "device: ${line#*__main__: }"
else
    print "No word from the device within 90 s; check its log (scripts/tmux.sh)."
fi
