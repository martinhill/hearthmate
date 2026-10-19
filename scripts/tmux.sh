#!/bin/zsh
# Open (or re-attach to) a tmux window for watching Hearthmate over MQTT:
#   top:          controller log lines - temp_control transitions and vent moves
#   bottom left:  controller state topics, timestamped
#   bottom right: shell in the repo
# The panes go in a window named mqtt, added to the session if it already exists.
# In iTerm2 it attaches in control mode (tmux -CC), so the panes are native iTerm2 splits;
# a session iTerm2 already shows just gets the window.
#
# Configuration comes from the repo's .env (copy .env.example); the environment takes precedence:
#   HEARTHMATE_TOPIC      the device's MQTT_TOPIC from settings.toml (required)
#   HEARTHMATE_MQTT_HOST  broker host (optional if -h is in the mosquitto_sub config file)
#   HEARTHMATE_SESSION    tmux session name (default: hearthmate)
#   Broker credentials go in ~/.config/mosquitto_sub, one option per line, e.g.
#     -u someuser
#     -P somepassword
set -eu

repo=${0:A:h:h}
if [[ -f $repo/.env ]]; then
    # Load .env without overriding variables already set in the environment
    while IFS='=' read -r key value; do
        value=${value%\"}; value=${value#\"}; value=${value%\'}; value=${value#\'}
        [[ -z ${(P)key:-} ]] && export "$key=$value"
    done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$repo/.env")
fi

session=${HEARTHMATE_SESSION:-hearthmate}
topic=${HEARTHMATE_TOPIC:?Set HEARTHMATE_TOPIC to the device MQTT_TOPIC}
# Full path, as the tmux server's PATH may lack it (e.g. MacPorts /opt/local/bin)
mosquitto_sub=${commands[mosquitto_sub]:?mosquitto_sub not found}
sub="$mosquitto_sub${HEARTHMATE_MQTT_HOST:+ -h $HEARTHMATE_MQTT_HOST}"

# Lines from temperature_target.py, vent_closer.py, vent_mover.py and the Idle state
log_filter='temp_control|timed_close|Closing to|Closed|override|Override|Moving to|Target reached|Detected vent'
state_args=""
for state_topic in state air_vent/state thermal/max/state 'temp_control/+/state'; do
    state_args+=" -t '$topic/$state_topic'"
done

# The panes live in their own window, so the script also works with a session that already
# exists, e.g. one created by an iTerm2 profile running tmux -CC
window=mqtt
window_id=$(tmux list-windows -t "=$session" -F '#{window_name} #{window_id}' 2>/dev/null \
    | awk -v name="$window" '$1 == name { print $2; exit }')
if [[ -z $window_id ]]; then
    if tmux has-session -t "=$session" 2>/dev/null; then
        log_pane=$(tmux new-window -d -P -F '#{pane_id}' -t "=$session:" -n "$window" -c "$repo")
    else
        log_pane=$(tmux new-session -d -P -F '#{pane_id}' -s "$session" -n "$window" -c "$repo")
    fi
    window_id=$(tmux display-message -p -t "$log_pane" '#{window_id}')
    tmux send-keys -t "$log_pane" \
        "$sub -t '$topic/log' | grep --line-buffered -E '$log_filter'" Enter

    state_pane=$(tmux split-window -v -d -P -F '#{pane_id}' -t "$log_pane" -c "$repo")
    tmux send-keys -t "$state_pane" "$sub -F '%I %t %p'$state_args" Enter

    shell_pane=$(tmux split-window -h -d -P -F '#{pane_id}' -t "$state_pane" -c "$repo")
    tmux select-pane -t "$shell_pane"
fi
tmux select-window -t "$window_id"

if [[ -n ${TMUX:-} ]]; then
    # Inside tmux: switch only if this client is on another session (switching to the
    # current session makes an iTerm2 control-mode client redraw and nothing else)
    if [[ $(tmux display-message -p '#{session_name}') != $session ]]; then
        exec tmux switch-client -t "$window_id"
    fi
elif tmux list-clients -t "=$session" -F '#{client_control_mode}' | grep -qx 1; then
    # iTerm2 already shows this session; a second control-mode client would just exit
    print "Session $session is open in iTerm2; the $window window is there"
elif [[ ${TERM_PROGRAM:-} == iTerm.app || ${LC_TERMINAL:-} == iTerm2 ]]; then
    # iTerm2 control mode: tmux windows and panes become native iTerm2 tabs and splits
    exec tmux -CC attach -t "=$session"
else
    exec tmux attach -t "=$session"
fi
