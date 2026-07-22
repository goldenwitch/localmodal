# Scout Vocabulary

This file is the canonical vocabulary for Scout source management. It
disambiguates terms used by the plan; it does not choose unresolved locator
syntax, materializer implementation, batch semantics, or artifact paths.

## Source Terms

| Term | Canonical meaning | Not |
| --- | --- | --- |
| **source** | One named unit of managed material. Its immutable `name` is its identity. | A media type, origin protocol, local path, query route, or tool. |
| **source declaration** | One complete declaration version for a source: `name`, origin locator, required MIME, and `ttl_days`. `name` is immutable; an `add` may replace the complete declaration for that name. | A public operation row or a fetched snapshot. |
| **source row** | One ordered public proposal operation. An `add` carries a complete declaration; a `remove` names one target. | The declaration itself or a runtime mutation. |
| **origin locator** | The declaration value reconciled by a materializer to one source origin. | Source identity, artifact location, citation, or a discovery instruction. |
| **inventory** | An audit or migration listing of existing material. | Declaration discovery or direct index intake. |
| **source discovery** | Creating a new declaration from content, a ref, a glob, or an inventory. Scout's normal commands do not do this. | Parsing an already materialized source artifact. |

An inventory, repository glob, VINE ref, redirect, or materialized response
cannot create a source row or declaration. A retained origin enters through an
explicit `add` row and receives one outcome.

## Material Terms

| Term | Canonical meaning | Not |
| --- | --- | --- |
| **materialize** | Produce a staged candidate from one declared origin locator. | Publish, index, or make material query-visible. |
| **fetch** | Remote materialization over the admitted remote transport. | The generic name for all materialization. |
| **re-fetch** | An explicit materialization requested by an `add` targeting an existing source. | A freshness no-op. |
| **absent** | A registered source with no live snapshot. | An unregistered name or a missing declaration. |
| **refresh-stale** | Select stale or absent registered sources, then materialize them through their lifecycle. | Source registration or fresh-source re-fetching. |
| **candidate** | Private staged materialization result with validation evidence. | A live snapshot or corpus input. |
| **artifact** | Local bytes and representation produced for a materialized source. | Source identity, a query result, or an index generation. |
| **live snapshot** | The one query-visible materialized state for a source in a committed publication, or no snapshot. | A candidate, partial artifact, or failed attempt. |
| **required MIME** | Declared expected media type. | Origin transport or chunk-adapter kind. |
| **observed MIME** | Media type evidenced by materialization. | A caller-selected chunking strategy. |
| **chunk kind** | Local adapter selection for a materialized representation. | Required or observed MIME. |

The exact origin-locator and reconciliation contract belongs to
`ssm/design/source-origin-locator`. Remote HTTPS admission belongs to
`ssm/design/fetch-admission`.

## Publication Terms

| Term | Canonical meaning | Not |
| --- | --- | --- |
| **chunk** | One indexable text segment with canonical tags. | A source or its whole artifact. |
| **index id** | Unique identity of one indexed chunk within the published index namespace. | A user-facing resolver handle or a cross-publication identity guarantee. |
| **citation** | Stable resolver handle carried in chunk tags. Multiple segments may share it. | An index id or artifact path. |
| **index generation** | Built indexed material referenced by a publication. | Independent reader truth. |
| **publication** | Immutable generation binding the complete source-state snapshot, artifact references, and index generation. | A candidate or a mutable index directory. |
| **master pointer** | The one pointer selecting the current publication. | A route-specific index `CURRENT`. |
| **current publication** | The immutable publication named by the master pointer. | The generic ledger or a plausible sibling generation. |
| **Scout store** | The current publication together with required checked-in configuration, evaluated as one validity boundary. | A source category or a raw artifact collection. |
| **source control plane** | The sole source mutation, publication, and post-activation public-query path. | A wrapper around legacy reader output. |
| **activation** | The one switch where the source control plane takes public-query ownership through the first source-bound publication. | A compatibility reader mode. |

## Health Terms

| Term | Canonical meaning | Not |
| --- | --- | --- |
| **valid** | Every declared invariant of the current publication and required configuration holds. | Freshness alone. |
| **invalid** | One or more declared current-store invariants fail; indexed answers are suppressed and every observed current-store failure is returned. | A failed private candidate or failed private refresh attempt. |
| **diagnostic** | A value of Scout's closed error union, including required evidence and repair instruction. | Display text parsed back into state. |
| **warning** | Typed, non-invalidating observation visible only while the store is valid. | A diagnostic or a third snapshot state. |
| **attempt record** | Durable non-live evidence of an attempt outcome where the lifecycle design requires it. | A source snapshot or candidate. |

## Legacy Names

`papers`, `docs`, and `workspace` name current legacy code paths only. They are
not source categories, namespaces, lifecycle variants, validity boundaries,
adapter inputs, or reader routes in the source-management design. Likewise,
the pre-activation legacy runtime is not a compatibility state of the Scout
store. After activation, every public search reader resolves the master
publication and its common health gate.

## Usage

When a design sentence could use more than one term above, use the canonical
term. Do not introduce a synonym that changes whether something is declared,
private, query-visible, or authoritative.