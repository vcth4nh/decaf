Output layouts.gif
Output layouts.txt
Source common.tape
Show

Type "# Process a folder, including the JAR nested in app.war"
Enter
Sleep 1s
Type "decaf libs -o mirror --no-maven --cpus 2 --memory 2g --config config.toml"
Sleep 1500ms
Enter
Wait+Screen /mirror\/decaf-report.json/
Sleep 8s
Type "find mirror -type f | sort"
Sleep 1500ms
Enter
Wait+Screen /mirror\/tools.jar\/demo\/Tool.java/
Sleep 10s

Hide
Ctrl+L
Sleep 500ms
Show
Type "# Merge the same batch into one package tree"
Enter
Sleep 1s
Type "decaf libs -o merged --merge --no-maven --cpus 2 --memory 2g --config config.toml"
Sleep 1500ms
Enter
Wait+Screen /merged\/decaf-report.json/
Sleep 8s
Type "find merged -type f | sort"
Sleep 1500ms
Enter
Wait+Screen /merged\/src\/demo\/Tool.java/
Sleep 10s
