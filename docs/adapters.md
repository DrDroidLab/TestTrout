# Writing an adapter

Three extension points. Each is a protocol plus an entry point, so an adapter
can ship as your own package — you do not need to fork this repository or send
us a pull request to support your stack.

| What | Protocol | Entry point group |
|---|---|---|
| Framework | `testtrout/analysis/frameworks/base.py` | `testtrout.frameworks` |
| Auth provider | `testtrout/deployment/auth/base.py` *(phase 2)* | `testtrout.auth` |
| Test emitter | `testtrout/authoring/base.py` | `testtrout.emitters` |

## Framework adapter

Answers one question: *what can a user navigate to, and what server code backs
it?* Implement four methods — `matches`, `screens`, `endpoints`,
`server_actions` — and return domain objects.

```python
# my_adapter/svelte.py
from typing import ClassVar


class SvelteKitAdapter:
    id: ClassVar[str] = "sveltekit"

    def matches(self, context) -> bool:
        return "@sveltejs/kit" in context.dependencies

    def screens(self, files, context, allocator): ...  # return (list[Screen], list[ScanWarning])

    def endpoints(self, files, context, allocator):
        return []

    def server_actions(self, files, context, allocator):
        return []
```

```toml
# pyproject.toml
[project.entry-points."testtrout.frameworks"]
sveltekit = "my_adapter.svelte:SvelteKitAdapter"
```

Install it alongside `testtrout` and `trout scan` picks it up.

## Rules

**Never call a model.** Adapters live inside the deterministic core. See
[ADR 3](adr/0003-deterministic-core.md).

**Never raise on malformed input.** A broken project should produce a partial
result and a `ScanWarning`, not a stack trace. The scanner already isolates
adapter failures, but relying on that is not the same as handling it.

**Leave `Screen.reaches` empty.** The scanner fills it from the module graph;
reachability is framework-independent and you should not duplicate it.

**Emit ids from durable properties.** Use `IdAllocator` and derive ids from
route paths or symbol names — never from file position, or every id shifts when
someone adds an import.

**Return what you found.** If you cannot resolve a route path, emit a warning
naming the file. A missing surface the user knows about beats a guessed one
they do not.

## Testing yours

Add a fixture app under `examples/` and a golden file:

```bash
QA_UPDATE_GOLDEN=1 pytest tests/golden
git diff    # read it — this is the review
```
