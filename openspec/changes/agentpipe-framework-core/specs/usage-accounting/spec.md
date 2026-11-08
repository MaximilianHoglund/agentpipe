## ADDED Requirements

### Requirement: Usage is accounted by the configured mode
The AgentRuntime SHALL report each run's usage. Accounting SHALL be one of `windows` (the
subscription's five-hour and seven-day utilization, today's behaviour), `budget` (cost per run
summed against a daily and a weekly cap), or `none`.

#### Scenario: Windows mode
- **WHEN** accounting is `windows` and a run reports five-hour utilization at the throttle
- **THEN** nothing new starts until that window resets, as today

#### Scenario: Budget mode
- **WHEN** accounting is `budget` with a daily cap of 50 and today's runs total 50
- **THEN** nothing new starts until the next day, and running jobs finish

### Requirement: Accounts are per machine or per target
The Claude account a run uses SHALL be set by config (binary, config dir or credentials env),
per machine and overridable per target, so a work target runs on the work account and a
personal target on the personal one.

#### Scenario: Work and personal targets on one machine
- **WHEN** target `work-api` sets the work account and `arpg` does not
- **THEN** their agents run under different accounts, and each account's usage is throttled separately
