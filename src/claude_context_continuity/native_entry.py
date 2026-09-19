"""cclaude 的原生入口：只为普通交互 TUI 附加会话本地插件。"""
from __future__ import annotations

from functools import lru_cache
import os
import re
import subprocess
import sys


# These native options own their own lifecycle or terminal/stream contract.
_NATIVE_DIRECT_OPTIONS = frozenset({
    "-h", "--help", "-v", "--version", "-p", "--print", "--bare", "--safe-mode",
    "--bg", "--background", "--cloud", "--environment", "--tmux",
})
_OPTION_LINE = re.compile(
    r"^ {2}(?P<names>(?:-{1,2}[A-Za-z][A-Za-z0-9-]*)(?:,\s*-{1,2}[A-Za-z][A-Za-z0-9-]*)*)"
    r"(?:\s+(?P<operand><[^>]+>|\[[^\]]+\]))?"
)
_COMMAND_LINE = re.compile(r"^ {2}(?P<names>[A-Za-z][A-Za-z0-9-]*(?:\|[A-Za-z][A-Za-z0-9-]*)*)(?=\s)")


def _operand_form(value: str | None) -> tuple[str, bool]:
    """Return (none|required|optional, variadic) from a local-help declaration."""
    if value is None:
        return "none", False
    mode = "required" if value.startswith("<") else "optional"
    return mode, value[1:-1].endswith("...")


def _option_token(value: str) -> bool:
    return value.startswith("-") and value != "-"


@lru_cache(maxsize=1)
def _native_cli_shape() -> tuple[frozenset[str], dict[str, tuple[str, bool]]] | None:
    """Read local help only to recognize current native commands and operands.

    This never starts a model session.  If the native CLI cannot describe its
    argv shape, callers preserve its invocation directly rather than guessing
    that an unknown command or option belongs in the continuity TUI.
    """
    try:
        completed = subprocess.run(
            ["claude", "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        return None

    commands: set[str] = set()
    option_forms: dict[str, tuple[str, bool]] = {}
    in_commands = False
    for line in completed.stdout.splitlines():
        if line.strip() == "Commands:":
            in_commands = True
            continue
        if in_commands:
            command = _COMMAND_LINE.match(line)
            if command is not None:
                commands.update(command.group("names").split("|"))
        option = _OPTION_LINE.match(line)
        if option is not None:
            form = _operand_form(option.group("operand"))
            for name in option.group("names").split(","):
                option_forms[name.strip()] = form
    return (frozenset(commands), option_forms) if commands else None


def _option_end(args: list[str], index: int, form: tuple[str, bool]) -> int | None:
    if "=" in args[index]:
        return index + 1
    mode, variadic = form
    next_index = index + 1
    if mode == "none":
        return next_index
    if variadic:
        if next_index >= len(args) or args[next_index] == "--" or _option_token(args[next_index]):
            return None if mode == "required" else next_index
        while next_index < len(args) and args[next_index] != "--" and not _option_token(args[next_index]):
            next_index += 1
        return next_index
    if mode == "required":
        if next_index >= len(args) or args[next_index] == "--":
            return None
        return next_index + 1
    if next_index < len(args) and args[next_index] != "--" and not _option_token(args[next_index]):
        return next_index + 1
    return next_index


def _native_invocation(args: list[str], *, parsed_options=None) -> bool:
    """Return whether the untouched argv must bypass the managed interactive TUI.

    One help-derived token walk recognizes actual options only: ``--`` ends
    option processing, required values may begin with ``-``, optional values do
    not consume the next option, and variadic operands consume their declared
    non-option tail.  It does not rewrite, validate, or authorize argv.  Managed
    launch checks may collect the same parsed options without a second parser.
    """
    shape = _native_cli_shape()
    if shape is None:
        return bool(args)
    commands, option_forms = shape
    index = 0
    saw_noncommand_positional = False
    while index < len(args):
        token = args[index]
        if token == "--":
            return False
        if _option_token(token):
            option = token.split("=", 1)[0]
            form = option_forms.get(option)
            if form is None:
                # An unknown option must retain native parsing/error behavior.
                return True
            if option in _NATIVE_DIRECT_OPTIONS:
                return True
            next_index = _option_end(args, index, form)
            if next_index is None:
                return True
            if parsed_options is not None:
                values = (token.split("=", 1)[1],) if "=" in token else tuple(args[index + 1:next_index])
                parsed_options.append((option, values))
            index = next_index
            continue
        if not saw_noncommand_positional and token in commands:
            return True
        # Claude accepts native options after a positional prompt.  Keep walking
        # those true option tokens, while later ordinary words remain prompt text.
        saw_noncommand_positional = True
        index += 1
    return False


def _direct_native(args: list[str]) -> bool:
    # Pipes/redirects and explicit native simple modes retain exact standard
    # streams, signals, exit status, settings, and all user-provided argv.
    return (
        not sys.stdin.isatty()
        or not sys.stdout.isatty()
        or os.environ.get("CLAUDE_CODE_SAFE_MODE") == "1"
        or os.environ.get("CLAUDE_CODE_SIMPLE") == "1"
        or _native_invocation(args)
    )


def _exec_native(args: list[str]) -> int:
    os.execvp("claude", ["claude", *args])
    return 0  # pragma: no cover - os.execvp only returns in mocked tests.


def main() -> int:
    args = sys.argv[1:]
    if _direct_native(args):
        return _exec_native(args)
    from .tui_runtime import SessionOwnerError, run

    try:
        run(os.getcwd(), native_args=args)
    except SessionOwnerError as exc:
        print(f"cclaude: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
