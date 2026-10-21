"""Hidden credential entry; token exists only in the child process environment."""

import getpass, os, sys
from pathlib import Path

root = Path(__file__).resolve().parent
offline = "--model" in sys.argv and sys.argv[sys.argv.index("--model")+1] == "off" and "--aux-model" in sys.argv and sys.argv[sys.argv.index("--aux-model")+1] == "off"
offline = offline or ("--model" in sys.argv and sys.argv[sys.argv.index("--model")+1] == "off" and all(k in sys.argv and sys.argv[sys.argv.index(k)+1]=="off" for k in ["--summary-model","--memory-model"]))
def opt(name, default):
    return sys.argv[sys.argv.index(name)+1] if name in sys.argv else default
aux = opt('--aux-model', None)
needs_api = (opt('--model','qwen3.8-max') != 'off' or (aux or opt('--summary-model','qwen3.7-plus')) != 'off' or
             (opt('--memory','read-write') == 'read-write' and opt('--memory-content','cards') == 'cards' and (aux or opt('--memory-model','qwen3.7-plus')) != 'off'))
offline = not needs_api
if not offline and "--help" not in sys.argv and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
    if not sys.stdin.isatty():
        raise SystemExit(
            "Use an interactive terminal or set ANTHROPIC_AUTH_TOKEN in the process environment"
        )
    os.environ["ANTHROPIC_AUTH_TOKEN"] = getpass.getpass("API token (not saved): ")
if not offline and '--help' not in sys.argv:
    token = os.environ.get('ANTHROPIC_AUTH_TOKEN', '')
    if not token or any(not ('!' <= ch <= '~') or ch in "\"'`" for ch in token):
        raise SystemExit('API credential invalid: paste only the token, without whitespace, Chinese punctuation, quotes or command text. Token is not displayed; no search started.')
os.chdir(root)
os.execvp("node", ["node", "dist/src/cli.js", *sys.argv[1:]])
