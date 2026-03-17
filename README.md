[![PyPI version](https://img.shields.io/pypi/v/wxc-cli.svg)](https://pypi.org/project/wxc-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/wxc-cli.svg)](https://pypi.org/project/wxc-cli/)

# wxc-cli

Auto-generated CLI for [`wxc_sdk`](https://pypi.org/project/wxc_sdk/) — every API endpoint is
a command, wired up at startup by introspecting `WebexSimpleApi`.

## Install

```bash
pip install wxc-cli
```

The tool does not work with Python 3.14. To install in a venv with a different Python version:

```bash
pipx install wxc-cli --python python3.13
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
wxc-cli --token YOUR_TOKEN people list
```

## Usage

```
wxc-cli --help                          # list all command groups
wxc-cli people --help                   # list people commands
wxc-cli people list --display-name Bob  # filter by name
wxc-cli people list --output json       # JSON output
wxc-cli people details --person-id Y2l… # single person
wxc-cli telephony callqueue --help      # nested sub-APIs work too
```

The [`wxc_sdk` supported endpoint list](https://wxc-sdk.readthedocs.io/en/latest/user/method_ref.html)  
serves as a reference for the available commands. 

If for example the `wxc_sdk` reference has an endpoint `api.person_settings.forwarding.read` then `wxc-cli` 
supports `wxc-cli person-settings forwarding read` and
`wxc-cli person-settings forwarding read --help` gives you an overview of the supported parameters. 

The endpoint names in the 
[`wxc_sdk` supported endpoint list](https://wxc-sdk.readthedocs.io/en/latest/user/method_ref.html) are 
links to the actual function definitions for the respective endpoint. 

For example, 
[this](https://wxc-sdk.readthedocs.io/en/latest/apidoc/wxc_sdk.person_settings.forwarding.html#wxc_sdk.person_settings.forwarding.PersonForwardingApi.read) is the 
documentation for the `api.person_settings.forwarding.read` endpoint which helps to understand the CLI parameters. 

Note: The SDK uses snake-case names for endpoints and the CLI kebab-case names for commands.

### Output formats

Every command accepts `--output table` (default), `--output json`, `--output csv`, or `--output raw`.

```bash
wxc-cli rooms list --output json | jq '.[].title'
```

### Pydantic model inputs

Methods that take a Pydantic model (e.g. `create`, `update`) accept a `--<param>-json` flag:

```bash
wxc-cli people create \
  --settings-json '{"emails":["alice@example.com"],"display_name":"Alice"}'
```

### Raw output

`-o raw` is clean stdout with no Rich markup, headers, or decoration.

| Situation                                    | Output                                      |
|----------------------------------------------|---------------------------------------------|
| `--fields person_id` (single field)          | one bare value per line — directly pipeable |
| `--fields person_id,display_name` (multiple) | tab-separated, one record per line          |
| no `--fields`                                | all non-None fields, tab-separated          |
| `list[scalar]` or nested model in a field    | JSON-encoded inline                         |
| scalar return (`bool`, `str`, `int`)         | `str(value)`                                |

Typical shell patterns:

```bash
# Feed IDs into a loop
for id in $(wxc-cli people list --display-name Alice -o raw --fields person_id); do
    wxc-cli people details --person-id "$id" --calling-data -o json
done

# Extract one field with cut/awk
wxc-cli rooms list -o raw --fields title,type | awk -F'\t' '$2=="direct" {print $1}'


# xargs
wxc-cli people list --display-name Alice -o raw --fields person_id \
  | xargs -I{} wxc-cli person-settings forwarding read --entity-id {} -o json \
  | jq '.call_forwarding.always'
  
# command substitution
wxc-cli people list --calling-data --fields display_name,emails \
  --location-id $(wxc-cli locations list --name Hartford --fields location_id -o raw)

```

## How it works

`cli.py` introspects `WebexSimpleApi` at import time:

1. Each sub-API attribute (`.people`, `.telephony`, `.rooms`, …) becomes a **Typer command group**.
2. Each public method becomes a **command** inside that group.
3. The method's type-annotated parameters become **`--option` flags** — scalars directly, Pydantic models via
   `--<n>-json`.
4. Generator return types (list endpoints) are consumed fully and rendered as a Rich table or JSON array.
5. Nested sub-APIs (e.g. `telephony.calls`, `telephony.callqueue`) recurse into sub-groups automatically.

New endpoints added to `wxc_sdk` appear in the CLI with **zero maintenance**.

## Shell completion

```bash
wxc-cli --install-completion   # bash / zsh / fish
```

