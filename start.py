"""Hidden credential entry; token exists only in the child process environment."""

import getpass, os, sys
from pathlib import Path

root = Path(__file__).resolve().parent
offline = "--model" in sys.argv and sys.argv[sys.argv.index("--model")+1] == "off" and "--aux-model" in sys.argv and sys.argv[sys.argv.index("--aux-model")+1] == "off"
if not offline and "--help" not in sys.argv and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
    if not sys.stdin.isatty():
        raise SystemExit(
            "Use an interactive terminal or set ANTHROPIC_AUTH_TOKEN in the process environment"
        )
    os.environ["ANTHROPIC_AUTH_TOKEN"] = getpass.getpass("API token (not saved): ")
os.chdir(root)
os.execvp("node", ["node", "dist/src/cli.js", *sys.argv[1:]])
