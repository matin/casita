# Contributing

Casita is used in a junior-engineer interview loop. There is no curated
good-first-issue list on purpose.

Pick something you think makes Casita better: a fix, a test, a refactor, a
feature, docs, polish, or a simplification. In your pull request description,
tell us why you chose it. The choice is part of the signal.

Please keep the demo path credentials-free:

- `uv run casita demo` should work from the committed fixture.
- The demo should not require GCS, Firebase, Vertex, browser login, or paid API
  calls.
- Private data, private project names, phone numbers, and one-off operational
  details should stay out of the public tree.

No support is implied. This is a personal-use codebase published as an
interview instrument, not a maintained product.

Before opening a pull request, run:

```bash
make check
```
