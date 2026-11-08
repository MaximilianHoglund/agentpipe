## ADDED Requirements

### Requirement: An agent walks the user through setup
`agentpipe setup` SHALL start an interactive Claude Code session in the repo with a setup guide
that covers, in order: task sources, tracker (or none/local), code host and whether git has a
remote, stack and gate, approval mode and identities, accounts and limits. For each stage it
SHALL offer the built-in adapters that fit and explain the trade-offs in a sentence each.

#### Scenario: All built-ins
- **WHEN** the user picks GitHub, OpenSpec and the rust stack
- **THEN** the session writes the config, runs `agentpipe doctor`, and ends with the command to start the target

### Requirement: Missing integrations are built in the session
When no adapter fits, the setup agent SHALL scaffold one, implement it against the tool's
documentation, run its contract suite against a sandbox the user provides (recording fixtures),
and only write it into the config once the suite passes. It SHALL NOT write credentials into
any file; it asks the user to set the environment variable or keyring entry.

#### Scenario: A tracker agentpipe doesn't ship
- **WHEN** the user's team uses Azure Boards
- **THEN** the session produces `tracker/azure-boards` in the plugin dir, its contract suite passes with recorded fixtures, and the target uses it

### Requirement: Setup is resumable and repeatable
The setup session SHALL record its decisions in a setup notes file next to the config, so
running `agentpipe setup` again offers to change one stage without redoing the rest.

#### Scenario: Switching tracker later
- **WHEN** a user who chose `local` runs setup again and picks GitLab
- **THEN** only the tracker stage is revisited, and the session offers `agentpipe export --to gitlab`
