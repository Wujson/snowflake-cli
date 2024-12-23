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

import logging
from contextlib import contextmanager
from textwrap import dedent
from typing import Any, Dict, List

from snowflake.cli._plugins.connection.util import UIParameter, get_ui_parameter
from snowflake.cli._plugins.nativeapp.constants import (
    AUTHORIZE_TELEMETRY_COL,
    NAME_COL,
    SPECIAL_COMMENT,
)
from snowflake.cli._plugins.nativeapp.same_account_install_method import (
    SameAccountInstallMethod,
)
from snowflake.cli._plugins.nativeapp.sf_facade_constants import UseObjectType
from snowflake.cli._plugins.nativeapp.sf_facade_exceptions import (
    CREATE_OR_UPGRADE_APPLICATION_EXPECTED_USER_ERROR_CODES,
    UPGRADE_RESTRICTION_CODES,
    CouldNotUseObjectError,
    InsufficientPrivilegesError,
    UnexpectedResultError,
    UpgradeApplicationRestrictionError,
    UserInputError,
    UserScriptError,
    handle_unclassified_error,
)
from snowflake.cli.api.cli_global_context import get_cli_context
from snowflake.cli.api.constants import ObjectType
from snowflake.cli.api.errno import (
    APPLICATION_REQUIRES_TELEMETRY_SHARING,
    CANNOT_DISABLE_MANDATORY_TELEMETRY,
    DOES_NOT_EXIST_OR_CANNOT_BE_PERFORMED,
    INSUFFICIENT_PRIVILEGES,
    NO_WAREHOUSE_SELECTED_IN_SESSION,
)
from snowflake.cli.api.identifiers import FQN
from snowflake.cli.api.metrics import CLICounterField
from snowflake.cli.api.project.schemas.v1.native_app.package import DistributionOptions
from snowflake.cli.api.project.util import (
    identifier_to_show_like_pattern,
    is_valid_unquoted_identifier,
    to_identifier,
    to_quoted_identifier,
    to_string_literal,
)
from snowflake.cli.api.sql_execution import BaseSqlExecutor
from snowflake.cli.api.utils.cursor import find_first_row
from snowflake.connector import DictCursor, ProgrammingError


class SnowflakeSQLFacade:
    def __init__(self, sql_executor: BaseSqlExecutor | None = None):
        self._sql_executor = (
            sql_executor if sql_executor is not None else BaseSqlExecutor()
        )
        self._log = logging.getLogger(__name__)

    def _use_object(self, object_type: UseObjectType, name: str):
        """
        Call sql to use snowflake object with error handling
        @param object_type: ObjectType, type of snowflake object to use
        @param name: object name, has to be a valid snowflake identifier.
        """
        try:
            self._sql_executor.execute_query(f"use {object_type} {name}")
        except ProgrammingError as err:
            if err.errno == DOES_NOT_EXIST_OR_CANNOT_BE_PERFORMED:
                raise CouldNotUseObjectError(object_type, name) from err
            else:
                handle_unclassified_error(err, f"Failed to use {object_type} {name}.")
        except Exception as err:
            handle_unclassified_error(err, f"Failed to use {object_type} {name}.")

    @contextmanager
    def _use_object_optional(self, object_type: UseObjectType, name: str | None):
        """
        Call sql to use snowflake object with error handling
        @param object_type: ObjectType, type of snowflake object to use
        @param name: object name, will be cast to a valid snowflake identifier.
        """
        if name is None:
            yield
            return

        try:
            current_obj_result_row = self._sql_executor.execute_query(
                f"select current_{object_type}()"
            ).fetchone()
        except Exception as err:
            return handle_unclassified_error(
                err, f"Failed to select current {object_type}."
            )

        try:
            prev_obj = current_obj_result_row[0]
        except IndexError:
            prev_obj = None

        if prev_obj is not None and _same_identifier(prev_obj, name):
            yield
            return

        self._log.debug(f"Switching to {object_type}: {name}")
        self._use_object(object_type, to_identifier(name))
        try:
            yield
        finally:
            if prev_obj is not None:
                self._log.debug(f"Switching back to {object_type}: {prev_obj}")
                self._use_object(object_type, prev_obj)

    def _use_warehouse_optional(self, new_wh: str | None):
        """
        Switches to a different warehouse for a while, then switches back.
        This is a no-op if the requested warehouse is already active or if no warehouse is passed in.
        @param new_wh: Name of the warehouse to use. If not a valid Snowflake identifier, will be converted before use.
        """
        return self._use_object_optional(UseObjectType.WAREHOUSE, new_wh)

    def _use_role_optional(self, new_role: str | None):
        """
        Switches to a different role for a while, then switches back.
        This is a no-op if the requested role is already active or if no role is passed in.
        @param new_role: Name of the role to use. If not a valid Snowflake identifier, will be converted before use.
        """
        return self._use_object_optional(UseObjectType.ROLE, new_role)

    def _use_database_optional(self, database_name: str | None):
        """
        Switch to database `database_name`, then switches back.
        This is a no-op if the requested database is already selected or if no database_name is passed in.
        @param database_name: Name of the database to use. If not a valid Snowflake identifier, will be converted before use.
        """
        return self._use_object_optional(UseObjectType.DATABASE, database_name)

    def _use_schema_optional(self, schema_name: str | None):
        """
        Switch to schema `schema_name`, then switches back.
        This is a no-op if the requested schema is already selected or if no schema_name is passed in.
        @param schema_name: Name of the schema to use. If not a valid Snowflake identifier, will be converted before use.
        """
        return self._use_object_optional(UseObjectType.SCHEMA, schema_name)

    def grant_privileges_to_role(
        self,
        privileges: list[str],
        object_type: ObjectType,
        object_identifier: str,
        role_to_grant: str,
        role_to_use: str | None = None,
    ) -> None:
        """
        Grants one or more access privileges on a securable object to a role

        @param privileges: List of privileges to grant to a role
        @param object_type: Type of snowflake object to grant to a role
        @param object_identifier: Valid identifier of the snowflake object to grant to a role
        @param role_to_grant: Name of the role to grant privileges to
        @param [Optional] role_to_use: Name of the role to use to grant privileges
        """
        comma_separated_privileges = ", ".join(privileges)
        object_type_and_name = f"{object_type.value.sf_name} {object_identifier}"

        with self._use_role_optional(role_to_use):
            try:
                self._sql_executor.execute_query(
                    f"grant {comma_separated_privileges} on {object_type_and_name} to role {role_to_grant}"
                )
            except Exception as err:
                handle_unclassified_error(
                    err,
                    f"Failed to grant {comma_separated_privileges} on {object_type_and_name} to role {role_to_grant}.",
                )

    def execute_user_script(
        self,
        queries: str,
        script_name: str,
        role: str | None = None,
        warehouse: str | None = None,
        database: str | None = None,
    ):
        """
        Runs the user-provided sql script.
        @param queries: Queries to run in this script
        @param script_name: Name of the file containing the script. Used to show logs to the user.
        @param [Optional] role: Role to switch to while running this script. Current role will be used if no role is passed in.
        @param [Optional] warehouse: Warehouse to use while running this script.
        @param [Optional] database: Database to use while running this script.
        """
        with (
            self._use_role_optional(role),
            self._use_warehouse_optional(warehouse),
            self._use_database_optional(database),
        ):
            try:
                self._sql_executor.execute_queries(queries)
            except ProgrammingError as err:
                if err.errno == NO_WAREHOUSE_SELECTED_IN_SESSION:
                    raise UserScriptError(
                        script_name,
                        f"{err.msg}. Please provide a warehouse in your project definition file, config.toml file, or via command line",
                    ) from err
                else:
                    raise UserScriptError(script_name, err.msg) from err
            except Exception as err:
                handle_unclassified_error(err, f"Failed to run script {script_name}.")

    def get_account_event_table(self, role: str | None = None) -> str | None:
        """
        Returns the name of the event table for the account.
        If the account has no event table set up or the event table is set to NONE, returns None.
        @param [Optional] role: Role to switch to while running this script. Current role will be used if no role is passed in.
        """
        query = "show parameters like 'event_table' in account"
        with self._use_role_optional(role):
            try:
                results = self._sql_executor.execute_query(
                    query, cursor_class=DictCursor
                )
            except Exception as err:
                handle_unclassified_error(err, f"Failed to get event table.")
        table = next((r["value"] for r in results if r["key"] == "EVENT_TABLE"), None)
        if table is None or table == "NONE":
            return None
        return table

    def create_version_in_package(
        self,
        package_name: str,
        stage_fqn: str,
        version: str,
        label: str | None = None,
        role: str | None = None,
    ):
        """
        Creates a new version in an existing application package.
        @param package_name: Name of the application package to alter.
        @param stage_fqn: Stage fully qualified name.
        @param version: Version name to create.
        @param [Optional] role: Switch to this role while executing create version.
        @param [Optional] label: Label for this version, visible to consumers.
        """

        # Make the version a valid identifier, adding quotes if necessary
        version = to_identifier(version)

        # Label must be a string literal
        with_label_cause = (
            f"\nlabel={to_string_literal(label)}" if label is not None else ""
        )
        add_version_query = dedent(
            f"""\
                alter application package {package_name}
                    add version {version}
                    using @{stage_fqn}{with_label_cause}
            """
        )
        with self._use_role_optional(role):
            try:
                self._sql_executor.execute_query(add_version_query)
            except Exception as err:
                handle_unclassified_error(
                    err,
                    f"Failed to add version {version} to application package {package_name}.",
                )

    def add_patch_to_package_version(
        self,
        package_name: str,
        stage_fqn: str,
        version: str,
        patch: int | None = None,
        label: str | None = None,
        role: str | None = None,
    ) -> int:
        """
        Add a new patch, optionally a custom one, to an existing version in an application package.
        @param package_name: Name of the application package to alter.
        @param stage_fqn: Stage fully qualified name.
        @param version: Version name to create.
        @param [Optional] patch: Patch number to create.
        @param [Optional] label: Label for this patch, visible to consumers.
        @param [Optional] role: Switch to this role while executing create version.

        @return patch number created for the version.
        """

        # Make the version a valid identifier, adding quotes if necessary
        version = to_identifier(version)

        # Label must be a string literal
        with_label_clause = (
            f"\nlabel={to_string_literal(label)}" if label is not None else ""
        )
        patch_query = f"{patch}" if patch else ""
        add_patch_query = dedent(
            f"""\
                 alter application package {package_name}
                     add patch {patch_query} for version {version}
                     using @{stage_fqn}{with_label_clause}
             """
        )
        with self._use_role_optional(role):
            try:
                result_cursor = self._sql_executor.execute_query(
                    add_patch_query, cursor_class=DictCursor
                ).fetchall()
            except Exception as err:
                handle_unclassified_error(
                    err,
                    f"Failed to create patch {patch_query} for version {version} in application package {package_name}.",
                )
            try:
                show_row = result_cursor[0]
            except IndexError as err:
                raise UnexpectedResultError(
                    f"Expected to receive the new patch but the result is empty"
                ) from err
            new_patch = show_row["patch"]

        return new_patch

    def get_event_definitions(
        self, app_name: str, role: str | None = None
    ) -> list[dict]:
        """
        Retrieves event definitions for the specified application.
        @param app_name: Name of the application to get event definitions for.
        @return: A list of dictionaries containing event definitions.
        """
        query = (
            f"show telemetry event definitions in application {to_identifier(app_name)}"
        )
        with self._use_role_optional(role):
            try:
                results = self._sql_executor.execute_query(
                    query, cursor_class=DictCursor
                ).fetchall()
            except Exception as err:
                handle_unclassified_error(
                    err,
                    f"Failed to get event definitions for application {to_identifier(app_name)}.",
                )
        return [dict(row) for row in results]

    def get_app_properties(
        self, app_name: str, role: str | None = None
    ) -> Dict[str, str]:
        """
        Retrieve the properties of the specified application.
        @param app_name: Name of the application.
        @return: A dictionary containing the properties of the application.
        """

        query = f"desc application {to_identifier(app_name)}"
        with self._use_role_optional(role):
            try:
                results = self._sql_executor.execute_query(
                    query, cursor_class=DictCursor
                ).fetchall()
            except Exception as err:
                handle_unclassified_error(
                    err, f"Failed to describe application {to_identifier(app_name)}."
                )
        return {row["property"]: row["value"] for row in results}

    def share_telemetry_events(
        self, app_name: str, event_names: List[str], role: str | None = None
    ):
        """
        Shares the specified events from the specified application to the application package provider.
        @param app_name: Name of the application to share events from.
        @param events: List of event names to share.
        """

        self._log.info("sharing events %s", event_names)
        query = f"alter application {to_identifier(app_name)} set shared telemetry events ({', '.join([to_string_literal(x) for x in event_names])})"

        with self._use_role_optional(role):
            try:
                self._sql_executor.execute_query(query)
            except Exception as err:
                handle_unclassified_error(
                    err,
                    f"Failed to share telemetry events for application {to_identifier(app_name)}.",
                )

    def create_schema(
        self, name: str, role: str | None = None, database: str | None = None
    ):
        """
        Creates a schema.
        @param name: Name of the schema to create. Can be a database-qualified name or just the schema name, in which case the current database or the database passed in will be used.
        @param [Optional] role: Role to switch to while running this script. Current role will be used if no role is passed in.
        @param [Optional] database: Database to use while running this query, unless the schema name is database-qualified.
        """
        fqn = FQN.from_string(name)
        identifier = to_identifier(fqn.name)
        database = fqn.prefix or database
        with (
            self._use_role_optional(role),
            self._use_database_optional(database),
        ):
            try:
                self._sql_executor.execute_query(
                    f"create schema if not exists {identifier}"
                )
            except ProgrammingError as err:
                if err.errno == INSUFFICIENT_PRIVILEGES:
                    raise InsufficientPrivilegesError(
                        f"Insufficient privileges to create schema {name}",
                        role=role,
                        database=database,
                    ) from err
                handle_unclassified_error(err, f"Failed to create schema {name}.")

    def stage_exists(
        self,
        name: str,
        role: str | None = None,
        database: str | None = None,
        schema: str | None = None,
    ) -> bool:
        """
        Checks if a stage exists.
        @param name: Name of the stage to check for. Can be a fully qualified name or just the stage name, in which case the current database and schema or the database and schema passed in will be used.
        @param [Optional] role: Role to switch to while running this script. Current role will be used if no role is passed in.
        @param [Optional] database: Database to use while running this script, unless the stage name is database-qualified.
        @param [Optional] schema: Schema to use while running this script, unless the stage name is schema-qualified.
        """
        fqn = FQN.from_string(name)
        identifier = to_identifier(fqn.name)
        database = fqn.database or database
        schema = fqn.schema or schema

        pattern = identifier_to_show_like_pattern(identifier)
        if schema and database:
            in_schema_clause = f" in schema {database}.{schema}"
        elif schema:
            in_schema_clause = f" in schema {schema}"
        elif database:
            in_schema_clause = f" in database {database}"
        else:
            in_schema_clause = ""

        try:
            with self._use_role_optional(role):
                try:
                    results = self._sql_executor.execute_query(
                        f"show stages like {pattern}{in_schema_clause}",
                    )
                except ProgrammingError as err:
                    if err.errno == DOES_NOT_EXIST_OR_CANNOT_BE_PERFORMED:
                        return False
                    if err.errno == INSUFFICIENT_PRIVILEGES:
                        raise InsufficientPrivilegesError(
                            f"Insufficient privileges to check if stage {name} exists",
                            role=role,
                            database=database,
                            schema=schema,
                        ) from err
                    handle_unclassified_error(
                        err, f"Failed to check if stage {name} exists."
                    )
            return results.rowcount > 0
        except CouldNotUseObjectError:
            return False

    def create_stage(
        self,
        name: str,
        encryption_type: str = "SNOWFLAKE_SSE",
        enable_directory: bool = True,
        role: str | None = None,
        database: str | None = None,
        schema: str | None = None,
    ):
        """
        Creates a stage.
        @param name: Name of the stage to create. Can be a fully qualified name or just the stage name, in which case the current database and schema or the database and schema passed in will be used.
        @param [Optional] encryption_type: Encryption type for the stage. Default is Snowflake SSE. Pass an empty string to disable encryption.
        @param [Optional] enable_directory: Directory settings for the stage. Default is enabled.
        @param [Optional] role: Role to switch to while running this script. Current role will be used if no role is passed in.
        @param [Optional] database: Database to use while running this script, unless the stage name is database-qualified.
        @param [Optional] schema: Schema to use while running this script, unless the stage name is schema-qualified.
        """
        fqn = FQN.from_string(name)
        identifier = to_identifier(fqn.name)
        database = fqn.database or database
        schema = fqn.schema or schema

        query = f"create stage if not exists {identifier}"
        if encryption_type:
            query += f" encryption = (type = '{encryption_type}')"
        if enable_directory:
            query += f" directory = (enable = {str(enable_directory)})"
        with (
            self._use_role_optional(role),
            self._use_database_optional(database),
            self._use_schema_optional(schema),
        ):
            try:
                self._sql_executor.execute_query(query)
            except ProgrammingError as err:
                if err.errno == INSUFFICIENT_PRIVILEGES:
                    raise InsufficientPrivilegesError(
                        f"Insufficient privileges to create stage {name}",
                        role=role,
                        database=database,
                        schema=schema,
                    ) from err
                handle_unclassified_error(err, f"Failed to create stage {name}.")

    def show_release_directives(
        self, package_name: str, role: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Show release directives for a package
        @param package_name: Name of the package
        @param [Optional] role: Role to switch to while running this script. Current role will be used if no role is passed in.
        """
        package_identifier = to_identifier(package_name)
        with self._use_role_optional(role):
            try:
                cursor = self._sql_executor.execute_query(
                    f"show release directives in application package {package_identifier}",
                    cursor_class=DictCursor,
                )
            except ProgrammingError as err:
                if err.errno == INSUFFICIENT_PRIVILEGES:
                    raise InsufficientPrivilegesError(
                        f"Insufficient privileges to show release directives for package {package_name}",
                        role=role,
                    ) from err
                handle_unclassified_error(
                    err,
                    f"Failed to show release directives for package {package_name}.",
                )
            return cursor.fetchall()

    def get_existing_app_info(self, name: str, role: str) -> dict | None:
        """
        Check for an existing application object by the same name as in project definition, in account.
        It executes a 'show applications like' query and returns the result as single row, if one exists.
        """
        with self._use_role_optional(role):
            try:
                object_type_plural = ObjectType.APPLICATION.value.sf_plural_name
                show_obj_query = f"show {object_type_plural} like {identifier_to_show_like_pattern(name)}".strip()

                show_obj_cursor = self._sql_executor.execute_query(
                    show_obj_query, cursor_class=DictCursor
                )

                show_obj_row = find_first_row(
                    show_obj_cursor, lambda row: _same_identifier(row[NAME_COL], name)
                )
            except Exception as err:
                handle_unclassified_error(
                    err, f"Unable to fetch information on application {name}."
                )
            return show_obj_row

    def upgrade_application(
        self,
        name: str,
        install_method: SameAccountInstallMethod,
        stage_fqn: str,
        role: str,
        warehouse: str,
        debug_mode: bool | None,
        should_authorize_event_sharing: bool | None,
    ) -> list[tuple[str]]:
        """
        Upgrades an application object using the provided clauses

        @param name: Name of the application object
        @param install_method: Method of installing the application
        @param stage_fqn: FQN of the stage housing the application artifacts
        @param role: Role to use when creating the application and provider-side objects
        @param warehouse: Warehouse which is required to create an application object
        @param debug_mode: Whether to enable debug mode; None means not explicitly enabled or disabled
        @param should_authorize_event_sharing: Whether to enable event sharing; None means not explicitly enabled or disabled
        """
        install_method.ensure_app_usable(
            app_name=name,
            app_role=role,
            show_app_row=self.get_existing_app_info(name, role),
        )
        # If all the above checks are in order, proceed to upgrade

        with self._use_role_optional(role), self._use_warehouse_optional(warehouse):
            try:
                using_clause = install_method.using_clause(stage_fqn)
                upgrade_cursor = self._sql_executor.execute_query(
                    f"alter application {name} upgrade {using_clause}",
                )

                # if debug_mode is present (controlled), ensure it is up-to-date
                if install_method.is_dev_mode:
                    if debug_mode is not None:
                        self._sql_executor.execute_query(
                            f"alter application {name} set debug_mode = {debug_mode}"
                        )
            except ProgrammingError as err:
                if err.errno in UPGRADE_RESTRICTION_CODES:
                    raise UpgradeApplicationRestrictionError(err.msg) from err
                elif (
                    err.errno in CREATE_OR_UPGRADE_APPLICATION_EXPECTED_USER_ERROR_CODES
                ):
                    raise UserInputError(
                        f"Failed to upgrade application {name} with the following error message:\n"
                        f"{err.msg}"
                    ) from err
                handle_unclassified_error(err, f"Failed to upgrade application {name}.")
            except Exception as err:
                handle_unclassified_error(err, f"Failed to upgrade application {name}.")

            try:
                # Only update event sharing if the current value is different as the one we want to set
                if should_authorize_event_sharing is not None:
                    current_authorize_event_sharing = (
                        self.get_app_properties(name, role)
                        .get(AUTHORIZE_TELEMETRY_COL, "false")
                        .lower()
                        == "true"
                    )
                    if (
                        current_authorize_event_sharing
                        != should_authorize_event_sharing
                    ):
                        self._log.info(
                            "Setting telemetry sharing authorization to %s",
                            should_authorize_event_sharing,
                        )
                        self._sql_executor.execute_query(
                            f"alter application {name} set AUTHORIZE_TELEMETRY_EVENT_SHARING = {str(should_authorize_event_sharing).upper()}"
                        )
            except ProgrammingError as err:
                if err.errno == CANNOT_DISABLE_MANDATORY_TELEMETRY:
                    get_cli_context().metrics.set_counter(
                        CLICounterField.EVENT_SHARING_ERROR, 1
                    )
                    raise UserInputError(
                        "Could not disable telemetry event sharing for the application because it contains mandatory events. Please set 'share_mandatory_events' to true in the application telemetry section of the project definition file."
                    ) from err
                handle_unclassified_error(
                    err,
                    f"Failed to set AUTHORIZE_TELEMETRY_EVENT_SHARING when upgrading application {name}.",
                )
            except Exception as err:
                handle_unclassified_error(
                    err,
                    f"Failed to set AUTHORIZE_TELEMETRY_EVENT_SHARING when upgrading application {name}.",
                )

            return upgrade_cursor.fetchall()

    def create_application(
        self,
        name: str,
        package_name: str,
        install_method: SameAccountInstallMethod,
        stage_fqn: str,
        role: str,
        warehouse: str,
        debug_mode: bool | None,
        should_authorize_event_sharing: bool | None,
    ) -> list[tuple[str]]:
        """
        Creates a new application object using an application package,
        running the setup script of the application package

        @param name: Name of the application object
        @param package_name: Name of the application package to install the application from
        @param install_method: Method of installing the application
        @param stage_fqn: FQN of the stage housing the application artifacts
        @param role: Role to use when creating the application and provider-side objects
        @param warehouse: Warehouse which is required to create an application object
        @param debug_mode: Whether to enable debug mode; None means not explicitly enabled or disabled
        @param should_authorize_event_sharing: Whether to enable event sharing; None means not explicitly enabled or disabled
        """

        # by default, applications are created in debug mode when possible;
        # this can be overridden in the project definition
        debug_mode_clause = ""
        if install_method.is_dev_mode:
            initial_debug_mode = debug_mode if debug_mode is not None else True
            debug_mode_clause = f"debug_mode = {initial_debug_mode}"

        authorize_telemetry_clause = ""
        if should_authorize_event_sharing is not None:
            self._log.info(
                "Setting AUTHORIZE_TELEMETRY_EVENT_SHARING to %s",
                should_authorize_event_sharing,
            )
            authorize_telemetry_clause = f" AUTHORIZE_TELEMETRY_EVENT_SHARING = {str(should_authorize_event_sharing).upper()}"

        using_clause = install_method.using_clause(stage_fqn)
        with self._use_role_optional(role), self._use_warehouse_optional(warehouse):
            try:
                create_cursor = self._sql_executor.execute_query(
                    dedent(
                        f"""\
                    create application {name}
                        from application package {package_name} {using_clause} {debug_mode_clause}{authorize_telemetry_clause}
                        comment = {SPECIAL_COMMENT}
                    """
                    ),
                )
            except ProgrammingError as err:
                if err.errno == APPLICATION_REQUIRES_TELEMETRY_SHARING:
                    get_cli_context().metrics.set_counter(
                        CLICounterField.EVENT_SHARING_ERROR, 1
                    )
                    raise UserInputError(
                        "The application package requires event sharing to be authorized. Please set 'share_mandatory_events' to true in the application telemetry section of the project definition file."
                    ) from err
                elif (
                    err.errno in CREATE_OR_UPGRADE_APPLICATION_EXPECTED_USER_ERROR_CODES
                ):
                    raise UserInputError(
                        f"Failed to create application {name} with the following error message:\n"
                        f"{err.msg}"
                    ) from err
                handle_unclassified_error(err, f"Failed to create application {name}.")
            except Exception as err:
                handle_unclassified_error(err, f"Failed to create application {name}.")

            return create_cursor.fetchall()

    def create_application_package(
        self,
        package_name: str,
        distribution: DistributionOptions,
        enable_release_channels: bool | None = None,
        role: str | None = None,
    ) -> None:
        """
        Creates a new application package.
        @param package_name: Name of the application package to create.
        @param [Optional] enable_release_channels: Enable/Disable release channels if not None.
        @param [Optional] role: Role to switch to while running this script. Current role will be used if no role is passed in.
        """
        package_name = to_identifier(package_name)

        enable_release_channels_clause = ""
        if enable_release_channels is not None:
            enable_release_channels_clause = (
                f"enable_release_channels = {str(enable_release_channels).lower()}"
            )

        with self._use_role_optional(role):
            try:
                self._sql_executor.execute_query(
                    dedent(
                        _strip_empty_lines(
                            f"""\
                            create application package {package_name}
                                comment = {SPECIAL_COMMENT}
                                distribution = {distribution}
                                {enable_release_channels_clause}
                            """
                        )
                    )
                )
            except ProgrammingError as err:
                if err.errno == INSUFFICIENT_PRIVILEGES:
                    raise InsufficientPrivilegesError(
                        f"Insufficient privileges to create application package {package_name}",
                        role=role,
                    ) from err
                handle_unclassified_error(
                    err, f"Failed to create application package {package_name}."
                )

    def alter_application_package_properties(
        self,
        package_name: str,
        enable_release_channels: bool | None = None,
        role: str | None = None,
    ) -> None:
        """
        Alters the properties of an existing application package.
        @param package_name: Name of the application package to alter.
        @param [Optional] enable_release_channels: Enable/Disable release channels if not None.
        @param [Optional] role: Role to switch to while running this script. Current role will be used if no role is passed in.
        """

        package_name = to_identifier(package_name)

        if enable_release_channels is not None:
            with self._use_role_optional(role):
                try:
                    self._sql_executor.execute_query(
                        dedent(
                            f"""\
                            alter application package {package_name}
                                set enable_release_channels = {str(enable_release_channels).lower()}
                        """
                        )
                    )
                except ProgrammingError as err:
                    if err.errno == INSUFFICIENT_PRIVILEGES:
                        raise InsufficientPrivilegesError(
                            f"Insufficient privileges update enable_release_channels for application package {package_name}",
                            role=role,
                        ) from err
                    handle_unclassified_error(
                        err,
                        f"Failed to update enable_release_channels for application package {package_name}.",
                    )

    def get_ui_parameter(self, parameter: UIParameter, default: Any) -> Any:
        """
        Returns the value of a single UI parameter.
        If the parameter is not found, the default value is returned.

        @param parameter: UIParameter, the parameter to get the value of.
        @param default: Default value to return if the parameter is not found.
        """
        connection = self._sql_executor._conn  # noqa SLF001

        return get_ui_parameter(connection, parameter, default)


# TODO move this to src/snowflake/cli/api/project/util.py in a separate
# PR since it's codeowned by the CLI team
def _same_identifier(id1: str, id2: str) -> bool:
    """
    Returns whether two identifiers refer to the same object.

    Two unquoted identifiers are considered the same if they are equal when both are converted to uppercase
    Two quoted identifiers are considered the same if they are exactly equal
    An unquoted identifier and a quoted identifier are considered the same
      if the quoted identifier is equal to the unquoted identifier
      when the unquoted identifier is converted to uppercase and quoted
    """
    # Canonicalize the identifiers by converting unquoted identifiers to uppercase and leaving quoted identifiers as is
    canonical_id1 = id1.upper() if is_valid_unquoted_identifier(id1) else id1
    canonical_id2 = id2.upper() if is_valid_unquoted_identifier(id2) else id2

    # The canonical identifiers are equal if they are equal when both are quoted
    # (if they are already quoted, this is a no-op)
    return to_quoted_identifier(canonical_id1) == to_quoted_identifier(canonical_id2)


def _strip_empty_lines(text: str) -> str:
    """
    Strips empty lines from the input string.
    """
    return "\n".join(line for line in text.splitlines() if line.strip())
