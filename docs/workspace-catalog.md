# Authenticated workspace catalog

One registry Worker serves enrolled readers from its private D1 binding. The operator controls publication through D1. Each client generates a distinct 256-bit reader credential. D1 stores its SHA-256, client label and workspace. Removing a reader row revokes that credential. Use TLS and keep local profiles owner-only. Each Worker response requests private, no-store caching. Disable public assets, preview URLs and invocation logs for this service.

## Released v0.13.0 client

Install the reviewed distribution and run the following on the intended client. Supply the HTTPS origin and workspace identifier obtained from your workspace operator.

```sh
skills-audit workspace catalog enroll --origin "$REGISTRY_ORIGIN" --workspace "$WORKSPACE"
skills-audit workspace catalog discover
skills-audit workspace catalog resolve --out private-catalog.json
skills-audit workspace catalog resolve --pin "$SHA256"
skills-audit workspace catalog plan project-a --pin "$SHA256" --plan-out reviewed.json
```

After enrollment, bind the printed credential **digest** to the intended host and workspace over an existing authenticated operator channel. Discovery requires this server-side binding. The default credential file is under the client's private configuration directory. Select another identity with `catalog --profile FILE`. Clients check their enrolled workspace on every resolution, fetch a fixed content address, verify the complete compressed payload, materialize and verify package hashes, required files and Skill metadata, and return the lint attestation and per-name dispositions. An old digest remains retrievable after a promotion. Private catalog exports contain the reviewed source bytes.

Register the target project and select published source IDs in its baseline using [workspace management](workspace-management.md). Catalog planning preserves explicit exclusions and captures locally bound project overrides. It rejects conflicting declared baseline provenance. Apply and native verification follow the existing reviewed-plan workflow. Registry availability and native client discovery are separate observations.

## Account directory and environment subscriptions (development version)

The production v0.13.0 service supports immutable releases. The development client also supports a directory-enabled host. Each workspace is an account directory. All enrolled environments can browse its registrations. A fresh environment starts with zero subscriptions. The directory also works before the account publishes its first release.

| Record | Identity and scope | Meaning |
| --- | --- | --- |
| Skill | Portable UUID | Identifies the logical Skill across versions. An owner can generate one offline with `catalog new-id` and place it in the project's `skill_ids` map. New local managed entries allocate a project-local UUID when none is declared. |
| Account registration | Workspace and Skill UUID | An account owner explicitly adds the Skill to the list. Registration can precede release. The account display name can change independently of the UUID. |
| Released entry | Workspace, release digest and Skill UUID | Identifies the native name for that revision. For a verified Git package the entry records the repository, commit and relative `SKILL.md` path. Unknown provenance is returned as null. |
| Subscription | Enrolled environment UUID and account Skill UUID | Records this environment's opt-in. Credential rotation can bind a replacement credential to the same environment UUID. Replacement environments get fresh UUIDs. |
| Native observation | Environment, project, platform, release digest, Skill UUID and scan time | Records what the client observed in that project. A successful Codex scan can show an enabled name once, multiple times, or absent. An unscanned entry is unverified. |

Account registration is an explicit owner action. Generate a UUID offline, keep it in the source project's declaration, then use an owner profile to register its display name and optional repository-relative `SKILL.md` path. This sends metadata only. A release publisher independently links the same UUID to a verified entry in an immutable release. Directory responses distinguish the owner-declared path from the pinned repository, revision and entry file. Unregistering retires the listing and clears its environment subscriptions. Historical packages that were previously distributed retain their original contents.

```sh
skills-audit workspace catalog new-id
skills-audit workspace catalog --profile "$OWNER_PROFILE" register "$SKILL_UUID" --name review --source-file skills/review/SKILL.md
```

Run the development CLI on the enrolled target. `list` shows the whole account directory. `select` displays checkboxes and accepts a number to toggle. For scripts, use `subscribe UUID` or `unsubscribe UUID`. Changing a subscription records intent immediately. Native links are reconciled through the existing reviewed plan and apply flow.

```sh
skills-audit workspace catalog list
skills-audit workspace catalog select
skills-audit workspace catalog plan project-a --pin "$SHA256" --plan-out reviewed.json
skills-audit workspace apply reviewed.json --approve-plan-id "$PLAN_ID"
skills-audit workspace catalog observe project-a --pin "$SHA256"
skills-audit workspace catalog list --project project-a --pin "$SHA256"
```

The directory host binds the authenticated reader to its environment. The client runs catalog planning against locally enrolled projects so a controller's selection cannot be used for a different SSH target. Apply and resume check the reader binding and selected registrations again. An explicit project exclusion continues to win. A selected parent pack that exposes an unselected nested entry produces a blocked plan with the nested name.

Project-owned local sources remain explicit. A local baseline or override can use a temporary directory while the account subscription is off. An entirely local project can use the ordinary workspace commands offline. Its source bytes and unregistered names stay out of directory reports. For a draft of a registered Skill, record the same UUID under its alias in the project's `skill_ids`. An uploaded observation contains the registered UUID, native state and optional entry-file digest. It never transmits the local path or body. Matching entry bytes establish entry-level evidence. Whole-package integrity comes from release verification.

After applying an unsubscribe, the project's managed installation is disabled. To use a draft, bind its local source and declare the Skill's project override, then plan and apply for that project. A changed draft produces one `enable-from-source` action that keeps the installation identity and publishes the reviewed draft directly. Later edits require another plan and apply. The environment's cloud subscription stays off. An independently installed native entry at the target remains subject to the existing ownership and conflict checks.

The directory retains historical observations separately from subscription changes. An old scan keeps its timestamp after a checkbox changes. A failed native inspection leaves the last successful receipt available. Codex is the supported observation reporter in this iteration.

## Publishing immutable v1 releases

Use an isolated Worker and a dedicated D1 database. Copy the [example configuration](../services/workspace-registry/wrangler.example.jsonc) into private operator state, set the verified account and database IDs and use the supplied [Worker](../services/workspace-registry/worker.mjs). Apply the [schema](../services/workspace-registry/schema.sql) once to the empty database. Bind a verified enrollment by inserting the client's digest, workspace and unique label into `readers`. Never upload client profiles.

The catalog has one disposition per reviewed name. Only `available` entries can point to a source. Upstream-only items remain on their official update channel. The operator reviews ownership, redistribution scope, package selection, project overrides and the dependency contract before assembling the portable release. The lint attestation binds a private review by SHA-256. Keep raw evidence outside the registry. Use the installed CLI's `workspace catalog lint FILE` to validate the catalog.

The [operator tool](../services/workspace-registry/prepare.py) produces private SQL for the pinned database. Run it with the reviewed CLI installed. Use the selected Wrangler profile and an explicit configuration with every remote operation.

```sh
python prepare.py --out stage.sql stage private-catalog.json
wrangler d1 execute "$DB" --config "$CONFIG" --remote --file stage.sql
python prepare.py --out promote.sql promote "$WORKSPACE" "$SHA256" "$CURRENT_GENERATION"
wrangler d1 execute "$DB" --config "$CONFIG" --remote --file promote.sql
```

The initial generation is zero. Staging validates the release, hashes its deterministic compressed bytes, inserts chunks and seals their count and total size. The Worker only serves content with a promotion record. The promotion insert advances the pointer atomically if its expected generation is current. Roll back by promoting a retained digest at the new generation. After an uncertain write, inspect the named release and pointer history before retrying. Verify anonymous, invalid-token and cross-workspace denial before publishing private material.

The per-name `findings` codes retain observed native conflicts and source limitations. A reviewed canonical package can coexist with an observed local override. Its source-tree digest covers file contents, paths and permissions. The portable packet also includes declared required files. A catalog review can be repeated without executing Skill code.
