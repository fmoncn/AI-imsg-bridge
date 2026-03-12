# AI-iMsg-Bridge Release Notes

## Current Milestone

This release upgrades the project from a single-process bridge script into a more complete remote AI control system for iMessage.

## Highlights

- Added SQLite-backed persistent state for:
  - message offsets
  - model bindings
  - task lifecycle
  - pending confirmations
  - review groups
- Added task control commands:
  - `/tasks`
  - `/task <id>`
  - `/task cancel <id>`
  - `/task retry <id>`
- Added remote service operations:
  - `/service status`
  - `/restart`
- Added explicit search control:
  - `/web ...`
  - `/local ...`
- Added fast-path routing for short acknowledgements and lightweight chat
- Added aggregated dual-model review workflow:
  - `/review`
  - `/review <id>`
- Added safer process-group termination for stop/timeout behavior
- Added persistent review/task metadata for post-run inspection and retries
- Split responsibilities into dedicated modules:
  - `engine.py`
  - `router.py`
  - `store.py`
  - `message_store.py`
  - `process_utils.py`
  - `transport.py`
  - `state.py`

## Verification

- Python compilation checks pass
- Regression test suite passes: `13 passed`

## Notes

- The bridge service must be restarted after code/config changes
- `BRIDGE_SECRET` is still recommended before wider use of remote control commands
