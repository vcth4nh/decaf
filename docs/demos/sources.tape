Output sources.gif
Output sources.txt
Source common.tape

# Start with an empty XDG cache without touching the user's cached sources.
Type@1ms 'export XDG_CACHE_HOME="$PWD/.cache"; clear'
Enter
Sleep 500ms
Show

Type "# Recover the published sources from Maven"
Enter
Sleep 1s
Type "decaf slf4j-api-2.0.13.jar -o original --cpus 2 --memory 2g --config config.toml"
Sleep 1500ms
Enter
Wait+Screen /original\/decaf-report.json/
Sleep 10s

Hide
Ctrl+L
Sleep 500ms
Show
Type "# Repeat the run: the sources are now cached"
Enter
Sleep 1s
Type "decaf slf4j-api-2.0.13.jar -o cached --cpus 2 --memory 2g --config config.toml"
Sleep 1500ms
Enter
Wait+Screen /cached\/decaf-report.json/
Sleep 10s
