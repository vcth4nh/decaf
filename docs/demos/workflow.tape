Output workflow.gif
Output workflow.txt
Source common.tape
Show

Type "decaf demo.jar"
Sleep 500ms
Enter
Wait+Screen /decaf-out\/decaf-report.json/
Sleep 2s

Hide
Ctrl+L
Sleep 500ms
Show
Type "decaf report decaf-out --artifact demo.jar"
Sleep 500ms
Enter
Wait+Screen /cfr class/
Sleep 5s
