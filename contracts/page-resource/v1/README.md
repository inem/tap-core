# TAP page resource provider contract v1

`tap.page-resource/v1` is the public seam between a reusable page-library
publisher, a consuming pack and the TAP host. It does not prescribe GitHub,
npm, a marketplace or another transport. Those systems may deliver artifacts;
the browser runtime never fetches source or dependencies from them.

## Provider side

A standalone UI/site-adapter release publishes `tap-resource.json` beside one
immutable browser file. The manifest uses the sibling `schema.json` shape:

- `id` is stable semantic identity across providers and packs;
- `version` is an exact release version;
- `kind` is `browser-classic-script` in v1;
- `file` is a canonical relative path beneath the provider root;
- `sha256` identifies the exact executable bytes;
- `license` and `source_revision` keep redistribution review reproducible.

The reference validator verifies both metadata and bytes without executing the
resource:

```sh
python3 -B -m tap_core.page_resources \
  contracts/page-resource/v1/provider.fixture
```

Passing this command proves provider conformance only. It does not grant page
access or prove compatibility with a current website.

## Pack side

A self-contained pack copies the provider object into its top-level `resources`
array and includes the declared file. Its `page/browser-scripts-v1` entrypoint
contains an ordered `uses` array of `{id, version}` references. Every use must
resolve inside the artifact in v1. This deliberate vendoring makes install and
rollback independent of a repository, registry, CDN or network connection.

The pack builder verifies the provider hash. A future build resolver may obtain
the provider from a local directory, GitHub Release or registry, but it must
freeze the same object and bytes into the resulting `.tap-pack`. A branch name,
mutable URL or unverified download is not an installed dependency.

## Host side

On install the host verifies the pack snapshot and materializes each page
resource under:

```text
<profile>/resources/page/<id>/<version>/<sha256>.js
```

The selected pack version retains its vendored source for whole-artifact
verification; page execution uses the shared resource-store path. Shared files
are retained as an immutable cache when a pack is removed. Garbage collection
is a separate operation because deletion must account for every installed pack
version, rollback history and active startup snapshot.

Before activation the host accumulates ordered uses from all enabled packs. For
each resource ID:

- the same version and SHA-256 collapse to one injection per document;
- origins from all matching uses are unioned;
- different versions conflict;
- equal ID/version with different hashes conflict;
- a missing provider or changed store file fails before page code runs.

Pack IDs are sorted and each pack's `uses` order is retained, making the first
provider and resulting asset indices deterministic. Resources are then selected
per exact document origin; an authenticated request from another allowed origin
cannot fetch a resource outside its plan.

## Ownership and compatibility

The provider owns its semantic API, release version, compatibility probes and
source provenance. A pack owns its ordered uses and feature behavior. TAP Core
owns validation, immutable storage, grants, deterministic composition and
delivery to the correct origin. All page code still shares the authority of its
document; this contract is composition and integrity, not a JavaScript sandbox.

Exact versions are intentional for v1. Version ranges, remote dependency
resolution, signatures, registry trust, cache collection and simultaneous
incompatible versions require later contracts rather than implicit behavior.
