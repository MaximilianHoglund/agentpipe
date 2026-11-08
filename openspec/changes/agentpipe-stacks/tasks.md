## 1. Shared evidence

- [ ] 1.1 Define the mutation evidence record and switch the reviewer prompt and verdict rule to it
- [ ] 1.2 Convert cargo-mutants output in the rust stack and check arpg reviews get the same mutants

## 2. dotnet

- [ ] 2.1 Add the `dotnet` Stack defaults (gate, format, preflight, overlap-ignore, prompt notes) with a `solution` option
- [ ] 2.2 Add per-slot build-process isolation and verify two parallel gates on one solution don't lock each other
- [ ] 2.3 Add Stryker.NET mutation scoped to changed files, with the JSON report parsed into evidence records
- [ ] 2.4 Run the Stack contract suite and a small sample solution end to end with stubbed agents

## 3. Presets

- [ ] 3.1 Add the `generic` Stack and its missing-gate error
- [ ] 3.2 Add the `node` and `python` presets and run them through the Stack contract suite
