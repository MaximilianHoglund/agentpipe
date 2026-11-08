## Context

After `agentpipe-framework-core` the ports are protocols with a registry that loads user
plugins. What is missing is a way for someone other than Max to build and trust an adapter.

## Goals / Non-Goals

**Goals:**
- The contract suites are the definition of "works": built-ins and user plugins are held to
  the same bar.
- A user who knows their tools, not agentpipe's internals, gets a working setup by talking to
  an agent.

**Non-Goals:**
- A plugin marketplace or hosted index. Sharing is by git repo or copying the plugin file.
- Supporting agent runtimes other than Claude Code for the setup session.

## Decisions

- **Contract suites are pytest modules parametrized by adapter**, shipped inside the package so
  `agentpipe adapter test` runs them without the source tree.
- **Fixtures at the transport layer** (an injectable `run`/HTTP client, already the pattern in
  `proc.run` and `GitHub(run=...)`), recorded as JSON. Simpler than vcrpy for CLI-based
  adapters, which are subprocess calls, not HTTP.
- **Setup is a prompt plus a Claude Code session**, launched with the scaffold and contract
  commands allowed and no deny on editing the plugin dir. The guide is a prompt file in the
  package, overridable per user, so the walkthrough improves without code changes.
- **Sandbox resources are tagged and cleaned** by run id so live contract runs are safe on a
  real tracker project.

## Risks / Trade-offs

- [Contract suites miss behaviour the core relies on] → every bug found in an adapter adds a
  contract case, not just an adapter test.
- [A setup agent writes a weak adapter that passes thin tests] → the suites cover failure
  paths and capabilities, and `doctor` re-runs a read-only smoke against the live tool.
- [Live contract runs on a work tracker create noise] → require a sandbox project or a
  dedicated label, and refuse `--live` on a target whose config marks it production.
