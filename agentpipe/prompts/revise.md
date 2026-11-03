Your pull request for **$key** came back from the pipeline. Round $round of $max_rounds;
when the rounds run out the task goes to a human, so fix the substance, not the symptom.

$feedback

$merge_note

Address every **blocking** finding. Do not paper over a finding (renaming a duplicate
instead of removing it, adding an assertion that restates the implementation, adding
a test that would pass with the code deleted). Nits are optional but cheap ones are
worth doing. If you are certain a finding is wrong, keep the code, and explain why in
`summary` with evidence (file:line, test name), since the reviewer will read it.

The task text, the standard and the git rules from your original instructions still
apply: run `$test_cmd`, commit without attribution, do not push, leave the tree clean.

Return the structured output as before.
