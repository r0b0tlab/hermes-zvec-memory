# Catalog submission policy and release gates

Policy checked against Hermes commit
`bfda74c71acd884f170345064d537bc0d3a30d20`. Re-read upstream before any
submission: admission policy can change independently of this plugin.

## Authorities

- [Catalog admission policy and entry schema](https://github.com/NousResearch/hermes-agent/blob/bfda74c71acd884f170345064d537bc0d3a30d20/plugin-catalog/README.md)
- [Public submission checklist](https://github.com/NousResearch/hermes-agent/blob/bfda74c71acd884f170345064d537bc0d3a30d20/website/docs/user-guide/features/plugin-catalog.md#submitting-a-plugin-to-the-catalog)
- [Admission CI](https://github.com/NousResearch/hermes-agent/blob/bfda74c71acd884f170345064d537bc0d3a30d20/.github/workflows/plugin-catalog-ci.yml)
- [Structural validator](https://github.com/NousResearch/hermes-agent/blob/bfda74c71acd884f170345064d537bc0d3a30d20/scripts/validate_plugin_catalog.py)

## Current requirements

- Human-maintainer review is required for a new entry and every SHA bump.
  Submissions come from the owner/major contributor or a maintainer-curated sweep.
- The repository must be public and cloneable, with a real release/tag.
- Pin the exact reachable **40-hex lowercase commit SHA**, not a tag, branch,
  short SHA, or unreleased working tree. Use `subdir: zvec-memory`.
- **No self-updating code:** the catalog build must not download and replace
  its own plugin files. Updates go through a reviewed catalog SHA bump and
  `hermes plugins update <name>`. An explicit engine dependency installation
  is not permission to add a plugin self-updater.
- Declared tools, hooks, middleware and required environment variables must
  match the pinned source. The install scanner rejects `dangerous` findings;
  `caution` warnings require reviewer assessment, not dismissal.
- Review dependency bounds and security policy. Hermes's own dependency
  quarantine is **not a plugin commit-age waiting period**. The former age
  rule was replaced by the no-self-updater rule in
  [48763a4](https://github.com/NousResearch/hermes-agent/commit/48763a4d019a88626123c2eb3933e7a9900db0db).
- Check the catalog's blocklist and provider-name conflicts. Desktop SDK rules
  apply if desktop code is introduced; this provider does not gain permission
  to bypass them.

## Historical pin versus the pending release

`plugin-catalog-entry.yaml` still records the historical **v0.2.0** commit
`36fdba76e1693ac8f9da14e0f0f88438aba43757`. Earlier owner/public-repository and
validation observations apply to that historical check, not a new release.
Do not copy this entry unchanged as a v0.3.0 submission, relabel its old SHA,
or treat the historical validation as current certification.

The **v0.3.0 candidate is not yet certified or published**. Publication,
production deployment and catalog submission are separate approval gates.
No catalog PR is authorized by release publication alone.

After the release is verified, resolve its actual landing commit with
`git rev-list -n1 v0.3.0` and compare it with the remote annotated tag's peeled
commit. Only then prepare the catalog entry with that literal SHA,
`version: "0.3.0"`, `category: memory`, and `subdir: zvec-memory`. Re-prove
capabilities and platform support at that pin. Keep any host-compatibility
limitations explicit; do not invent an untested `requires_hermes` floor.
A follow-up metadata commit may name the release SHA; never move a published
tag to solve the self-reference problem.

## Fresh admission checks, after separate submission approval

1. Re-read the authorities and current CI, verify repository ownership,
   public cloneability, release existence and blocklist/name status.
2. In a disposable validation environment, stage the proposed entry alongside
   the current catalog and run the structural gate from the host checkout:
   `python3 scripts/validate_plugin_catalog.py "$STAGED_CATALOG"`.
3. Fresh-clone the plugin, detach at the exact proposed SHA, and validate the
   installed subtree. Current CI uses
   `hermes plugins validate --install-deps "$PIN_CHECKOUT/zvec-memory"`.
   This can install dependencies: use an explicitly prepared isolated host
   environment and approved dependency inputs, never the active profile or
   live Hermes interpreter. Capture the command, source identity, exit status,
   warnings and capability/security checks.
4. Inspect for self-updaters beyond CI's JavaScript fetch-plus-file-write
   heuristic. A grep result alone is not a policy or security approval.
5. Review the old-to-new pinned diff and fix or explicitly disposition every
   warning. Open the catalog PR only with fresh evidence for both gates and
   the pinned capability surface. Maintainer review remains required.

These are required future checks, not commands claimed to have passed for
v0.3.0. Source retrieval and documentation checks do not certify admission.
