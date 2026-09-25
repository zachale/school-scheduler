#!/bin/sh
# Run on the host from the repo checkout: install the units and start the timers.
# Inputs (config.yaml, tasks.yaml, events.yaml, gcal.json) and secrets are placed separately.
set -eu
cd "$(dirname "$0")"
mkdir -p ~/.config/systemd/user
cp f26-planner-poll.service f26-planner-poll.timer f26-planner-sweep.service f26-planner-sweep.timer \
    ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now --quiet f26-planner-poll.timer f26-planner-sweep.timer
systemctl --user list-timers 'f26-planner-*' --no-pager
