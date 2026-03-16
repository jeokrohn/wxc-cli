[![PyPI version](https://img.shields.io/pypi/v/wxc-cli.svg)](https://pypi.org/project/wxc-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/wxc-cli.svg)](https://pypi.org/project/wxc-cli/)

# wxc-cli
 
Auto-generated CLI for [`wxc_sdk`](https://pypi.org/project/wxc_sdk/) — every API endpoint is
a command, wired up at startup by introspecting `WebexSimpleApi`.
 
## Install
 
```bash
pip install wxc_cli
```
 
Or from source:
 
```bash
git clone ...
pip install -e .
```
 
## Auth
 
```bash
export WEBEX_ACCESS_TOKEN="your-token-here"
# or pass it per-command:
wxc_cli --token YOUR_TOKEN people list
```
 
## Usage
 
```
wxc-cli --help                          # list all command groups
wxc-cli people --help                   # list people commands
wxc-cli people list --display-name Bob  # filter by name
wxc-cli people list --output json       # JSON output
wxc-cli people details --person-id Y2l… # single person
wxc-cli telephony calls list-calls      # list active calls
wxc-cli telephony callqueue --help      # nested sub-APIs work too
```
 
### Output formats
 
Every command accepts `--output table` (default), `--output json`, or `--output csv`.
 
```bash
wxc_cli rooms list --output json | jq '.[].title'
```
 
### Pydantic model inputs
 
Methods that take a Pydantic model (e.g. `create`, `update`) accept a `--<param>-json` flag:
 
```bash
wxc_cli people create \
  --settings-json '{"emails":["alice@example.com"],"display_name":"Alice"}'
```
 
## How it works
 
`cli.py` introspects `WebexSimpleApi` at import time:
 
1. Each sub-API attribute (`.people`, `.telephony`, `.rooms`, …) becomes a **Typer command group**.
2. Each public method becomes a **command** inside that group.
3. The method's type-annotated parameters become **`--option` flags** — scalars directly, Pydantic models via `--<n>-json`.
4. Generator return types (list endpoints) are consumed fully and rendered as a Rich table or JSON array.
5. Nested sub-APIs (e.g. `telephony.calls`, `telephony.callqueue`) recurse into sub-groups automatically.
 
New endpoints added to `wxc_sdk` appear in the CLI with **zero maintenance**.
 
## Shell completion
 
```bash
wxc_cli --install-completion   # bash / zsh / fish
```