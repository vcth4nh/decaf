Output checks.gif
Output checks.txt
Source common.tape
Show

Type "# Check decompiled Java and retry flagged classes"
Enter
Sleep 1s
Type "decaf bytecode.jar -o checked --no-maven --cpus 2 --memory 2g --config config.toml"
Sleep 1500ms
Enter
Wait+Screen /checked\/decaf-report.json/
Sleep 10s

Hide
Ctrl+L
Sleep 500ms
Show
Type "# This error remains after Vineflower and CFR both try"
Enter
Sleep 1s
Type "decaf report checked --artifact bytecode.jar"
Sleep 1500ms
Enter
Wait+Screen /cfr class/
Sleep 12s
