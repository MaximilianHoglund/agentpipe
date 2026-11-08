## ADDED Requirements

### Requirement: .NET defaults
The `dotnet` Stack SHALL default the gate to `dotnet test` on the configured solution, the
formatter to `dotnet format`, preflight to `dotnet --version`, and overlap-ignore to `*.sln`,
`*.slnx`, `**/*.csproj`, `Directory.Packages.props` and `Directory.Build.props`. Each SHALL be
overridable by the target.

#### Scenario: Only the solution named
- **WHEN** a target sets `stack = "dotnet"` and `solution = "src/Api.sln"`
- **THEN** the gate runs `dotnet test src/Api.sln` and additive conflicts are followed by `dotnet format src/Api.sln`

### Requirement: Parallel slots don't share build processes
Each slot's agent, gate and mutation runs SHALL get an environment that stops MSBuild node reuse
and the shared build server from holding files across worktrees, and SHALL leave the NuGet
package cache shared.

#### Scenario: Two gates at once
- **WHEN** two slots run `dotnet test` on different worktrees at the same time
- **THEN** neither fails on a file locked by the other's build processes

### Requirement: Mutation evidence from Stryker.NET
When mutation is on, the Stack SHALL run Stryker.NET limited to files the change request
changed, parse its JSON report, and hand the reviewer the survived mutants in the shared
evidence format.

#### Scenario: A survived mutant
- **WHEN** Stryker reports a survived mutant on a changed line
- **THEN** the reviewer prompt lists it with file, line, mutation and status `survived`
