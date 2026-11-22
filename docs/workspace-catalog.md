# Authenticated workspace catalog

One registry Worker serves enrolled readers from its private D1 binding. The operator controls publication through D1. Each client generates a distinct 256-bit reader credential. D1 stores its SHA-256, client label and workspace. Removing a reader row revokes that credential. Use TLS and keep local profiles owner-only. Each Worker response requests private, no-store caching. Disable public assets, preview URLs and invocation logs for this service.

## Client

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

## Publisher

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
