You are checking that this machine's tools work for the agents of an unattended
pipeline. Do nothing else: don't read, change or fix anything.

Run each command below exactly as written, from the current directory, once with each
of these tools: $shells. Make one tool call per command per tool, even if a call
fails or is denied. Don't work around a failure.

$commands

Then return one result per command per tool: the tool, the command exactly as listed,
its exit code (use -1 if the call was denied or could not run), and the first line or
two of its output or error. A tool that shows no exit code shows one only on failure:
a call that ran and completed without a reported error or exit code exited 0.
