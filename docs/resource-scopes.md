# Project resource scopes

On POSIX hosts with no-follow directory descriptors, Skill Trace 0.4.0 exposes an explicit resource resolver through Skills Auditor 0.12.0.
Global Skill instructions stay generic. A project owns its portable context declaration;
each environment/workspace/Skill binding owns private state. Capture is optional and does not
authorize or disable resource access. This is an additive opt-in mechanism: existing tools and
Skills do not become isolated merely because the plugin is installed.

## Identity and storage

The shared project file `.skill-context.json` uses this contract:

```json
{
  "schema_version": 1,
  "project_id": "example-project",
  "skills": {"example-skill": {"context": ["DESIGN.md"]}}
}
```

Commit only confirmed shareable context. Never put credentials, local paths, personal payloads,
or environment bindings in this declaration. Context files must be regular single-link UTF-8
files inside the selected project. Every declared file must exist for resolve/read-context;
there is no inheritance from a parent, sibling, global directory or previous invocation.
A nested `.skill-context.json` marks a separate project boundary, including malformed manifests.
Pass the actual task target to detect a nested project when operating from a larger repository.

An explicit `init` reuses the workspace Node's environment/generation/runtime fingerprint and
physical workspace binding. It registers an empty Codex project Skill root if needed; it does not
install or activate Skills. The portable project ID is additionally bound to the physical project
and state-directory device/inode identities. Directory replacement, copied state, another generation
or a foreign project manifest are rejected. Host fingerprints detect accidents, not remote attestation.

The dedicated state directory must be outside the project tree. Private resources live under
`resource-scopes/<workspace-and-skill-hash>/private`, with owner-only directory/file permissions.
Bindings are target-local. Different workspaces and Skills get different private paths; environment
state directories remain separate. A shared project ID does not grant cross-environment private access.
Existing enrolled workspace/environment identities can be reused with their existing state directory.
Use exact IDs/generation/path; do not infer identity from a project basename or choose a recent match.

## Explicit initialization and use

Example (replace paths and identities with the selected target):

```bash
skills-audit skill-trace scope init \
  --project-root /work/example-project --project-id example-project \
  --environment example-host --generation instance-1 --workspace example-worktree \
  --state-dir /private/example-host-state --skill example-skill --context DESIGN.md

skills-audit skill-trace scope read-context \
  --project-root /work/example-project --target /work/example-project/src \
  --environment example-host --generation instance-1 --workspace example-worktree \
  --state-dir /private/example-host-state --skill example-skill
```

`init` can declare a context file before it exists, but does not invent its content. It is idempotent
for the same binding and refuses a different existing declaration. Add another Skill by explicitly
editing the project's manifest and initializing that Skill's binding. Existing source and private
data are not automatically migrated. Do not initialize unknown projects as a fallback for a denial.
A bounded task may proceed using only its explicitly supplied brief without persistent context.

`resolve` returns verified scope paths and context content hashes; `read-context` also returns text.
`read-private --name draft.txt` emits the private file bytes. `write-private --name draft.txt` reads
bytes from stdin. All commands require the same explicit binding flags. Private subdirectories must
already exist inside the private root; the resolver never creates directories from arbitrary names.
Individual reads/writes are limited to 1 MiB; at most 32 context resources are allowed per Skill.
Private writes replace the selected file in place and are not a transactional document database.

Rejects return exit 2 and a stable body-free reason. No returned path is a durable capability:
run the scoped operation again before the next read/write. Missing, malformed, foreign or inaccessible
resources do not fall back to another scope. Already loaded context with an unverified origin must
not be reused; start a clean task when unrelated project content has already contaminated the context.

## Evidence and limits

When existing Skill Trace capture settings enable collection, scope operations append
`skill-resource-decision/v1` JSONL to the selected target's `scope-decisions.jsonl`. This separate
stream records a hashed scope identity, timestamp, operation, outcome, reason and, for context
resolution, content hashes. It contains no context/private bodies, absolute paths or prompts.
It is diagnostic metadata and may still be sensitive. Normal sensor aggregation does not ingest
this new schema. No upload, synchronization, rotation or deletion is automatic.

Capture configuration or append failure warns without changing an access decision. A denial before
an environment can be safely identified may have no trace entry; missing logs are not proof of no
attempt. Existing native hook definitions/trust and capture preference are unchanged by this feature.

The resolver opens resource path components without following symlinks and rejects non-regular and
hard-linked files. The local account and explicitly enrolled filesystem remain trusted: this is not
an OS sandbox or protection against a hostile process concurrently replacing enrollment metadata.
It does not intercept arbitrary shell/tools, grant native permissions, synchronize context, or erase
model memory. Strong access control still belongs to the host's sandbox and authorization boundary.
