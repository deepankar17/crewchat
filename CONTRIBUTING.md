# Contributing

Thanks for helping. crewchat is deliberately small: one file, standard library only, Python 3.9+.

## Ground rules

- **No dependencies.** If a change needs a package, it probably belongs in a separate tool.
- **One file.** `crewchat.py` must keep working when copied on its own to another machine.
- **The server binds to 127.0.0.1.** Changes that make it listen elsewhere will not be merged;
  remote access goes through a private network.
- **Tests come with the change.** The suite starts a real server; add a test that fails without
  your change.

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

## Trying a change by hand

Use a throwaway home so you do not touch your real chat:

```bash
export CREWCHAT_HOME=/tmp/crewchat-dev
python3 crewchat.py setup --agents a,b --project Dev --port 8799
python3 crewchat.py serve
```

## Reporting bugs

Open an issue with what you ran, what you expected and what happened. For anything
security-sensitive, see [SECURITY.md](SECURITY.md) instead.
