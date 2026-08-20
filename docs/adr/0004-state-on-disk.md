# 4. State is YAML on disk, not a database

**Status:** accepted

## Context

The tool accumulates real state: configuration, the surface map, approved
scenarios, mock contracts, run history. That could live in SQLite, in a hosted
service, or in files.

## Decision

Everything lives in a `.trout/` directory as human-readable YAML. Configuration,
surfaces, intent, scenarios, and contracts are committed to the repository. Run
outputs and evidence are not.

## Consequences

The user owns their test suite. They can read it, hand-edit it, diff it, and
review a change to it in a pull request — which is exactly what you want when
the change was proposed by a model.

The CLI, the web interface, and any agent driving the tool read and write the
same files, so there is never a question of which view is authoritative and no
synchronisation to get wrong.

Nothing is hosted, so nothing is sent anywhere and there is no account to
create.

The cost is that we give up querying and concurrent-write safety. If run
history outgrows this, put *runs* in SQLite and leave the committed artifacts
as files. Do not migrate the whole thing.
