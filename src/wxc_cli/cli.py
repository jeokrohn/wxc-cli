#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = [
#     "keyring>=25.7.0",
#     "python-dotenv>=1.2.2",
#     "rich>=14.3.3",
#     "typer>=0.24.1",
#     "wxc-sdk>=1.30.0",
# ]
# ///
"""
wxc-cli - CLI for the Webex SDK (wxc_sdk)

Auto-generates Typer commands by introspecting WebexSimpleApi at startup.
Every sub-API becomes a command group; every public method becomes a command.

Auth: run `wxc-cli login` to store a token, set WEBEX_ACCESS_TOKEN, or pass --token.

Usage examples:
  wxc-cli login                                    # store token in keyring
  wxc-cli people list --display-name Alice
  wxc-cli people list --output json --max-items 5
  wxc-cli people list --fields person_id,emails,display_name
  wxc-cli people details --person-id Y2lzY29...
  wxc-cli people create --first-name Alice --last-name Smith --emails alice@x.com
  wxc-cli rooms list --output json | jq '.[].title'
  wxc-cli telephony calls list-calls --dry-run
"""

from __future__ import annotations

import collections.abc
import inspect
import json
import os
import typing
from collections.abc import Callable
from typing import Any, Optional

import typer
import wxc_sdk
from pydantic import BaseModel
from rich import print as rprint
from rich.console import Console
from rich.table import Table

__version__ = 'v0.3.0'

console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Methods defined on ApiChild that are raw HTTP helpers — skip unless overridden.
_SKIP_METHODS = frozenset({'ep', 'get', 'post', 'put', 'patch', 'delete'})
# Always skip 'ep' (internal URL builder) regardless of override.
_ALWAYS_SKIP = frozenset({'ep', 'f_ep'})


def _is_inherited_http_helper(cls: type, mname: str) -> bool:
    """
    Return True when mname should be skipped — i.e. it is not defined
    directly on cls or any SDK-specific intermediate class, meaning it is
    just the raw ApiChild HTTP helper inherited unchanged.

    If the concrete class (or a mixin between it and ApiChild) overrides
    the method with a typed signature we want to include it.
    """
    from wxc_sdk.api_child import ApiChild

    for klass in cls.__mro__:
        if mname in klass.__dict__:
            # First class in MRO that owns this method
            return klass is ApiChild or klass is object
    return True


_RESERVED_PARAM_NAMES = frozenset(
    {'help', 'output', 'token', 'ctx', 'max_items', 'fields', 'dry_run', 'patch', 'from_file'}
)
_KEYRING_SERVICE = 'wxc-cli'
_KEYRING_USER = 'access-token'


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
    return os.environ.get('WEBEX_ACCESS_TOKEN') or _keyring_get()


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


def _is_sub_api(obj: Any) -> bool:
    name = type(obj).__name__
    return 'Api' in name or name.endswith('API')


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


def _flatten_for_csv(item: Any) -> dict[str, str]:
    """
    Convert a Pydantic model (or plain dict) to a flat {str: str} dict for CSV.

    Rules:
    - Scalar values          → str(value)
    - list[scalar]           → pipe-joined  "a|b|c"
    - list[model/dict]       → JSON string  (too nested to flatten further)
    - nested model/dict      → JSON string
    - None                   → ""
    """
    if isinstance(item, BaseModel):
        raw = item.model_dump(exclude_none=False, mode='json')
    elif isinstance(item, dict):
        raw = item
    else:
        return {'value': str(item)}

    flat: dict[str, str] = {}
    for k, v in raw.items():
        if v is None:
            flat[k] = ''
        elif isinstance(v, list):
            if not v:
                flat[k] = ''
            elif isinstance(v[0], (dict, BaseModel)):
                flat[k] = json.dumps(
                    [i.model_dump(exclude_none=True, mode='json') if isinstance(i, BaseModel) else i for i in v]
                )
            else:
                flat[k] = '|'.join(str(x) for x in v)
        elif isinstance(v, (dict, BaseModel)):
            flat[k] = json.dumps(v.model_dump(exclude_none=True, mode='json') if isinstance(v, BaseModel) else v)
        else:
            flat[k] = str(v)
    return flat


def _format_raw(items: list, fields: list[str] | None) -> None:
    """
    Raw output — no Rich markup, no decorators, stdout only.

    Rules:
    - Single field requested (or single scalar result) -> one bare value per line
    - Multiple fields (or no --fields on a model)      -> tab-separated values,
                                                          one record per line,
                                                          no header
    - Non-model scalars (bool, str, int)               -> str(value) per line
    - list/nested model fields                         -> JSON-encoded inline
    """
    import sys

    out = sys.stdout

    def _row(item: Any, wanted: list[str] | None) -> list[str]:
        if isinstance(item, BaseModel):
            d = item.model_dump()  # snake_case, keeps Nones
            if wanted:
                return [_cell(d.get(f)) for f in wanted]
            # No --fields: emit only non-None scalar fields, tab-separated
            return [_cell(v) for v in d.values() if v is not None]
        return [str(item) if item is not None else '']

    def _cell(v: Any) -> str:
        if v is None:
            return ''
        if isinstance(v, (list, dict)):
            return json.dumps(v, separators=(',', ':'))
        return str(v)

    for item in items:
        cells = _row(item, fields)
        if len(cells) == 1:
            print(cells[0], file=out)
        else:
            print('\t'.join(cells), file=out)


def _format_output(
    result: Any, output: str, max_items: Optional[int] = None, fields: Optional[list[str]] = None
) -> None:
    """Render a single model, list, or generator as table / json / csv."""
    import csv as _csv
    import sys

    if isinstance(result, collections.abc.Generator):
        items = list(result) if max_items is None else _take(result, max_items)
    elif isinstance(result, list):
        items = result if max_items is None else result[:max_items]
    else:
        items = [result]

    # ---- JSON ----
    if output == 'json':
        rows = []
        for item in items:
            if isinstance(item, BaseModel):
                data = item.model_dump(exclude_none=True, mode='json')
                if fields:
                    data = {k: data[k] for k in fields if k in data}
                rows.append(data)
            else:
                rows.append(item)
        payload = rows if len(rows) != 1 else rows[0]
        rprint(json.dumps(payload, indent=2))
        return

    # ---- CSV ----
    if output == 'csv':
        if not items:
            return
        flat_rows = [_flatten_for_csv(item) for item in items]
        if fields:
            # keep only requested fields, in order; fill missing with ""
            all_keys = fields
            flat_rows = [{k: row.get(k, '') for k in all_keys} for row in flat_rows]
        else:
            # Union of all keys across rows, preserving first-row order
            all_keys = list(dict.fromkeys(k for row in flat_rows for k in row))

        writer = _csv.DictWriter(
            sys.stdout,
            fieldnames=all_keys,
            extrasaction='ignore',
            lineterminator='\n',
        )
        writer.writeheader()
        writer.writerows(flat_rows)
        if max_items and len(items) == max_items:
            rprint(f'[dim]# Results capped at {max_items} — increase with --max-items[/dim]', file=sys.stderr)
        return

    # ---- raw ----
    if output == 'raw':
        _format_raw(items, fields)
        return

    # ---- table (default) ----
    if not items:
        rprint('[yellow]No results.[/yellow]')
        return

    first = items[0]
    if isinstance(first, BaseModel):
        all_fields = list(first.__class__.model_fields.keys())
        if fields:
            show = [f for f in fields if f in all_fields]
            if not show:
                rprint(f'[red]None of the requested fields exist. Available: {", ".join(all_fields)}[/red]')
                return
        else:
            show = all_fields[:6]

        table = Table(show_header=True, header_style='bold cyan', expand=False)
        for f in show:
            table.add_column(f, overflow='fold', max_width=40)
        for item in items:
            table.add_row(*[str(getattr(item, f, '') or '') for f in show])
        console.print(table)

        hidden = len(all_fields) - len(show)
        hints = []
        if hidden > 0 and not fields:
            hints.append(f'[dim]+{hidden} hidden fields — use --fields or --output json/csv[/dim]')
        if max_items and len(items) == max_items:
            hints.append(f'[dim]Results capped at {max_items} — increase with --max-items[/dim]')
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
# Model utilities: deep merge, template, @file resolution, reader lookup
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _resolve_json_arg(raw: str) -> str:
    import sys as _sys

    if raw.startswith('@'):
        source = raw[1:]
        try:
            if source == '-':
                return _sys.stdin.read()
            with open(source) as fh:
                return fh.read()
        except OSError as e:
            raise ValueError(f'Cannot read {source!r}: {e}') from e
    return raw


def _make_template(cls, _depth: int = 0) -> dict:
    import enum as _enum

    result = {}
    for fname, field in cls.model_fields.items():
        ann = _unwrap_optional(field.annotation)
        is_list = typing.get_origin(ann) is list
        if is_list:
            inner_args = typing.get_args(ann)
            ann = inner_args[0] if inner_args else str
        if inspect.isclass(ann) and issubclass(ann, BaseModel):
            inner = _make_template(ann, _depth + 1) if _depth < 3 else {}
            result[fname] = [inner] if is_list else inner
        elif inspect.isclass(ann) and issubclass(ann, _enum.Enum):
            vals = [e.value for e in ann]
            result[fname] = [vals[0]] if is_list else vals[0]
        elif ann is bool:
            result[fname] = [] if is_list else False
        elif ann is int:
            result[fname] = [] if is_list else 0
        elif ann is str:
            result[fname] = [] if is_list else ''
        else:
            result[fname] = [] if is_list else None
    return result


def _get_single_model_return(sig):
    ret = sig.return_annotation
    if ret is inspect.Parameter.empty:
        return None
    ret = _unwrap_optional(ret)
    if typing.get_origin(ret) in (list, collections.abc.Generator):
        return None
    if inspect.isclass(ret) and issubclass(ret, BaseModel):
        return ret
    return None


def _find_reader(resolve_method_fn, model_cls):
    writer = resolve_method_fn()
    api_obj = writer.__self__
    for mname, method in inspect.getmembers(api_obj, predicate=inspect.ismethod):
        if mname.startswith('_'):
            continue
        try:
            ret = _get_single_model_return(inspect.signature(method))
            if ret is not None and (ret is model_cls or issubclass(ret, model_cls)):
                return method
        except Exception:
            continue
    raise ValueError(
        f'No read method returning {model_cls.__name__} found on '
        f'{type(api_obj).__name__}. '
        f'Supply the full object with --{model_cls.__name__.lower()}-json instead.'
    )


# ---------------------------------------------------------------------------
# Dynamic command factory
# ---------------------------------------------------------------------------


def _build_command_fn(method: Callable, api_path: str, method_name: str) -> Callable:
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
        if param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        if pname in ('self', 'params') or pname in _RESERVED_PARAM_NAMES:
            continue
        usable.append((pname, param))

    pydantic_params: dict[str, type[BaseModel]] = {
        pname: param.annotation
        for pname, param in usable
        if inspect.isclass(param.annotation) and issubclass(param.annotation, BaseModel)
    }

    # For each Pydantic param, compute the flat scalar fields we can expose
    pydantic_flat: dict[str, dict[str, tuple[type, Any]]] = {
        pname: _flat_model_fields(model_cls) for pname, model_cls in pydantic_params.items()
    }

    is_list_cmd = _is_generator_return(sig)

    # ----------------------------------------------------------------
    # Build exec namespace + function signature source
    # ----------------------------------------------------------------
    exec_ns: dict[str, Any] = {
        'Optional': Optional,
        'typer': typer,
        # global option defaults
        '_output_default': typer.Option('table', '--output', '-o', help='Output format: table | json | csv | raw'),
        '_max_items_default': typer.Option(
            None, '--max-items', '-n', help='Cap number of results (list commands)', min=1
        ),
        '_fields_default': typer.Option(None, '--fields', help='Comma-separated field names to display'),
        '_dry_run_default': typer.Option(
            False, '--dry-run', is_flag=True, help='Print the SDK call without executing it'
        ),
    }

    for _pn, _mc in pydantic_params.items():
        _pk = f'{_pn}__patch'
        exec_ns[f'_ann_{_pk}'] = Optional[str]
        exec_ns[f'_def_{_pk}'] = typer.Option(
            None,
            f'--{_pn.replace("_", "-")}-patch',
            help=(
                f'Partial JSON deep-merged over current {_mc.__name__}. '
                f'Accepts @file or @- (stdin). '
                f'Takes priority over --{_pn.replace("_", "-")}-json and flat flags.'
            ),
            show_default=False,
        )

    sig_parts = [
        'def cmd(*,',
        '    output: str = _output_default,',
        '    max_items: Optional[int] = _max_items_default,',
        '    fields: Optional[str] = _fields_default,',
        '    dry_run: bool = _dry_run_default,',
    ]
    for _pn in pydantic_params:
        _pk = f'{_pn}__patch'
        sig_parts.append(f'    {_pk}: _ann_{_pk} = _def_{_pk},')
    captured_flat_keys: dict[str, str] = {}  # cli_key → pname (the parent model param)

    # Scalar param names — used to detect flag name collisions with model fields
    _scalar_param_names = {pname for pname, param in usable if pname not in pydantic_params}

    for pname, param in usable:
        if pname in pydantic_params:
            model_cls = pydantic_params[pname]
            flat = pydantic_flat[pname]
            for fname, (ftype, _fdefault) in flat.items():
                cli_key = f'{pname}__{fname}'
                # If this field name collides with a scalar param name, prefix
                # the flag with the model param name to avoid duplicate flags.
                # e.g. locations.update has both location_id (scalar) and
                # Location.location_id (field) -> --location-id vs --settings-location-id
                if fname in _scalar_param_names:
                    _flag = f'--{pname.replace("_", "-")}-{fname.replace("_", "-")}'
                    _help = (
                        f'[{model_cls.__name__}] {fname} (prefixed to avoid ambiguity with --{fname.replace("_", "-")})'
                    )
                else:
                    _flag = f'--{fname.replace("_", "-")}'
                    _help = f'[{model_cls.__name__}] {fname}'
                exec_ns[f'_ann_{cli_key}'] = Optional[ftype]
                exec_ns[f'_def_{cli_key}'] = typer.Option(
                    None,
                    _flag,
                    help=_help,
                    show_default=False,
                )
                sig_parts.append(f'    {cli_key}: _ann_{cli_key} = _def_{cli_key},')
                captured_flat_keys[cli_key] = pname

            # Always also expose a --<param>-json fallback
            json_key = f'{pname}__json'
            exec_ns[f'_ann_{json_key}'] = Optional[str]
            _json_help = f'Full {model_cls.__name__} as JSON (overrides individual flags). Accepts @file or @-.'
            if not flat:
                _json_help += f' Use "wxc-cli schema {model_cls.__name__}" for a template.'
            exec_ns[f'_def_{json_key}'] = typer.Option(
                None,
                f'--{pname.replace("_", "-")}-json',
                help=_json_help,
                show_default=False,
            )
            sig_parts.append(f'    {json_key}: _ann_{json_key} = _def_{json_key},')
        else:
            ann = param.annotation if param.annotation is not inspect.Parameter.empty else str
            cli_type = _scalar_cli_type(ann)
            default = param.default if param.default is not inspect.Parameter.empty else None
            exec_ns[f'_ann_{pname}'] = Optional[cli_type]
            exec_ns[f'_def_{pname}'] = typer.Option(
                default,
                f'--{pname.replace("_", "-")}',
                show_default=(default is not None and default is not False),
            )
            sig_parts.append(f'    {pname}: _ann_{pname} = _def_{pname},')

    sig_parts.append('):')
    sig_parts.append('    pass')

    exec(compile('\n'.join(sig_parts), '<generated>', 'exec'), exec_ns)
    shell_fn = exec_ns['cmd']

    # ----------------------------------------------------------------
    # Runtime logic — resolve a fresh API instance at call time so that
    # the token set by _root() (via env var) is always used.
    # captured_method is kept only for dry-run label; never called.
    # ----------------------------------------------------------------
    captured_api_path = api_path
    captured_method_name = method_name
    captured_usable = usable
    captured_pydantic = pydantic_params
    captured_pydantic_flat = pydantic_flat

    def _resolve_method() -> Callable:
        """Instantiate a fresh API with the current token and navigate to the method."""
        tok = _resolve_token()
        if not tok:
            rprint('[red]Not authenticated. Run `wxc-cli login` or set WEBEX_ACCESS_TOKEN.[/red]')
            raise typer.Exit(1)
        api = wxc_sdk.WebexSimpleApi(tokens=tok)
        obj = api
        for part in captured_api_path.split('.'):
            obj = getattr(obj, part)
        return getattr(obj, captured_method_name)

    def runtime(**kw: Any) -> None:
        output = kw.pop('output', 'table')
        max_items_raw = kw.pop('max_items', None)
        fields_raw = kw.pop('fields', None)
        dry_run = kw.pop('dry_run', False)

        max_items = int(max_items_raw) if max_items_raw is not None else None
        fields = [f.strip() for f in fields_raw.split(',')] if fields_raw else None

        # Patches are collected here first; resolved after dry-run check
        _pending_patches: dict[str, tuple] = {}  # pname -> (model_cls, patch_dict)
        call_kw: dict[str, Any] = {}

        for pname, _param in captured_usable:
            if pname in captured_pydantic:
                model_cls = captured_pydantic[pname]
                flat = captured_pydantic_flat[pname]
                json_key = f'{pname}__json'
                raw_json = kw.get(json_key)

                patch_key = f'{pname}__patch'
                raw_patch = kw.get(patch_key)

                if raw_patch:
                    # --*-patch: parse the patch dict first (needed for dry-run too)
                    try:
                        patch_dict = json.loads(_resolve_json_arg(raw_patch))
                    except (ValueError, json.JSONDecodeError) as e:
                        rprint(f'[red]Bad patch JSON for --{pname.replace("_", "-")}-patch: {e}[/red]')
                        raise typer.Exit(1) from e
                    # Stash patch for dry-run display; actual read happens below
                    _pending_patches[pname] = (model_cls, patch_dict)
                    call_kw[pname] = None  # placeholder; resolved after dry-run check

                elif raw_json:
                    # --*-json: full object; supports @file / @- expansion
                    try:
                        call_kw[pname] = model_cls.model_validate_json(_resolve_json_arg(raw_json))
                    except Exception as e:
                        rprint(f'[red]Bad JSON for --{pname.replace("_", "-")}-json: {e}[/red]')
                        raise typer.Exit(1) from e
                else:
                    # Collect individual flat fields into a model
                    model_data: dict[str, Any] = {}
                    for fname, (ftype, _) in flat.items():
                        cli_key = f'{pname}__{fname}'
                        val = kw.get(cli_key)
                        if val is not None:
                            # list[scalar] fields are passed as comma-separated strings
                            ann = _unwrap_optional(model_cls.model_fields[fname].annotation)
                            if typing.get_origin(ann) is list:
                                model_data[fname] = [v.strip() for v in val.split(',')]
                            else:
                                model_data[fname] = val
                    if model_data:
                        try:
                            call_kw[pname] = model_cls.model_validate(model_data)
                        except Exception as e:
                            rprint(f'[red]Invalid model data for {model_cls.__name__}: {e}[/red]')
                            raise typer.Exit(1) from e
            else:
                val = kw.get(pname)
                if val is not None:
                    call_kw[pname] = val

        if dry_run:
            qname = f'{captured_api_path}.{captured_method_name}'
            rprint(f'[bold cyan]DRY RUN[/bold cyan] [dim]—[/dim] [yellow]{qname}[/yellow](')
            for k, v in call_kw.items():
                if v is None and k in _pending_patches:
                    continue  # printed below as patch block
                if isinstance(v, BaseModel):
                    v_str = v.model_dump_json(exclude_none=True, indent=2)
                    # indent continuation lines to align under the key
                    pad = ' ' * (4 + len(k) + 3)
                    v_str = v_str.replace('\n', '\n' + pad)
                    rprint(f'  [green]{k}[/green] = {v_str}')
                else:
                    rprint(f'  [green]{k}[/green] = {v!r}')
            # Show pending patches with what WOULD be read
            for _ppname, (_pmc, _pd) in _pending_patches.items():
                rprint(f'  [green]{_ppname}[/green] = [dim](patch — reads current {_pmc.__name__} then merges:)[/dim]')
                rprint('  ' + json.dumps(_pd, indent=2).replace('\n', '\n  '))
            rprint(')')
            return

        # Resolve any --patch args now (requires a live API read)
        for _ppname, (_pmodel_cls, _ppatch_dict) in _pending_patches.items():
            try:
                _reader = _find_reader(_resolve_method, _pmodel_cls)
                _reader_sig = inspect.signature(_reader)
                _reader_kw = {}
                for _spn, _sp in captured_usable:
                    if _spn not in captured_pydantic and _spn in _reader_sig.parameters:
                        _sv = kw.get(_spn)
                        if _sv is not None:
                            _reader_kw[_spn] = _sv
                _current = _reader(**_reader_kw)
                _merged = _deep_merge(_current.model_dump(), _ppatch_dict)
                call_kw[_ppname] = _pmodel_cls.model_validate(_merged)
            except typer.Exit:
                raise
            except Exception as e:
                rprint(f'[red]Patch failed: {e}[/red]')
                raise typer.Exit(1) from e

        try:
            result = _resolve_method()(**call_kw)
        except Exception as e:
            rprint(f'[red]{type(e).__name__}: {e}[/red]')
            raise typer.Exit(1) from e

        if result is None:
            rprint('[green]✓ Done[/green]')
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


def _register_api_group(parent: typer.Typer, name: str, api_obj: Any, api_path: str = '') -> None:
    group = typer.Typer(
        help=f'{type(api_obj).__name__} commands',
        no_args_is_help=True,
        rich_markup_mode='rich',
    )
    parent.add_typer(group, name=name.replace('_', '-'))

    # The dotted path from the root API to this object, e.g. "telephony.calls"
    current_path = f'{api_path}.{name}' if api_path else name

    for mname, method in inspect.getmembers(api_obj, predicate=inspect.ismethod):
        if mname.startswith('_'):
            continue
        if mname in _ALWAYS_SKIP:
            continue
        # Skip raw HTTP helpers unless the concrete class overrides them
        if mname in _SKIP_METHODS and _is_inherited_http_helper(type(api_obj), mname):
            continue
        try:
            fn = _build_command_fn(method, current_path, mname)
            doc = (inspect.getdoc(method) or mname).splitlines()[0]
            fn.__doc__ = doc
            group.command(name=mname.replace('_', '-'), help=doc)(fn)
        except Exception:
            pass

    for attr_name in sorted(dir(api_obj)):
        if attr_name.startswith('_'):
            continue
        try:
            child = getattr(api_obj, attr_name)
        except Exception:
            continue
        if _is_sub_api(child):
            _register_api_group(group, attr_name, child, api_path=current_path)


# ---------------------------------------------------------------------------
# Root app
# ---------------------------------------------------------------------------

root_app = typer.Typer(
    name='wxc-cli',
    help='[bold]Webex CLI[/bold] — every wxc_sdk endpoint as a command.\n\n'
    'Set [bold cyan]WEBEX_ACCESS_TOKEN[/bold cyan], run [bold]wxc-cli login[/bold], '
    'or pass [bold]--token[/bold].',
    no_args_is_help=True,
    rich_markup_mode='rich',
)

# Ensure the underlying Click context always sees "wxc-cli" as the program name,
# regardless of how the script was invoked (python cli.py, ./cli.py, etc.).
# This is what gets baked into shell completion scripts.
_PROG_NAME = 'wxc-cli'


@root_app.callback()
def _root(
    token: str | None = typer.Option(
        None,
        '--token',
        '-t',
        help='Webex access token (overrides env / keyring)',
        envvar='WEBEX_ACCESS_TOKEN',
        show_default=False,
    ),
):
    if token:
        os.environ['WEBEX_ACCESS_TOKEN'] = token
    else:
        stored = _keyring_get()
        if stored:
            os.environ['WEBEX_ACCESS_TOKEN'] = stored


# ---------------------------------------------------------------------------
# Built-in commands (login / logout / whoami)
# ---------------------------------------------------------------------------


@root_app.command()
def login(
    token: str = typer.Option(
        ...,
        '--token',
        '-t',
        help='Your Webex personal access token',
        prompt='Webex access token',
        hide_input=True,
    ),
):
    """Store a Webex access token in the system keyring."""
    if _keyring_set(token):
        os.environ['WEBEX_ACCESS_TOKEN'] = token
        rprint('[green]✓ Token saved to keyring.[/green]')
    else:
        rprint('[yellow]⚠ Keyring unavailable — set WEBEX_ACCESS_TOKEN manually.[/yellow]')


@root_app.command()
def logout():
    """Remove the stored Webex access token from the keyring."""
    if _keyring_delete():
        rprint('[green]✓ Token removed.[/green]')
    else:
        rprint('[yellow]No stored token found (or keyring unavailable).[/yellow]')


@root_app.command()
def completion(
    shell: Optional[str] = typer.Option(
        None,
        '--shell',
        '-s',
        help='Shell type: bash | zsh | fish. Auto-detected from $SHELL if omitted.',
    ),
    install: bool = typer.Option(
        False, '--install', is_flag=True, help='Write the script to the appropriate rc file automatically.'
    ),
):
    """
    Print or install shell completion for wxc-cli.

    Usage:
      wxc-cli completion          # auto-detect shell, print script
      wxc-cli completion bash     # print bash script
      wxc-cli completion --install  # write to ~/.bashrc / ~/.zshrc / ~/.config/fish/...
    """
    # Auto-detect shell from $SHELL if not given
    if shell is None:
        shell_path = os.environ.get('SHELL', '')
        shell = os.path.basename(shell_path)
        if shell not in ('bash', 'zsh', 'fish'):
            rprint('[red]Could not auto-detect shell. Pass bash, zsh, or fish explicitly.[/red]')
            raise typer.Exit(1)

    shell = shell.lower()
    if shell not in ('bash', 'zsh', 'fish'):
        rprint(f"[red]Unsupported shell '{shell}'. Choose bash, zsh, or fish.[/red]")
        raise typer.Exit(1)

    # Generate via Click's ShellComplete, explicitly naming the program "wxc-cli".
    # This is the reliable path: it never reads sys.argv[0], so the script is
    # always correct regardless of how the user originally invoked the CLI.
    complete_var = f'_{_PROG_NAME.upper()}_COMPLETE'
    try:
        from click.shell_completion import BashComplete, FishComplete, ZshComplete

        _cls = {'bash': BashComplete, 'zsh': ZshComplete, 'fish': FishComplete}[shell]
        cli_obj = typer.main.get_command(app)
        script = _cls(cli_obj, {}, _PROG_NAME, complete_var).source()
    except Exception as e:
        rprint(f'[red]Could not generate completion script: {e}[/red]')
        raise typer.Exit(1) from e

    if not install:
        rprint(script)
        rprint()
        if shell == 'bash':
            rprint('[dim]# Add to ~/.bashrc:[/dim]')
            rprint('[dim]#   eval "$(wxc-cli completion --shell bash)"[/dim]')
        elif shell == 'zsh':
            rprint('[dim]# Add to ~/.zshrc:[/dim]')
            rprint('[dim]#   eval "$(wxc-cli completion --shell zsh)"[/dim]')
        elif shell == 'fish':
            rprint('[dim]# Add to ~/.config/fish/completions/wxc-cli.fish:[/dim]')
            rprint('[dim]#   wxc-cli completion --shell fish > ~/.config/fish/completions/wxc-cli.fish[/dim]')
        return

    # --install: write to the appropriate rc file
    home = os.path.expanduser('~')
    if shell == 'bash':
        rc = os.path.join(home, '.bashrc')
        line = 'eval "$(wxc-cli completion --shell bash)"'
    elif shell == 'zsh':
        rc = os.path.join(home, '.zshrc')
        line = 'eval "$(wxc-cli completion --shell zsh)"'
    elif shell == 'fish':
        fish_dir = os.path.join(home, '.config', 'fish', 'completions')
        os.makedirs(fish_dir, exist_ok=True)
        rc = os.path.join(fish_dir, 'wxc-cli.fish')
        with open(rc, 'w') as f:
            f.write(script + '\n')
        rprint(f'[green]✓ Completion written to {rc}[/green]')
        return

    # bash / zsh: add eval line to rc if not already there
    try:
        existing = open(rc).read() if os.path.exists(rc) else ''
        if line in existing:
            rprint(f'[yellow]Completion already present in {rc}[/yellow]')
        else:
            with open(rc, 'a') as f:
                f.write(f'\n# wxc-cli shell completion\n{line}\n')
            rprint(f'[green]✓ Added to {rc}[/green]')
            rprint(f'[dim]Restart your shell or run: source {rc}[/dim]')
    except OSError as e:
        rprint(f'[red]Could not write to {rc}: {e}[/red]')
        raise typer.Exit(1)


@root_app.command()
def schema(
    model_name: Optional[str] = typer.Argument(None, help='Model class name, e.g. CallQueue, CallForwarding'),
    output: str = typer.Option(
        'template',
        '--output',
        '-o',
        help='Output: template (editable JSON with defaults) | schema (JSON Schema)',
    ),
    list_models: bool = typer.Option(False, '--list', '-l', is_flag=True, help='List all available model names'),
):
    """Print a JSON template or JSON Schema for any wxc_sdk model.

    Use this to build input files for --*-json @file flags.

    Examples:
      wxc-cli schema --list                           # discover model names
      wxc-cli schema CallForwarding > forwarding.json
      wxc-cli schema CallQueue --output schema | jq .properties
    """
    import importlib
    import pkgutil

    import wxc_sdk as _wxc_root

    # Shared: walk all modules collecting BaseModel subclasses
    def _all_models():
        seen = set()
        for _importer, modname, _ispkg in pkgutil.walk_packages(
            path=_wxc_root.__path__,
            prefix=_wxc_root.__name__ + '.',
            onerror=lambda _: None,
        ):
            try:
                mod = importlib.import_module(modname)
            except Exception:
                continue
            for attr in dir(mod):
                cls = getattr(mod, attr, None)
                if (
                    cls is not None
                    and inspect.isclass(cls)
                    and issubclass(cls, BaseModel)
                    and cls.__module__.startswith('wxc_sdk')
                    and cls.__name__ not in seen
                ):
                    seen.add(cls.__name__)
                    yield cls.__name__

    if list_models:
        names = sorted(_all_models())
        for name in names:
            rprint(name)
        rprint(f'[dim]({len(names)} models)[/dim]')
        return

    if model_name is None:
        rprint('[red]Provide a model name or use --list to see all models.[/red]')
        raise typer.Exit(1)

    target = None
    for _importer, modname, _ispkg in pkgutil.walk_packages(
        path=_wxc_root.__path__,
        prefix=_wxc_root.__name__ + '.',
        onerror=lambda _: None,
    ):
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        cls = getattr(mod, model_name, None)
        if cls is not None and inspect.isclass(cls) and issubclass(cls, BaseModel):
            target = cls
            break

    if target is None:
        # Try case-insensitive match and suggest closest names

        candidates = sorted(n for n in _all_models() if model_name.lower() in n.lower())[:8]
        if candidates:
            rprint(f"[red]Model '{model_name}' not found.[/red] Did you mean one of:")
            for c in candidates:
                rprint(f'  [cyan]{c}[/cyan]')
        else:
            rprint(f"[red]Model '{model_name}' not found.[/red]")
            rprint('[dim]Use wxc-cli schema --list to see all available models.[/dim]')
        raise typer.Exit(1)

    if output == 'schema':
        rprint(json.dumps(target.model_json_schema(), indent=2))
    else:
        rprint(json.dumps(_make_template(target), indent=2))


@root_app.command()
def whoami(
    output: str = typer.Option('table', '--output', '-o', help='Output format: table | json | csv | raw'),
):
    """Show the currently authenticated user."""
    tok = _resolve_token()
    if not tok:
        rprint('[red]Not authenticated. Run `wxc-cli login` or set WEBEX_ACCESS_TOKEN.[/red]')
        raise typer.Exit(1)
    try:
        api = wxc_sdk.WebexSimpleApi(tokens=tok)
        me = api.people.me()
        _format_output(me, output)
    except Exception as e:
        rprint(f'[red]{type(e).__name__}: {e}[/red]')
        raise typer.Exit(1) from e


# ---------------------------------------------------------------------------
# CLI builder
# ---------------------------------------------------------------------------


def build_cli() -> typer.Typer:
    probe = wxc_sdk.WebexSimpleApi(tokens=os.environ.get('WEBEX_ACCESS_TOKEN', 'dummy'))
    for attr_name in sorted(dir(probe)):
        if attr_name.startswith('_'):
            continue
        try:
            child = getattr(probe, attr_name)
        except Exception:
            continue
        if _is_sub_api(child):
            _register_api_group(root_app, attr_name, child)
    return root_app


app = build_cli()

# ---------------------------------------------------------------------------
# -v/--version: show version
# ---------------------------------------------------------------------------


def version_callback(value: bool):
    if value:
        typer.echo(f'wxc-cli version: {__version__}')
        raise typer.Exit()


@app.callback()
def cb_main(
    version: bool = typer.Option(
        None,
        '--version',
        '-v',
        help='Show the application version and exit.',
        callback=version_callback,
        is_eager=True,
    ),
):
    pass


def main():
    app(prog_name=_PROG_NAME)


if __name__ == '__main__':
    main()
