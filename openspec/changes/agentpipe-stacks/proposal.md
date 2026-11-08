## Why

Max's work codebase is .NET, his friends use other stacks, and today the only stack knowledge
in agentpipe is Rust's (slot target dirs, workspace-crate cleanup, cargo-mutants). Each stack
has its own parallel-build hazards, formatter and mutation tool; a user should get sensible
defaults by naming the stack and override only what differs.

## What Changes

- A `dotnet` Stack: `dotnet test` gate, `dotnet format` formatter, Stryker.NET mutation scoped
  to the change request's changed files, per-slot isolation for MSBuild node reuse and the
  build server, default overlap-ignore globs, and agent prompt notes.
- A `generic` Stack: no defaults beyond what the target sets, for any language.
- Stack presets for `node` and `python` as small examples of the authoring path.
- Mutation output parsed into one evidence format (file, line, mutant, status) so the reviewer
  prompt is the same whichever tool produced it.

## Capabilities

### New Capabilities
- `dotnet-stack`: the .NET defaults, isolation and mutation evidence.
- `stack-presets`: the generic, node and python presets and the shared mutation evidence format.

### Modified Capabilities

## Impact

- New adapters under `agentpipe/adapters/stacks/`; the reviewer prompt reads the shared
  evidence format instead of cargo-mutants' `MISSED` lines.
- Depends on `agentpipe-framework-core`; verified with `agentpipe-integration-kit`'s Stack suite.
