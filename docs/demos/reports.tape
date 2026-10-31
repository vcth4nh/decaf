Output reports.gif
Output reports.txt
Source common.tape
Show

Type "decaf samples --no-maven"
Sleep 500ms
Enter
Wait+Screen /decaf-out\/decaf-report.json/
Sleep 2s

Hide
Ctrl+L
Sleep 500ms
Show
Type "decaf report decaf-out --problems"
Sleep 500ms
Enter
Wait+Screen /1 flagged/
Sleep 2s

Hide
Ctrl+L
Sleep 500ms
Show
Type "decaf report decaf-out --artifact bytecode.jar"
Sleep 500ms
Enter
Wait+Screen /cfr class/
Sleep 5s
