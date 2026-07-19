# Alternatives and escape hatches

skills-auditor is useful when local agent skill roots need repeatable inspection and safe sync planning. It is not always the smallest tool for the job.

## Manual shell scripts

Best when:

- You have one machine.
- You need a one-time copy.
- You already know the canonical source path.

Weakness:

- Shell scripts usually do not explain duplicate names, drift, platform profile rules, or dry-run sync plans across multiple roots.

## Package-manager installs

Best when:

- The skill source is packaged.
- The target runtime supports package install directly.

Weakness:

- Package installs do not inspect existing local root drift or broken symlinks.

## General agent harnesses

Best when:

- You are orchestrating agents rather than maintaining skill folders.
- The core problem is task execution, not source hygiene.

Weakness:

- They usually do not provide skill-folder dedupe, symlink health, metadata repair, or discovery-profile sync.

## Use skills-auditor when

- A team has several agent tools using overlapping skill concepts.
- A local root has a mixture of copied folders and symlinks.
- You need dry-run evidence before changing filesystem state.
- You want a repeatable audit gate in CI.
- You need apply to consume the exact source and target state that was reviewed.

## Escape hatch

If the audit output reveals that a single root and a single canonical source are enough, keep the workflow simple and use a direct copy or symlink script.
