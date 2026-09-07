# TAP Core

Open infrastructure for observing and extending the exchange between local applications and the network.

TAP Core is being extracted from an existing working TAP installation. This repository currently contains the release backlog and project scope; it does not yet contain an installable runtime.

## Scope

- Traffic capture and explicit routing policies by host, transport and source application.
- Local records, body storage and repeatable processing by independent readers.
- Response mutators, injected page code and a same-origin WebSocket bridge to local tools.
- Pack installation, lifecycle, configuration, compatibility checks and diagnostics.
- Installation, update, recovery and removal on supported macOS configurations.

Application-specific readers, UI changes and workflows belong in packs. Packs may be community-maintained or commercial. The core must support third-party packs without a vendor account or payment service.

## First release

A person on a clean supported Mac can install TAP Core, enable an open example pack, capture a controlled request, process its saved record, inject a page script and complete a page-to-local round trip. The same installation must survive restart and update, explain failures and restore the previous network configuration on removal.

Support for source-application attribution and routing must be established experimentally. A process label alone is not evidence that per-application routing works. Supported and unsupported configurations will be documented before release.

We will port and verify existing mechanisms incrementally. A general workflow engine, marketplace, billing service and application-specific features are outside this first core release.

## Development

Issues contain the scope, acceptance criteria and dependencies for each slice. Follow the release tracking issue and milestones. A slice is complete when its user-visible behavior is demonstrated, not only when its files have been moved.

Read [TECHNOLOGY.md](TECHNOLOGY.md) for release requirements, working stack choices
and unresolved packaging decisions before starting an implementation task.

## License

The new scaffold in this repository is MIT licensed. Existing implementation files and bundled dependencies must pass the migration and license review before being added.
