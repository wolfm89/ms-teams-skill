# Regenerate with: vhs docs/demo.tape
# Requires VHS, ttyd and ffmpeg on PATH.
# The send is simulated: docs/demo-bin/teams.py prints a demo result and never
# calls Teams. The real command would post only after the user confirms.
Output docs/assets/demo.gif

Set Shell "bash"
Set Theme { "name": "ms-teams", "black": "#000000", "red": "#ffffff", "green": "#ffffff", "yellow": "#ffffff", "blue": "#ffffff", "magenta": "#ffffff", "cyan": "#ffffff", "white": "#ffffff", "brightBlack": "#ffffff", "brightRed": "#ffffff", "brightGreen": "#ffffff", "brightYellow": "#ffffff", "brightBlue": "#ffffff", "brightMagenta": "#ffffff", "brightCyan": "#ffffff", "brightWhite": "#ffffff", "background": "#000000", "foreground": "#ffffff", "selection": "#444444", "cursor": "#ffffff" }
Set FontFamily "DejaVu Sans Mono"
Set FontSize 17
Set Width 1400
Set Height 410
Set Padding 20
Set Margin 16
Set MarginFill "#000000"
Set BorderRadius 10
Set WindowBar Colorful
Set LetterSpacing 0
Set LineHeight 1.2
Set TypingSpeed 10ms
Set PlaybackSpeed 0.5
Set CursorBlink true

Hide
Type "export PATH=$PWD/docs/demo-bin:$PWD/skills/ms-teams/scripts:$PATH; export PS1='$ '; clear"
Enter
Show

Type "teams.py send --to self --subject 'Release v1.2.0' --importance high --html '<p><b>Deployment v1.2.0</b> is live 🚀 <a href=https://example.com/release-notes>Notes</a>. @[Ada Lovelace]</p>' --send"
Enter
Sleep 3s

