# SD-WAN Analyzer application package.
#
# Module boundaries (data flows one direction, left to right):
#
#   collectors  ->  normalize  ->  storage  ->  service (app API)  ->  web (presentation)
#
# - collectors:  read-only NCOS status/config snapshots. No business logic.
# - normalize:   turn raw NCOS shapes into stable internal records.
# - storage:     store interfaces + in-memory defaults; durable.py/persistence.py hold
#                the durable JSONL backend (tmp/telemetry). See their docstrings.
# - service:     composes collectors + normalize + storage into the app-facing API.
# - web:         http.server presentation. Calls service, NEVER NCOS directly.
#
# The browser talks only to `web`, which talks only to `service`. The browser
# never reaches NCOS APIs. Collection/storage/business logic stay out of the
# presentation layer.

VERSION = '1.0.3'
