# TAP Core

Open infrastructure for observing and extending the exchange between local applications and the network.

TAP Core is being extracted from an existing working TAP installation. A first
[development-checkout runtime](docs/runtime.md) provides isolated macOS profiles
with the existing `install`, `on`, `off`, `status`, `doctor` and `where` command
surface. This is not yet a packaged clean-Mac release.
[Capture record v1 and journal positions](docs/capture-records.md) define the
current storage input for independent readers, including legacy compatibility
and explicit errors when retention removes a saved position.
[Independent reader runs](docs/readers.md) add per-reader progress, resume and
explicit replay over retained capture, with synthetic subprocess examples.

[The first live vertical slice](docs/live-slice.md) connects captured data to a
reader projection and an injected browser page through the existing WS bridge.

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

The [initial extraction proposal](docs/extraction-start.md) records observed
couplings, proposed distribution boundaries and a reproducible characterization
harness for the trusted legacy source. The [release tracker](https://github.com/inem/tap-core/issues/1)
links the implementation work. The legacy directory structure is not the target
architecture by default.

Read [TECHNOLOGY.md](TECHNOLOGY.md) for release requirements, working stack choices
and unresolved packaging decisions before starting an implementation task.

## License

The new scaffold in this repository is MIT licensed. Existing implementation files and bundled dependencies must pass the migration and license review before being added.
