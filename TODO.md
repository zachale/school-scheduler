# TODO

A term planner that time-blocks school work around a Google Calendar and tracks progress
through a Notion database.

- [x] Import the planner code with its real history, leaving out personal inputs
- [x] Replace the always-on loop with one script run by systemd timers:
      `poll` every minute, a full `sweep` once a day
- [ ] Example inputs (config, tasks, events) so the repo runs without personal data
- [x] README: what it does, how to set it up, where secrets go
