#!/bin/zsh
# Open (or re-attach to) a tmux session for watching Hearthmate over MQTT:
#   top:          controller log lines - temp_control transitions and vent moves
#   bottom left:  controller state topics, timestamped
#   bottom right: shell in the repo
# In iTerm2 it attaches in control mode (tmux -CC), so the panes are native iTerm2 splits.
#
# Configuration, kept out of the repo:
#   HEARTHMATE_TOPIC      the device's MQTT_TOPIC from settings.toml (required)
#   HEARTHMATE_MQTT_HOST  broker host (optional if -h is in the mosquitto_sub config file)
#   HEARTHMATE_SESSION    tmux session name (default: hearthmate)
#   Broker credentials go in ~/.config/mosquitto_sub, one option per line, e.g.
#     -u someuser
#     -P somepassword
set -eu

session=${HEARTHMATE_SESSION:-hearthmate}
repo=${0:A:h:h}
topic=${HEARTHMATE_TOPIC:?Set HEARTHMATE_TOPIC to the device MQTT_TOPIC}
# Full path, as the tmux server's PATH may lack it (e.g. MacPorts /opt/local/bin)
mosquitto_sub=${commands[mosquitto_sub]:?mosquitto_sub not found}
sub="$mosquitto_sub${HEARTHMATE_MQTT_HOST:+ -h $HEARTHMATE_MQTT_HOST}"

if ! tmux has-session -t "=$session" 2>/dev/null; then
    # Lines from temperature_target.py, vent_closer.py, vent_mover.py and the Idle state
    log_filter='temp_control|timed_close|Closing to|Closed|override|Override|Moving to|Target reached|Detected vent'
    state_args=""
    for state_topic in state air_vent/state thermal/max/state 'temp_control/+/state'; do
        state_args+=" -t '$topic/$state_topic'"
    done

    tmux new-session -d -s "$session" -c "$repo"
    tmux send-keys -t "$session:0.0" \
        "$sub -t '$topic/log' | grep --line-buffered -E '$log_filter'" Enter

    tmux split-window -v -t "$session:0.0" -c "$repo"
    tmux send-keys -t "$session:0.1" \
        "$sub -F '%I %t %p'$state_args" Enter

    tmux split-window -h -t "$session:0.1" -c "$repo"
    tmux select-pane -t "$session:0.2"
fi

if [[ -n ${TMUX:-} ]]; then
    exec tmux switch-client -t "$session"
elif [[ ${TERM_PROGRAM:-} == iTerm.app || ${LC_TERMINAL:-} == iTerm2 ]]; then
    # iTerm2 control mode: tmux windows and panes become native iTerm2 tabs and splits
    exec tmux -CC attach -t "$session"
else
    exec tmux attach -t "$session"
fi
