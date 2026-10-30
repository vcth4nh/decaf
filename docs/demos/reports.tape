Output reports.gif
Output reports.txt
Source common.tape
Show

Type "# Keep valid sources even when another archive fails"
Enter
Sleep 1s
Type "decaf samples -o out --no-maven --cpus 2 --memory 2g --config config.toml"
Sleep 1500ms
Enter
Wait+Screen /out\/decaf-report.json/
Sleep 8s
Type "echo Exit code: $?"
Sleep 1500ms
Enter
Sleep 4s

Hide
Ctrl+L
Sleep 500ms
Show
Type "# List failed archives and partial sources"
Enter
Sleep 1s
Type "decaf report out --problems"
Sleep 1500ms
Enter
Wait+Screen /1 flagged/
Sleep 10s

Hide
Ctrl+L
Sleep 500ms
Show
Type "# Inspect the compiler diagnostic and engine attempts"
Enter
Sleep 1s
Type "decaf report out --artifact bytecode.jar"
Sleep 1500ms
Enter
Wait+Screen /cfr class/
Sleep 12s
