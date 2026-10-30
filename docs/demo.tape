Output demo.gif

Set Shell "bash"
Set FontFamily "DejaVu Sans Mono"
Set FontSize 18
Set Width 1200
Set Height 620
Set Theme "Catppuccin Mocha"
Set Padding 24
Set TypingSpeed 25ms
Set PlaybackSpeed 0.35
Set WaitTimeout 60s

Hide
Type "export PS1='$ '; clear"
Enter
Sleep 500ms
Show

Type "decaf app.war -o out --no-maven --no-fallback --cpus 2 --memory 2g --config config.toml"
Enter
Wait+Screen /Completed in/
Sleep 4s

Type "decaf report out --memory"
Enter
Sleep 4s
