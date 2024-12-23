# Copyright (c) 2024 Snowflake Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import sys
from typing import TextIO

from click import ClickException
from snowflake.cli.api.constants import ObjectType
from snowflake.cli.api.exceptions import ObjectAlreadyExistsError
from snowflake.cli.api.project.util import unquote_identifier
from snowflake.connector.errors import ProgrammingError

if not sys.stdout.closed and sys.stdout.isatty():
    GREEN = "\033[32m"
    BLUE = "\033[34m"
    ORANGE = "\033[38:2:238:76:44m"
    GRAY = "\033[2m"
    ENDC = "\033[0m"
else:
    GREEN = ""
    ORANGE = ""
    BLUE = ""
    GRAY = ""
    ENDC = ""


def _prefix_line(prefix: str, line: str) -> str:
    """
    _prefix_line ensure the prefix is still present even when dealing with return characters
    """
    if "\r" in line:
        line = line.replace("\r", f"\r{prefix}")
    if "\n" in line[:-1]:
        line = line[:-1].replace("\n", f"\n{prefix}") + line[-1:]
    if not line.startswith("\r"):
        line = f"{prefix}{line}"
    return line


def print_log_lines(file: TextIO, name, identifier, logs):
    prefix = f"{GREEN}{name}/{identifier}{ENDC} "
    logs = logs[0:-1]
    for log in logs:
        print(_prefix_line(prefix, log + "\n"), file=file, end="", flush=True)


def strip_empty_lines(lines: list[str]) -> str:
    return "\n".join(stripped for l in lines if (stripped := l.strip()))


def validate_and_set_instances(min_instances, max_instances, instance_name):
    """
    Used to validate that min_instances is positive and that max_instances is not less than min_instances. In the
    case that max_instances is none, sets it equal to min_instances by default. Used like `max_instances =
    validate_and_set_instances(min_instances, max_instances, "name")`.
    """
    if min_instances < 1:
        raise ClickException(f"min_{instance_name} must be positive")

    if max_instances is None:
        max_instances = min_instances
    elif max_instances < min_instances:
        raise ClickException(
            f"max_{instance_name} must be greater or equal to min_{instance_name}"
        )
    return max_instances


def handle_object_already_exists(
    error: ProgrammingError,
    object_type: ObjectType,
    object_name: str,
    replace_available: bool = False,
):
    if error.errno == 2002:
        raise ObjectAlreadyExistsError(
            object_type=object_type,
            name=unquote_identifier(object_name),
            replace_available=replace_available,
        )
    else:
        raise error


def filter_log_timestamp(log: str, include_timestamps: bool) -> str:
    if include_timestamps:
        return log
    else:
        return log.split(" ", 1)[1] if " " in log else log


def new_logs_only(prev_log_records: list[str], new_log_records: list[str]) -> list[str]:
    # Sort the log records, we get time-ordered logs
    # due to ISO 8601 timestamp format in the log content
    # eg: 2024-10-22T01:12:29.873896187Z Count: 1
    new_log_records_sorted = sorted(new_log_records)

    # Get the first new log record to establish the overlap point
    first_new_log_record = new_log_records_sorted[0]

    # Traverse previous logs in reverse and remove duplicates from new logs
    for prev_log in reversed(prev_log_records):
        # Stop if the previous log is earlier than the first new log
        if prev_log < first_new_log_record:
            break

        # Remove matching previous logs from the new logs list
        if prev_log in new_log_records_sorted:
            new_log_records_sorted.remove(prev_log)

    return new_log_records_sorted


class NoPropertiesProvidedError(ClickException):
    pass
