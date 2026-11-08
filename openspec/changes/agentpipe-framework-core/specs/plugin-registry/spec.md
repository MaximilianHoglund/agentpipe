## ADDED Requirements

### Requirement: Adapters are registered by kind and name
Every adapter SHALL register under a port kind and a name (e.g. `tracker/github`). Config
selects adapters by name. An unknown name SHALL fail at load with the list of known names for
that kind.

#### Scenario: Unknown adapter
- **WHEN** config selects `tracker = "jria"`
- **THEN** loading fails with "unknown tracker 'jria'; known: github, gitlab, jira, local, none"

### Requirement: Adapters load from three places
Adapters SHALL be found among the built-ins, in Python entry points (`agentpipe.adapters`), and
in a user plugin directory (default `~/.config/agentpipe/plugins/`, and `.agentpipe/plugins/`
in the repo). A user or repo adapter with a built-in's name SHALL override it, and the load log
SHALL say so.

#### Scenario: A user's own tracker
- **WHEN** `~/.config/agentpipe/plugins/linear_tracker.py` registers `tracker/linear`
- **THEN** a target with `tracker = "linear"` uses it without any change to agentpipe

### Requirement: Adapters declare their contract version and options
An adapter SHALL declare the port contract version it implements and the config options it
reads, with types and defaults. A contract version the core does not support SHALL fail at load.

#### Scenario: Outdated plugin
- **WHEN** a plugin implements Tracker contract 1 and the core requires 2
- **THEN** loading fails naming the plugin, both versions and the changelog entry
