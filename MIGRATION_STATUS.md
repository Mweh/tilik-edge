# Consolidation delivery status

## Implemented

- Single deployable project with shared configuration and data store.
- Existing capture engine carried forward; Linux camera support only.
- Continuous capture independent of browser requests.
- GUI rule changes validated and applied by the capture worker.
- One-command development startup and independent Linux boot services.
- Persistent device identity and stable device/event-based capture IDs.
- Atomic capture publication; unfinished bundles excluded from API.
- PNG/raw validation, artifact sizes and hashes, integrity checks before transfer.
- Persistent indexed catalog, pagination and snapshot support.
- Existing POC iOS array/download endpoint compatibility.
- Legacy capture copy/import with provenance preservation and conflict checks.
- Optional API token and HTTPS certificate configuration.
- Setup, migration, rollback and acceptance instructions in README.md.

## Verified locally

18 tests passed with temporary simulation stores and loopback HTTP servers.
The combined launcher produced a capture, served PNG/JSON/BIN with matching
manifest hashes, and released the API port on shutdown. Other tests cover rules,
metadata/source mapping, incomplete publication, corrupt artifacts, identity after
move/restart, pagination, authentication, and repeatable legacy import.
Python compilation, JavaScript syntax and shell syntax checks passed.

## Not yet verified on hardware

Deployment has not been performed on the user's Raspberry Pi. PureThermal live
capture, Linux systemd installation/restarts, browser visual interaction, optional
TLS configuration, and physical iPhone downloads need the acceptance run described
in README.md. The old project folders remain the rollback source.

## Production integration follow-up

Tilik's production iOS client still needs pairing/credential UX, pagination and
manifest verification. Current endpoint compatibility supports the earlier POC
client but does not implement those iOS features. Initial import into an old POC
phone cache may duplicate historical items because old path-hash IDs differ.
Offline certificate provisioning and pairing are not supplied by this consolidation.
