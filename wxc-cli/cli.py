#!/usr/bin/env -S uv run --script
"""
wxc - CLI for the Webex SDK (wxc_sdk)

Auto-generates Typer commands by introspecting WebexSimpleApi at startup.
Every sub-API becomes a command group; every public method becomes a command.

Auth: run `wxc login` to store a token, set WEBEX_ACCESS_TOKEN, or pass --token.

Usage examples:
  wxc login                                    # store token in keyring
  wxc people list --display-name Alice
  wxc people list --output json --max-items 5
  wxc people list --fields person_id,emails,display_name
  wxc people details --person-id Y2lzY29...
  wxc people create --first-name Alice --last-name Smith --emails alice@x.com
  wxc rooms list --output json | jq '.[].title'
  wxc telephony calls list-calls --dry-run
"""

from __future__ import annotations

import collections.abc
import inspect
import json
import os
import typing
from typing import Any, Callable, Optional

import typer
import wxc_sdk
from pydantic import BaseModel
from rich import print as rprint
from rich.console import Console
from rich.table import Table

console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SKIP_METHODS = frozenset({"ep", "get", "post", "put", "patch", "delete"})
_RESERVED_PARAM_NAMES = frozenset(
    {"help", "output", "token", "ctx", "max_items", "fields", "dry_run"}
)
_KEYRING_SERVICE = "wxc-cli"
_KEYRING_USER = "access-token"


# ---------------------------------------------------------------------------
# Keyring helpers (graceful fallback when keyring has no backend)
# ---------------------------------------------------------------------------


def _keyring_get() -> str | None:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
    except Exception:
        return None


def _keyring_set(token: str) -> bool:
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, token)
        return True
    except Exception:
        return False


def _keyring_delete() -> bool:
    try:
        import keyring

        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
        return True
    except Exception:
        return False


def _resolve_token() -> str | None:
    """Return a token from env → keyring, in that order."""
    return os.environ.get("WEBEX_ACCESS_TOKEN") or _keyring_get()


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


def _is_sub_api(obj: Any) -> bool:
    name = type(obj).__name__
    return "Api" in name or name.endswith("API")


def _scalar_cli_type(annotation: Any) -> type:
    if annotation in (str, int, float, bool):
        return annotation
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _scalar_cli_type(args[0])
    return str


def _unwrap_optional(annotation: Any) -> Any:
    """Strip Optional[X] → X."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_generator_return(sig: inspect.Signature) -> bool:
    origin = typing.get_origin(sig.return_annotation)
    return origin is collections.abc.Generator


# ---------------------------------------------------------------------------
# Pydantic field expansion
# ---------------------------------------------------------------------------


def _flat_model_fields(
    model_cls: type[BaseModel],
) -> dict[str, tuple[type, Any]]:
    """
    Return {field_name: (cli_type, default)} for fields that can be
    represented as a single CLI string/bool/int/float option.

    Supports:
    - Scalar fields:      str, int, float, bool
    - list[scalar]:       accepted as comma-separated string, split at call time
    Skips:
    - Nested models, dict, list[Model], complex types
    """
    result: dict[str, tuple[type, Any]] = {}
    for fname, field in model_cls.model_fields.items():
        ann = _unwrap_optional(field.annotation)
        _default = field.default  # usually None or PydanticUndefined

        # Scalar
        if ann in (str, int, float, bool):
            result[fname] = (ann, None)
            continue

        # list[scalar]
        origin = typing.get_origin(ann)
        if origin is list:
            args = typing.get_args(ann)
            if args and args[0] in (str, int, float):
                result[fname] = (str, None)  # comma-sep string
                continue

    return result


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_output(
    result: Any,
    output: str,
    max_items: Optional[int] = None,
    fields: Optional[list[str]] = None,
) -> None:
    """Pretty-print a single model, list, or generator."""
    if isinstance(result, collections.abc.Generator):
        items = list(result) if max_items is None else _take(result, max_items)
    elif isinstance(result, list):
        items = result if max_items is None else result[:max_items]
    else:
        items = [result]

    if output == "json":
        rows = []
        for item in items:
            if isinstance(item, BaseModel):
                data = json.loads(item.model_dump_json(exclude_none=True))
                if fields:
                    data = {k: data[k] for k in fields if k in data}
                rows.append(data)
            else:
                rows.append(item)
        payload = rows if len(rows) != 1 else rows[0]
        rprint(json.dumps(payload, indent=2))
        return

    if not items:
        rprint("[yellow]No results.[/yellow]")
        return

    first = items[0]
    if isinstance(first, BaseModel):
        all_fields = list(first.__class__.model_fields.keys())
        if fields:
            show = [f for f in fields if f in all_fields]
            if not show:
                rprint(
                    f"[red]None of the requested fields exist. Available: {', '.join(all_fields)}[/red]"
                )
                return
        else:
            show = all_fields[:6]

        table = Table(show_header=True, header_style="bold cyan", expand=False)
        for f in show:
            table.add_column(f, overflow="fold", max_width=40)
        for item in items:
            table.add_row(*[str(getattr(item, f, "") or "") for f in show])
        console.print(table)

        hidden = len(all_fields) - len(show)
        hints = []
        if hidden > 0 and not fields:
            hints.append(
                f"[dim]+{hidden} hidden fields — use --fields or --output json[/dim]"
            )
        if max_items and len(items) == max_items:
            hints.append(
                f"[dim]Results capped at {max_items} — increase with --max-items[/dim]"
            )
        for h in hints:
            rprint(h)
    else:
        for item in items:
            rprint(item)


def _take(gen: collections.abc.Generator, n: int) -> list:
    result = []
    for item in gen:
        result.append(item)
        if len(result) >= n:
            break
    return result


# ---------------------------------------------------------------------------
# Dynamic command factory
# ---------------------------------------------------------------------------


def _build_command_fn(method: Callable) -> Callable:
    """
    Synthesise a real Python function with named keyword-only parameters.

    Global options added to every command:
      --output table|json     render mode
      --max-items N           cap generator results
      --fields f1,f2,f3       choose which model fields to display
      --dry-run               print the call without executing it

    Per-param options:
      Scalar types            → --param-name option
      Pydantic model          → individual --field-name options (scalars)
                                plus --<param>-json fallback for full JSON
    """
    sig = inspect.signature(method)

    usable: list[tuple[str, inspect.Parameter]] = []
    for pname, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            continue
        if pname in ("self", "params") or pname in _RESERVED_PARAM_NAMES:
            continue
        usable.append((pname, param))

    pydantic_params: dict[str, type[BaseModel]] = {
        pname: param.annotation
        for pname, param in usable
        if inspect.isclass(param.annotation) and issubclass(param.annotation, BaseModel)
    }

    # For each Pydantic param, compute the flat scalar fields we can expose
    pydantic_flat: dict[str, dict[str, tuple[type, Any]]] = {
        pname: _flat_model_fields(model_cls)
        for pname, model_cls in pydantic_params.items()
    }

    _is_list_cmd = _is_generator_return(sig)

    # ----------------------------------------------------------------
    # Build exec namespace + function signature source
    # ----------------------------------------------------------------
    exec_ns: dict[str, Any] = {
        "Optional": Optional,
        "typer": typer,
        # global option defaults
        "_output_default": typer.Option(
            "table", "--output", "-o", help="Output format: table | json"
        ),
        "_max_items_default": typer.Option(
            None,
            "--max-items",
            "-n",
            help="Cap number of results (list commands)",
            min=1,
        ),
        "_fields_default": typer.Option(
            None, "--fields", help="Comma-separated field names to display"
        ),
        "_dry_run_default": typer.Option(
            False,
            "--dry-run",
            is_flag=True,
            help="Print the SDK call without executing it",
        ),
    }

    sig_parts = [
        "def cmd(*,",
        "    output: str = _output_default,",
        "    max_items: Optional[int] = _max_items_default,",
        "    fields: Optional[str] = _fields_default,",
        "    dry_run: bool = _dry_run_default,",
    ]
    captured_flat_keys: dict[str, str] = {}  # cli_key → pname (the parent model param)

    for pname, param in usable:
        if pname in pydantic_params:
            model_cls = pydantic_params[pname]
            flat = pydantic_flat[pname]
            for fname, (ftype, _fdefault) in flat.items():
                cli_key = f"{pname}__{fname}"
                exec_ns[f"_ann_{cli_key}"] = Optional[ftype]
                exec_ns[f"_def_{cli_key}"] = typer.Option(
                    None,
                    f"--{fname.replace('_', '-')}",
                    help=f"[{model_cls.__name__}] {fname}",
                    show_default=False,
                )
                sig_parts.append(f"    {cli_key}: _ann_{cli_key} = _def_{cli_key},")
                captured_flat_keys[cli_key] = pname

            # Always also expose a --<param>-json fallback
            json_key = f"{pname}__json"
            exec_ns[f"_ann_{json_key}"] = Optional[str]
            exec_ns[f"_def_{json_key}"] = typer.Option(
                None,
                f"--{pname.replace('_', '-')}-json",
                help=f"Full {model_cls.__name__} as JSON (overrides individual fields)",
                show_default=False,
            )
            sig_parts.append(f"    {json_key}: _ann_{json_key} = _def_{json_key},")
        else:
            ann = (
                param.annotation
                if param.annotation is not inspect.Parameter.empty
                else str
            )
            cli_type = _scalar_cli_type(ann)
            default = (
                param.default if param.default is not inspect.Parameter.empty else None
            )
            exec_ns[f"_ann_{pname}"] = Optional[cli_type]
            exec_ns[f"_def_{pname}"] = typer.Option(
                default,
                f"--{pname.replace('_', '-')}",
                show_default=(default is not None and default is not False),
            )
            sig_parts.append(f"    {pname}: _ann_{pname} = _def_{pname},")

    sig_parts.append("):")
    sig_parts.append("    pass")

    exec(compile("\n".join(sig_parts), "<generated>", "exec"), exec_ns)
    shell_fn = exec_ns["cmd"]

    # ----------------------------------------------------------------
    # Runtime logic (closure over method + metadata)
    # ----------------------------------------------------------------
    captured_method = method
    captured_usable = usable
    captured_pydantic = pydantic_params
    captured_pydantic_flat = pydantic_flat

    def runtime(**kw: Any) -> None:
        output = kw.pop("output", "table")
        max_items_raw = kw.pop("max_items", None)
        fields_raw = kw.pop("fields", None)
        dry_run = kw.pop("dry_run", False)

        max_items = int(max_items_raw) if max_items_raw is not None else None
        fields = [f.strip() for f in fields_raw.split(",")] if fields_raw else None

        call_kw: dict[str, Any] = {}

        for pname, _param in captured_usable:
            if pname in captured_pydantic:
                model_cls = captured_pydantic[pname]
                flat = captured_pydantic_flat[pname]
                json_key = f"{pname}__json"
                raw_json = kw.get(json_key)

                if raw_json:
                    # Full JSON overrides individual fields
                    try:
                        call_kw[pname] = model_cls.model_validate_json(raw_json)
                    except Exception as e:
                        rprint(
                            f"[red]Bad JSON for --{pname.replace('_', '-')}-json: {e}[/red]"
                        )
                        raise typer.Exit(1)
                else:
                    # Collect individual flat fields into a model
                    model_data: dict[str, Any] = {}
                    for fname, (ftype, _) in flat.items():
                        cli_key = f"{pname}__{fname}"
                        val = kw.get(cli_key)
                        if val is not None:
                            # list[scalar] fields are passed as comma-separated strings
                            ann = _unwrap_optional(
                                model_cls.model_fields[fname].annotation
                            )
                            if typing.get_origin(ann) is list:
                                model_data[fname] = [v.strip() for v in val.split(",")]
                            else:
                                model_data[fname] = val
                    if model_data:
                        try:
                            call_kw[pname] = model_cls.model_validate(model_data)
                        except Exception as e:
                            rprint(
                                f"[red]Invalid model data for {model_cls.__name__}: {e}[/red]"
                            )
                            raise typer.Exit(1)
            else:
                val = kw.get(pname)
                if val is not None:
                    call_kw[pname] = val

        if dry_run:
            qname = (
                f"{type(captured_method.__self__).__name__}.{captured_method.__name__}"
            )
            rprint("[bold cyan]DRY RUN[/bold cyan] — would call:")
            rprint(f"  [yellow]{qname}[/yellow](")
            for k, v in call_kw.items():
                rprint(f"    [green]{k}[/green] = {v!r}")
            rprint("  )")
            return

        try:
            result = captured_method(**call_kw)
        except Exception as e:
            rprint(f"[red]{type(e).__name__}: {e}[/red]")
            raise typer.Exit(1)

        if result is None:
            rprint("[green]✓ Done[/green]")
        else:
            _format_output(result, output, max_items=max_items, fields=fields)

    import functools

    @functools.wraps(shell_fn)
    def final(**kw: Any) -> None:
        runtime(**kw)

    final.__annotations__ = shell_fn.__annotations__
    final.__kwdefaults__ = shell_fn.__kwdefaults__
    return final


# ---------------------------------------------------------------------------
# Recursive group builder
# ---------------------------------------------------------------------------


def _register_api_group(parent: typer.Typer, name: str, api_obj: Any) -> None:
    group = typer.Typer(
        help=f"{type(api_obj).__name__} commands",
        no_args_is_help=True,
        rich_markup_mode="rich",
    )
    parent.add_typer(group, name=name.replace("_", "-"))

    for mname, method in inspect.getmembers(api_obj, predicate=inspect.ismethod):
        if mname.startswith("_") or mname in _SKIP_METHODS:
            continue
        try:
            fn = _build_command_fn(method)
            doc = (inspect.getdoc(method) or mname).splitlines()[0]
            fn.__doc__ = doc
            group.command(name=mname.replace("_", "-"), help=doc)(fn)
        except Exception:
            pass

    for attr_name in sorted(dir(api_obj)):
        if attr_name.startswith("_"):
            continue
        try:
            child = getattr(api_obj, attr_name)
        except Exception:
            continue
        if _is_sub_api(child):
            _register_api_group(group, attr_name, child)


# ---------------------------------------------------------------------------
# Root app
# ---------------------------------------------------------------------------

root_app = typer.Typer(
    name="wxc",
    help="[bold]Webex CLI[/bold] — every wxc_sdk endpoint as a command.\n\n"
    "Set [bold cyan]WEBEX_ACCESS_TOKEN[/bold cyan], run [bold]wxc login[/bold], "
    "or pass [bold]--token[/bold].",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Ensure the underlying Click context always sees "wxc" as the program name,
# regardless of how the script was invoked (python cli.py, ./cli.py, etc.).
# This is what gets baked into shell completion scripts.
_PROG_NAME = "wxc_cli"


@root_app.callback()
def _root(
    token: Optional[str] = typer.Option(
        None,
        "--token",
        "-t",
        help="Webex access token (overrides env / keyring)",
        envvar="WEBEX_ACCESS_TOKEN",
        show_default=False,
    ),
):
    if token:
        os.environ["WEBEX_ACCESS_TOKEN"] = token
    else:
        stored = _keyring_get()
        if stored:
            os.environ["WEBEX_ACCESS_TOKEN"] = stored


# ---------------------------------------------------------------------------
# Built-in commands (login / logout / whoami)
# ---------------------------------------------------------------------------


@root_app.command()
def login(
    token: str = typer.Option(
        ...,
        "--token",
        "-t",
        help="Your Webex personal access token",
        prompt="Webex access token",
        hide_input=True,
    ),
):
    """Store a Webex access token in the system keyring."""
    if _keyring_set(token):
        os.environ["WEBEX_ACCESS_TOKEN"] = token
        rprint("[green]✓ Token saved to keyring.[/green]")
    else:
        rprint(
            "[yellow]⚠ Keyring unavailable — set WEBEX_ACCESS_TOKEN manually.[/yellow]"
        )


@root_app.command()
def logout():
    """Remove the stored Webex access token from the keyring."""
    if _keyring_delete():
        rprint("[green]✓ Token removed.[/green]")
    else:
        rprint("[yellow]No stored token found (or keyring unavailable).[/yellow]")


@root_app.command()
def completion(
    shell: Optional[str] = typer.Option(
        None,
        "--shell",
        "-s",
        help="Shell type: bash | zsh | fish. Auto-detected from $SHELL if omitted.",
    ),
    install: bool = typer.Option(
        False,
        "--install",
        is_flag=True,
        help="Write the script to the appropriate rc file automatically.",
    ),
):
    """
    Print or install shell completion for wxc.

    Usage:
      wxc completion          # auto-detect shell, print script
      wxc completion bash     # print bash script
      wxc completion --install  # write to ~/.bashrc / ~/.zshrc / ~/.config/fish/...
    """
    # Auto-detect shell from $SHELL if not given
    if shell is None:
        shell_path = os.environ.get("SHELL", "")
        shell = os.path.basename(shell_path)
        if shell not in ("bash", "zsh", "fish"):
            rprint(
                "[red]Could not auto-detect shell. Pass bash, zsh, or fish explicitly.[/red]"
            )
            raise typer.Exit(1)

    shell = shell.lower()
    if shell not in ("bash", "zsh", "fish"):
        rprint(f"[red]Unsupported shell '{shell}'. Choose bash, zsh, or fish.[/red]")
        raise typer.Exit(1)

    # Generate via Click's ShellComplete, explicitly naming the program "wxc".
    # This is the reliable path: it never reads sys.argv[0], so the script is
    # always correct regardless of how the user originally invoked the CLI.
    complete_var = f"_{_PROG_NAME.upper()}_COMPLETE"
    try:
        from click.shell_completion import BashComplete, ZshComplete, FishComplete

        _cls = {"bash": BashComplete, "zsh": ZshComplete, "fish": FishComplete}[shell]
        cli_obj = typer.main.get_command(app)
        script = _cls(cli_obj, {}, _PROG_NAME, complete_var).source()
    except Exception as e:
        rprint(f"[red]Could not generate completion script: {e}[/red]")
        raise typer.Exit(1)

    if not install:
        rprint(script)
        rprint()
        if shell == "bash":
            rprint("[dim]# Add to ~/.bashrc:[/dim]")
            rprint(f'[dim]#   eval "$({_PROG_NAME} completion --shell bash)"[/dim]')
        elif shell == "zsh":
            rprint("[dim]# Add to ~/.zshrc:[/dim]")
            rprint(f'[dim]#   eval "$({_PROG_NAME} completion --shell zsh)"[/dim]')
        elif shell == "fish":
            rprint(f"[dim]# Add to ~/.config/fish/completions/{_PROG_NAME}.fish:[/dim]")
            rprint(
                f"[dim]#   {_PROG_NAME} completion --shell fish > ~/.config/fish/completions/{_PROG_NAME}.fish[/dim]"
            )
        return

    # --install: write to the appropriate rc file
    home = os.path.expanduser("~")
    if shell == "bash":
        rc = os.path.join(home, ".bashrc")
        line = f'eval "$({_PROG_NAME} completion --shell bash)"'
    elif shell == "zsh":
        rc = os.path.join(home, ".zshrc")
        line = f'eval "$({_PROG_NAME} completion --shell zsh)"'
    elif shell == "fish":
        fish_dir = os.path.join(home, ".config", "fish", "completions")
        os.makedirs(fish_dir, exist_ok=True)
        rc = os.path.join(fish_dir, f"{_PROG_NAME}.fish")
        with open(rc, "w") as f:
            f.write(script + "\n")
        rprint(f"[green]✓ Completion written to {rc}[/green]")
        return

    # bash / zsh: add eval line to rc if not already there
    try:
        existing = open(rc).read() if os.path.exists(rc) else ""
        if line in existing:
            rprint(f"[yellow]Completion already present in {rc}[/yellow]")
        else:
            with open(rc, "a") as f:
                f.write(f"\n# {_PROG_NAME} shell completion\n{line}\n")
            rprint(f"[green]✓ Added to {rc}[/green]")
            rprint(f"[dim]Restart your shell or run: source {rc}[/dim]")
    except OSError as e:
        rprint(f"[red]Could not write to {rc}: {e}[/red]")
        raise typer.Exit(1)


@root_app.command()
def whoami(
    output: str = typer.Option(
        "table", "--output", "-o", help="Output format: table | json"
    ),
):
    """Show the currently authenticated user."""
    tok = _resolve_token()
    if not tok:
        rprint(
            f"[red]Not authenticated. Run `{_PROG_NAME} login` or set WEBEX_ACCESS_TOKEN.[/red]"
        )
        raise typer.Exit(1)
    try:
        api = wxc_sdk.WebexSimpleApi(tokens=tok)
        me = api.people.me()
        _format_output(me, output)
    except Exception as e:
        rprint(f"[red]{type(e).__name__}: {e}[/red]")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# CLI builder
# ---------------------------------------------------------------------------


def build_cli() -> typer.Typer:
    probe = wxc_sdk.WebexSimpleApi(tokens=os.environ.get("WEBEX_ACCESS_TOKEN", "dummy"))
    for attr_name in sorted(dir(probe)):
        if attr_name.startswith("_"):
            continue
        try:
            child = getattr(probe, attr_name)
        except Exception:
            continue
        if _is_sub_api(child):
            _register_api_group(root_app, attr_name, child)
    return root_app


app = build_cli()

if __name__ == "__main__":
    app(prog_name=_PROG_NAME)
