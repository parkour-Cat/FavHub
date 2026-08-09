# example-improve

An example agent skill that audits any codebase and writes implementation plans
for other agents to execute.

The idea: use the most capable model for the part where judgement compounds —
reading the codebase, deciding what is worth doing, writing the specification —
and hand execution to cheaper models. The skill never implements anything
itself. The plan is the product.

```
you          →  /example-improve            (expensive model, advises)
the plan     →  plans/NNNN-topic.md         (plain markdown, portable)
execution    →  any agent or any human      (cheap model, follows)
```

## Install

```bash
npm install -g example-improve
example-improve init
```

Works in any agent that reads plain markdown plans, so nothing here is tied to
one runtime.

## What it does

1. **Reads** the repository — layout, dependencies, tests, and the conventions
   already in use, rather than the conventions it would have chosen.
2. **Judges** what is worth changing, and says why. A finding without a reason
   is a preference wearing a lab coat.
3. **Writes** a plan as ordered, checkable steps with the files each one
   touches, so progress is visible without re-reading the diff.

It stops there on purpose. Writing the plan and executing it are different
jobs, and the second one does not need the first one's price.

## Output

Plans land in `plans/` as ordinary markdown:

```markdown
# Plan: replace the ad-hoc cache with a bounded one

## 1. Add the eviction policy
- [ ] src/cache.py — size bound and eviction order
- [ ] tests/test_cache.py — eviction under pressure

## 2. Move callers over
- [ ] src/api.py — construct with an explicit bound
```

Nothing in the format is special. A plan is a file, and any agent or person can
pick one up in the middle.

## Configuration

`example-improve.toml`, all keys optional:

```toml
model = "your-preferred-model"
plans_dir = "plans"
max_files = 400
ignore = ["vendor/", "generated/"]
```

## Why plans rather than patches

A patch is an answer to a question nobody wrote down. Six months later the diff
is still there and the reason is not. A plan keeps the reasoning next to the
work, survives being handed to someone else, and can be argued with before any
code moves.

It also fails better. A patch that is wrong has already been applied; a plan
that is wrong is a paragraph somebody disagrees with.

## Limitations

- It reads the repository as it is, so a codebase with no tests gets a plan with
  no test steps unless you ask for them.
- Very large repositories are sampled rather than read whole; `max_files` is the
  knob and the plan says when it was hit.
- It does not run anything. No builds, no migrations, no deploys.

## License

MIT
