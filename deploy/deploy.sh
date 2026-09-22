#!/bin/sh
# Push the planner's code and inputs to the VM and restart the service.
#   deploy/deploy.sh            deploy and restart
#   deploy/deploy.sh --no-start deploy only (for a first dry run on the VM)
set -eu
cd "$(dirname "$0")/.."
rsync -a config.yaml tasks.yaml events.yaml gcal.json plan.py verify.py gcal.py service.py \
    notion.py notion_sync.py notion_map.py learn.py \
    deploy/f26-planner.service vm:f26-planner/
ssh vm 'mkdir -p ~/.config/systemd/user \
    && cp ~/f26-planner/f26-planner.service ~/.config/systemd/user/ \
    && systemctl --user daemon-reload'
if [ "${1:-}" != "--no-start" ]; then
    ssh vm 'systemctl --user enable --quiet f26-planner && systemctl --user restart f26-planner \
        && sleep 3 && systemctl --user --no-pager status f26-planner | head -5'
fi
