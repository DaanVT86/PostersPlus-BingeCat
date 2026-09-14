# v2 render freshness clocks

`ImmutableRenderSnapshot` carries the captured metadata clock in
`evaluated_at`.  Facts and ratings are always validated against that clock.
The optional `artwork_evaluated_at` field is an acquisition clock for a new
source-art derivative:

- It may be omitted for the legacy contract.
- When present, it must be greater than or equal to `evaluated_at`.
- A selected source reference whose `checked_at` is at or before
  `evaluated_at` is validated against `evaluated_at`, even when the artwork
  clock is newer.  This preserves captured old artwork, including artwork
  that is expired relative to the newer clock.
- A selected source reference whose `checked_at` is after `evaluated_at`
  requires `artwork_evaluated_at`.  Its interval must satisfy
  `observed_at <= checked_at <= artwork_evaluated_at < expires_at`.
- Missing artwork clock, an artwork clock before `checked_at`, and an
  interval expired at its applicable clock are rejected fail-closed.

The timestamps are provenance only.  Neither clock is included in the visual
snapshot projection or canonical visual snapshot hash.  The renderer does not
rewrite timestamps or consult wall time, and this seam does not change
`RENDERER_REVISION` or compositor output.
