# Release Readiness

> Evergreen internal checklist for deciding whether the product is ready to ship.

This is intentionally version-agnostic. Do not freeze it around one old release number and pretend the board is still current.

## Release Bar

A release is ready when the product is coherent, test evidence exists for the important claims, and the packaging story is not embarrassing.

## Product Surface

Before shipping, confirm:

- the root docs tell a clear story quickly
- the admin console surfaces match the product we actually want to defend
- deprecated or hidden features are not still being advertised as headline value
- onboarding and first-use flows are consistent with the current operating model

## Documentation

Before shipping, confirm:

- root docs are limited to user-facing essentials
- internal planning docs live under `docs/internal/`
- architecture docs describe stable capabilities, not brittle file counts
- installer and deployment guides avoid hardcoded stale version strings

## Quality Gates

Before shipping, confirm:

- lint and targeted tests run cleanly for the touched surfaces
- the main user claims have an automated verification path where practical
- packaging or setup changes have at least one smoke path
- new behavior has tests or a documented reason it does not

## Data And Migration Discipline

Before shipping, confirm:

- schema changes are represented as numbered migrations
- runtime backfill logic is not silently substituting for proper migrations
- forward upgrades work on existing user data
- critical continuity and audit tables are part of the supported migration path

## Security And Control Plane

Before shipping, confirm:

- secrets handling and auth expectations are still documented accurately
- admin surfaces bind and behave as intended for local deployment
- operator-visible control paths still match current behavior

## Packaging And Evaluation

Before shipping, confirm:

- Windows packaging still produces a working installer if that path is claimed
- Docker instructions still match the real deployment path if that path is claimed
- an evaluator can get from install to first value without guesswork

## Final Review Questions

Ask these before tagging a release:

- What are we asking a new user to believe?
- Which automated checks actually back that claim up?
- Which docs would make us look sloppy if someone opened them cold?
- Which surfaces are still carrying internal-history baggage instead of product logic?
