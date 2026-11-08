## ADDED Requirements

### Requirement: Scaffold a working skeleton
`agentpipe adapter new <kind> <name>` SHALL create a plugin in the user plugin dir (or the
repo's with `--repo`) containing a class with every port method raising `NotImplementedError`
with a docstring from the port, an option schema, a contract-test entry and a README stub. The
new adapter SHALL load and be listed by `agentpipe adapter list`.

#### Scenario: New tracker
- **WHEN** a user runs `agentpipe adapter new tracker linear`
- **THEN** `agentpipe adapter list` shows `tracker/linear (user, contract v<n>)` and its contract run reports every method as not implemented
