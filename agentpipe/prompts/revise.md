Your pull request for **$key** came back from the pipeline. Round $round of $max_rounds;
when the rounds run out the task goes to a human, so fix the substance, not the symptom.

$feedback

$merge_note

Treat each finding as a sample, not a full list. The reviewer probes a few spots; when it found
a rule no test catches, the rest of your diff likely has more of the same kind. Fix the finding,
then look for and fix the same kind of gap elsewhere in your change, so the next round doesn't
find it. Most PRs that went back a second time were rejected for a new gap of the same kind.

$self_check

Address every **blocking** finding. Do not paper over a finding (renaming a duplicate
instead of removing it, adding an assertion that restates the implementation, adding
a test that would pass with the code deleted). Nits are optional but cheap ones are
worth doing. If you are certain a finding is wrong, keep the code, and explain why in
`summary` with evidence (file:line, test name), since the reviewer will read it.

The task text, the standard and the git rules from your original instructions still
apply. Before you finish, run $test_cmd. Commit without attribution, do not push, and leave
the tree clean.

Return the structured output as before.
