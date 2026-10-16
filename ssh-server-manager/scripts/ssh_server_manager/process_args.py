"""Preserve literal -F operands at native Windows -> MSYS/Cygwin boundaries."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


_STARTUP_SPECIAL = "${}[]*?'"


def _is_msys_executable(executable: str) -> bool:
    try:
        selected = Path(shutil.which(executable) or executable).resolve()
        return selected.is_file() and any(
            (selected.parent / name).is_file() for name in ("msys-2.0.dll", "cygwin1.dll")
        )
    except OSError:
        return False


def _double_quoted_arg(value: str) -> str:
    """CRT quoting with outer double quotes even when there is no whitespace."""
    result = ['"']
    backslashes = 0
    for character in value:
        if character == "\\":
            backslashes += 1
            continue
        result.append("\\" * (backslashes * 2 + 1 if character == '"' else backslashes))
        result.append(character)
        backslashes = 0
    result.extend(["\\" * (backslashes * 2), '"'])
    return "".join(result)


def literal_config_args(command: list[str]) -> list[str] | str:
    """Quote only special -F operands for an actual Windows POSIX runtime.

    Python's Windows list serialization does not quote no-whitespace paths.
    MSYS/Cygwin startup then expands braces/globs and treats apostrophes as
    quotes. Its double-quoted startup words preserve those literal characters,
    including config paths and ssh-keygen's -F host lookup names.
    Leave every other operand under the existing list2cmdline rules, including
    remote commands. This is a shell=False call-boundary adapter, not a shell
    command builder; native clients and POSIX retain the original list.
    """
    if os.name != "nt" or not command:
        return command
    try:
        operand = command.index("-F") + 1
    except ValueError:
        return command
    if (operand >= len(command)
            or not any(character in command[operand] for character in _STARTUP_SPECIAL)
            or not _is_msys_executable(command[0])):
        return command
    return " ".join(
        _double_quoted_arg(value) if index == operand else subprocess.list2cmdline([value])
        for index, value in enumerate(command)
    )
