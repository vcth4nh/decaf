Output demo.gif

Set Shell "bash"
Set FontFamily "DejaVu Sans Mono"
Set FontSize 18
Set Width 1200
Set Height 760
Set Theme "Catppuccin Mocha"
Set Padding 24
Set TypingSpeed 25ms
Set PlaybackSpeed 1
Set WaitTimeout 60s

Hide
Type "export PS1='$ '; clear"
Enter
Sleep 500ms
Show

Type "# Process valid and broken archives together"
Enter
Type "decaf samples -o out --no-maven --cpus 2 --memory 2g --config config.toml"
Enter
Wait+Screen /Completed with failures/
Sleep 1s
Type "echo Exit code: $?"
Enter
Sleep 5s

Hide
Ctrl+L
Sleep 500ms
Show
Type "# Inspect the unreadable archive and partial source"
Enter
Type "decaf report out --problems"
Enter
Wait+Screen /1 flagged/
Sleep 5s

Hide
Ctrl+L
Sleep 500ms
Show
Type "# Inspect javac diagnostics and both engine attempts"
Enter
Type "decaf report out --artifact bytecode.jar"
Enter
Wait+Screen /cfr class/
Sleep 8s
