from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent
from typing import List, Literal, Optional, Union

import typer
from click import BadOptionUsage, ClickException
from pydantic import Field, field_validator
from snowflake.cli._plugins.nativeapp.artifacts import (
    BundleMap,
    build_bundle,
    find_version_info_in_manifest_file,
)
from snowflake.cli._plugins.nativeapp.bundle_context import BundleContext
from snowflake.cli._plugins.nativeapp.codegen.compiler import NativeAppCompiler
from snowflake.cli._plugins.nativeapp.constants import (
    ALLOWED_SPECIAL_COMMENTS,
    COMMENT_COL,
    EXTERNAL_DISTRIBUTION,
    INTERNAL_DISTRIBUTION,
    NAME_COL,
    OWNER_COL,
    PATCH_COL,
    SPECIAL_COMMENT,
    VERSION_COL,
)
from snowflake.cli._plugins.nativeapp.exceptions import (
    ApplicationPackageAlreadyExistsError,
    ApplicationPackageDoesNotExistError,
    CouldNotDropApplicationPackageWithVersions,
    ObjectPropertyNotFoundError,
    SetupScriptFailedValidation,
)
from snowflake.cli._plugins.nativeapp.policy import (
    AllowAlwaysPolicy,
    AskAlwaysPolicy,
    DenyAlwaysPolicy,
    PolicyBase,
)
from snowflake.cli._plugins.nativeapp.utils import needs_confirmation
from snowflake.cli._plugins.stage.diff import DiffResult
from snowflake.cli._plugins.stage.manager import StageManager
from snowflake.cli._plugins.workspace.context import ActionContext
from snowflake.cli.api.cli_global_context import get_cli_context
from snowflake.cli.api.entities.common import EntityBase, get_sql_executor
from snowflake.cli.api.entities.utils import (
    drop_generic_object,
    execute_post_deploy_hooks,
    generic_sql_error_handler,
    sync_deploy_root_with_stage,
    validation_item_to_str,
)
from snowflake.cli.api.errno import DOES_NOT_EXIST_OR_NOT_AUTHORIZED
from snowflake.cli.api.exceptions import SnowflakeSQLExecutionError
from snowflake.cli.api.metrics import CLICounterField
from snowflake.cli.api.project.schemas.entities.common import (
    EntityModelBase,
    Identifier,
    PostDeployHook,
)
from snowflake.cli.api.project.schemas.updatable_model import (
    DiscriminatorField,
    IdentifierField,
)
from snowflake.cli.api.project.schemas.v1.native_app.package import DistributionOptions
from snowflake.cli.api.project.schemas.v1.native_app.path_mapping import PathMapping
from snowflake.cli.api.project.util import (
    append_test_resource_suffix,
    extract_schema,
    identifier_to_show_like_pattern,
    to_identifier,
    unquote_identifier,
)
from snowflake.cli.api.utils.cursor import find_all_rows
from snowflake.connector import DictCursor, ProgrammingError
from snowflake.connector.cursor import SnowflakeCursor


class ApplicationPackageEntityModel(EntityModelBase):
    type: Literal["application package"] = DiscriminatorField()  # noqa: A003
    artifacts: List[Union[PathMapping, str]] = Field(
        title="List of paths or file source/destination pairs to add to the deploy root",
    )
    bundle_root: Optional[str] = Field(
        title="Folder at the root of your project where artifacts necessary to perform the bundle step are stored.",
        default="output/bundle/",
    )
    deploy_root: Optional[str] = Field(
        title="Folder at the root of your project where the build step copies the artifacts",
        default="output/deploy/",
    )
    generated_root: Optional[str] = Field(
        title="Subdirectory of the deploy root where files generated by the Snowflake CLI will be written.",
        default="__generated/",
    )
    stage: Optional[str] = IdentifierField(
        title="Identifier of the stage that stores the application artifacts.",
        default="app_src.stage",
    )
    scratch_stage: Optional[str] = IdentifierField(
        title="Identifier of the stage that stores temporary scratch data used by the Snowflake CLI.",
        default="app_src.stage_snowflake_cli_scratch",
    )
    distribution: Optional[DistributionOptions] = Field(
        title="Distribution of the application package created by the Snowflake CLI",
        default="internal",
    )
    manifest: str = Field(
        title="Path to manifest.yml",
    )

    @field_validator("identifier")
    @classmethod
    def append_test_resource_suffix_to_identifier(
        cls, input_value: Identifier | str
    ) -> Identifier | str:
        identifier = (
            input_value.name if isinstance(input_value, Identifier) else input_value
        )
        with_suffix = append_test_resource_suffix(identifier)
        if isinstance(input_value, Identifier):
            return input_value.model_copy(update=dict(name=with_suffix))
        return with_suffix

    @field_validator("artifacts")
    @classmethod
    def transform_artifacts(
        cls, orig_artifacts: List[Union[PathMapping, str]]
    ) -> List[PathMapping]:
        transformed_artifacts = []
        if orig_artifacts is None:
            return transformed_artifacts

        for artifact in orig_artifacts:
            if isinstance(artifact, PathMapping):
                transformed_artifacts.append(artifact)
            else:
                transformed_artifacts.append(PathMapping(src=artifact))

        return transformed_artifacts


class ApplicationPackageEntity(EntityBase[ApplicationPackageEntityModel]):
    """
    A Native App application package.
    """

    @property
    def project_root(self) -> Path:
        return self._workspace_ctx.project_root

    @property
    def deploy_root(self) -> Path:
        return self.project_root / self._entity_model.deploy_root

    @property
    def bundle_root(self) -> Path:
        return self.project_root / self._entity_model.bundle_root

    @property
    def generated_root(self) -> Path:
        return self.deploy_root / self._entity_model.generated_root

    @property
    def name(self) -> str:
        return self._entity_model.fqn.name

    @property
    def role(self) -> str:
        model = self._entity_model
        return (model.meta and model.meta.role) or self._workspace_ctx.default_role

    @property
    def warehouse(self) -> str:
        model = self._entity_model
        return (
            model.meta and model.meta.warehouse and to_identifier(model.meta.warehouse)
        ) or to_identifier(self._workspace_ctx.default_warehouse)

    @property
    def stage_fqn(self) -> str:
        return f"{self.name}.{self._entity_model.stage}"

    @property
    def scratch_stage_fqn(self) -> str:
        return f"{self.name}.{self._entity_model.scratch_stage}"

    @property
    def post_deploy_hooks(self) -> list[PostDeployHook] | None:
        model = self._entity_model
        return model.meta and model.meta.post_deploy

    def action_bundle(self, action_ctx: ActionContext, *args, **kwargs):
        return self._bundle()

    def action_deploy(
        self,
        action_ctx: ActionContext,
        prune: bool,
        recursive: bool,
        paths: List[Path],
        validate: bool,
        interactive: bool,
        force: bool,
        stage_fqn: Optional[str] = None,
        *args,
        **kwargs,
    ):
        return self._deploy(
            bundle_map=None,
            prune=prune,
            recursive=recursive,
            paths=paths,
            print_diff=True,
            validate=validate,
            stage_fqn=stage_fqn or self.stage_fqn,
            interactive=interactive,
            force=force,
        )

    def action_drop(self, action_ctx: ActionContext, force_drop: bool, *args, **kwargs):
        console = self._workspace_ctx.console
        sql_executor = get_sql_executor()
        needs_confirm = True

        # 1. If existing application package is not found, exit gracefully
        show_obj_row = self.get_existing_app_pkg_info()
        if show_obj_row is None:
            console.warning(
                f"Role {self.role} does not own any application package with the name {self.name}, or the application package does not exist."
            )
            return

        with sql_executor.use_role(self.role):
            # 2. Check for versions in the application package
            show_versions_query = f"show versions in application package {self.name}"
            show_versions_cursor = sql_executor.execute_query(
                show_versions_query, cursor_class=DictCursor
            )
            if show_versions_cursor.rowcount is None:
                raise SnowflakeSQLExecutionError(show_versions_query)

            if show_versions_cursor.rowcount > 0:
                # allow dropping a package with versions when --force is set
                if not force_drop:
                    raise CouldNotDropApplicationPackageWithVersions(
                        "Drop versions first, or use --force to override."
                    )

        # 3. Check distribution of the existing application package
        actual_distribution = self.get_app_pkg_distribution_in_snowflake()
        if not self.verify_project_distribution():
            console.warning(
                f"Dropping application package {self.name} with distribution '{actual_distribution}'."
            )

        # 4. If distribution is internal, check if created by the Snowflake CLI
        row_comment = show_obj_row[COMMENT_COL]
        if actual_distribution == INTERNAL_DISTRIBUTION:
            if row_comment in ALLOWED_SPECIAL_COMMENTS:
                needs_confirm = False
            else:
                if needs_confirmation(needs_confirm, force_drop):
                    console.warning(
                        f"Application package {self.name} was not created by Snowflake CLI."
                    )
        else:
            if needs_confirmation(needs_confirm, force_drop):
                console.warning(
                    f"Application package {self.name} in your Snowflake account has distribution property '{EXTERNAL_DISTRIBUTION}' and could be associated with one or more of your listings on Snowflake Marketplace."
                )

        if needs_confirmation(needs_confirm, force_drop):
            should_drop_object = typer.confirm(
                dedent(
                    f"""\
                        Application package details:
                        Name: {self.name}
                        Created on: {show_obj_row["created_on"]}
                        Distribution: {actual_distribution}
                        Owner: {show_obj_row[OWNER_COL]}
                        Comment: {show_obj_row[COMMENT_COL]}
                        Are you sure you want to drop it?
                    """
                )
            )
            if not should_drop_object:
                console.message(f"Did not drop application package {self.name}.")
                return  # The user desires to keep the application package, therefore exit gracefully

        # All validations have passed, drop object
        drop_generic_object(
            console=console,
            object_type="application package",
            object_name=(self.name),
            role=(self.role),
        )

    def action_validate(
        self,
        action_ctx: ActionContext,
        interactive: bool,
        force: bool,
        use_scratch_stage: bool = True,
        *args,
        **kwargs,
    ):
        self.validate_setup_script(
            use_scratch_stage=use_scratch_stage,
            interactive=interactive,
            force=force,
        )
        self._workspace_ctx.console.message("Setup script is valid")

    def action_version_list(
        self, action_ctx: ActionContext, *args, **kwargs
    ) -> SnowflakeCursor:
        """
        Get all existing versions, if defined, for an application package.
        It executes a 'show versions in application package' query and returns all the results.
        """
        sql_executor = get_sql_executor()
        with sql_executor.use_role(self.role):
            show_obj_query = f"show versions in application package {self.name}"
            show_obj_cursor = sql_executor.execute_query(show_obj_query)

            if show_obj_cursor.rowcount is None:
                raise SnowflakeSQLExecutionError(show_obj_query)

            return show_obj_cursor

    def action_version_create(
        self,
        action_ctx: ActionContext,
        version: Optional[str],
        patch: Optional[int],
        skip_git_check: bool,
        interactive: bool,
        force: bool,
        *args,
        **kwargs,
    ):
        """
        Perform bundle, application package creation, stage upload, version and/or patch to an application package.
        """
        console = self._workspace_ctx.console

        if force:
            policy = AllowAlwaysPolicy()
        elif interactive:
            policy = AskAlwaysPolicy()
        else:
            policy = DenyAlwaysPolicy()

        if skip_git_check:
            git_policy = DenyAlwaysPolicy()
        else:
            git_policy = AllowAlwaysPolicy()

        # Make sure version is not None before proceeding any further.
        # This will raise an exception if version information is not found. Patch can be None.
        bundle_map = None
        if not version:
            console.message(
                dedent(
                    f"""\
                        Version was not provided through the Snowflake CLI. Checking version in the manifest.yml instead.
                        This step will bundle your app artifacts to determine the location of the manifest.yml file.
                    """
                )
            )
            bundle_map = self._bundle()
            version, patch = find_version_info_in_manifest_file(self.deploy_root)
            if not version:
                raise ClickException(
                    "Manifest.yml file does not contain a value for the version field."
                )

        # Check if --patch needs to throw a bad option error, either if application package does not exist or if version does not exist
        if patch is not None:
            try:
                if not self.get_existing_version_info(version):
                    raise BadOptionUsage(
                        option_name="patch",
                        message=f"Cannot create a custom patch when version {version} is not defined in the application package {self.name}. Try again without using --patch.",
                    )
            except ApplicationPackageDoesNotExistError as app_err:
                raise BadOptionUsage(
                    option_name="patch",
                    message=f"Cannot create a custom patch when application package {self.name} does not exist. Try again without using --patch.",
                )

        if git_policy.should_proceed():
            self.check_index_changes_in_git_repo(policy=policy, interactive=interactive)

        self._deploy(
            bundle_map=bundle_map,
            prune=True,
            recursive=True,
            paths=[],
            print_diff=True,
            validate=True,
            stage_fqn=self.stage_fqn,
            interactive=interactive,
            force=force,
        )

        # Warn if the version exists in a release directive(s)
        existing_release_directives = (
            self.get_existing_release_directive_info_for_version(version)
        )

        if existing_release_directives:
            release_directive_names = ", ".join(
                row["name"] for row in existing_release_directives
            )
            console.warning(
                dedent(
                    f"""\
                    Version {version} already defined in application package {self.name} and in release directive(s): {release_directive_names}.
                    """
                )
            )

            user_prompt = (
                f"Are you sure you want to create a new patch for version {version} in application "
                f"package {self.name}? Once added, this operation cannot be undone."
            )
            if not policy.should_proceed(user_prompt):
                if interactive:
                    console.message("Not creating a new patch.")
                    raise typer.Exit(0)
                else:
                    console.message(
                        "Cannot create a new patch non-interactively without --force."
                    )
                    raise typer.Exit(1)

        # Define a new version in the application package
        if not self.get_existing_version_info(version):
            self.add_new_version(version)
            return  # A new version created automatically has patch 0, we do not need to further increment the patch.

        # Add a new patch to an existing (old) version
        self.add_new_patch_to_version(version=version, patch=patch)

    def action_version_drop(
        self,
        action_ctx: ActionContext,
        version: Optional[str],
        interactive: bool,
        force: bool,
        *args,
        **kwargs,
    ):
        """
        Drops a version defined in an application package. If --force is provided, then no user prompts will be executed.
        """
        console = self._workspace_ctx.console

        if force:
            interactive = False
            policy = AllowAlwaysPolicy()
        else:
            policy = AskAlwaysPolicy() if interactive else DenyAlwaysPolicy()

        # 1. Check for existing an existing application package
        show_obj_row = self.get_existing_app_pkg_info()
        if not show_obj_row:
            raise ApplicationPackageDoesNotExistError(self.name)

        # 2. Check distribution of the existing application package
        actual_distribution = self.get_app_pkg_distribution_in_snowflake()
        if not self.verify_project_distribution(
            expected_distribution=actual_distribution
        ):
            console.warning(
                f"Continuing to execute version drop on application package "
                f"{self.name} with distribution '{actual_distribution}'."
            )

        # 3. If the user did not pass in a version string, determine from manifest.yml
        if not version:
            console.message(
                dedent(
                    f"""\
                        Version was not provided through the Snowflake CLI. Checking version in the manifest.yml instead.
                        This step will bundle your app artifacts to determine the location of the manifest.yml file.
                    """
                )
            )
            self._bundle()
            version, _ = find_version_info_in_manifest_file(self.deploy_root)
            if not version:
                raise ClickException(
                    "Manifest.yml file does not contain a value for the version field."
                )

        # Make the version a valid identifier, adding quotes if necessary
        version = to_identifier(version)

        console.step(
            f"About to drop version {version} in application package {self.name}."
        )

        # If user did not provide --force, ask for confirmation
        user_prompt = (
            f"Are you sure you want to drop version {version} "
            f"in application package {self.name}? "
            f"Once dropped, this operation cannot be undone."
        )
        if not policy.should_proceed(user_prompt):
            if interactive:
                console.message("Not dropping version.")
                raise typer.Exit(0)
            else:
                console.message(
                    "Cannot drop version non-interactively without --force."
                )
                raise typer.Exit(1)

        # Drop the version
        sql_executor = get_sql_executor()
        with sql_executor.use_role(self.role):
            try:
                sql_executor.execute_query(
                    f"alter application package {self.name} drop version {version}"
                )
            except ProgrammingError as err:
                raise err  # e.g. version is referenced in a release directive(s)

        console.message(
            f"Version {version} in application package {self.name} dropped successfully."
        )

    def _bundle(self):
        model = self._entity_model
        bundle_map = build_bundle(self.project_root, self.deploy_root, model.artifacts)
        bundle_context = BundleContext(
            package_name=self.name,
            artifacts=model.artifacts,
            project_root=self.project_root,
            bundle_root=self.bundle_root,
            deploy_root=self.deploy_root,
            generated_root=self.generated_root,
        )
        compiler = NativeAppCompiler(bundle_context)
        compiler.compile_artifacts()
        return bundle_map

    def _deploy(
        self,
        bundle_map: BundleMap | None,
        prune: bool,
        recursive: bool,
        paths: list[Path],
        print_diff: bool,
        validate: bool,
        stage_fqn: str,
        interactive: bool,
        force: bool,
        run_post_deploy_hooks: bool = True,
    ) -> DiffResult:
        model = self._entity_model
        workspace_ctx = self._workspace_ctx
        if force:
            policy = AllowAlwaysPolicy()
        elif interactive:
            policy = AskAlwaysPolicy()
        else:
            policy = DenyAlwaysPolicy()

        console = workspace_ctx.console
        stage_fqn = stage_fqn or self.stage_fqn

        # 1. Create a bundle if one wasn't passed in
        bundle_map = bundle_map or self._bundle()

        # 2. Create an empty application package, if none exists
        try:
            self.create_app_package()
        except ApplicationPackageAlreadyExistsError as e:
            console.warning(e.message)
            if not policy.should_proceed("Proceed with using this package?"):
                raise typer.Abort() from e

        with get_sql_executor().use_role(self.role):
            # 3. Upload files from deploy root local folder to the above stage
            stage_schema = extract_schema(stage_fqn)
            diff = sync_deploy_root_with_stage(
                console=console,
                deploy_root=self.deploy_root,
                package_name=self.name,
                stage_schema=stage_schema,
                bundle_map=bundle_map,
                role=self.role,
                prune=prune,
                recursive=recursive,
                stage_fqn=stage_fqn,
                local_paths_to_sync=paths,
                print_diff=print_diff,
            )

            if run_post_deploy_hooks:
                self.execute_post_deploy_hooks()

        if validate:
            self.validate_setup_script(
                use_scratch_stage=False,
                interactive=interactive,
                force=force,
            )

        return diff

    def get_existing_version_info(self, version: str) -> Optional[dict]:
        """
        Get the latest patch on an existing version by name in the application package.
        Executes 'show versions like ... in application package' query and returns
        the latest patch in the version as a single row, if one exists. Otherwise,
        returns None.
        """
        sql_executor = get_sql_executor()
        with sql_executor.use_role(self.role):
            try:
                query = f"show versions like {identifier_to_show_like_pattern(version)} in application package {self.name}"
                cursor = sql_executor.execute_query(query, cursor_class=DictCursor)

                if cursor.rowcount is None:
                    raise SnowflakeSQLExecutionError(query)

                matching_rows = find_all_rows(
                    cursor, lambda row: row[VERSION_COL] == unquote_identifier(version)
                )

                if not matching_rows:
                    return None

                return max(matching_rows, key=lambda row: row[PATCH_COL])

            except ProgrammingError as err:
                if err.msg.__contains__("does not exist or not authorized"):
                    raise ApplicationPackageDoesNotExistError(self.name)
                else:
                    generic_sql_error_handler(err=err)
                    return None

    def get_existing_release_directive_info_for_version(
        self, version: str
    ) -> List[dict]:
        """
        Get all existing release directives, if present, set on the version defined in an application package.
        It executes a 'show release directives in application package' query and returns the filtered results, if they exist.
        """
        sql_executor = get_sql_executor()
        with sql_executor.use_role(self.role):
            show_obj_query = (
                f"show release directives in application package {self.name}"
            )
            show_obj_cursor = sql_executor.execute_query(
                show_obj_query, cursor_class=DictCursor
            )

            if show_obj_cursor.rowcount is None:
                raise SnowflakeSQLExecutionError(show_obj_query)

            show_obj_rows = find_all_rows(
                show_obj_cursor,
                lambda row: row[VERSION_COL] == unquote_identifier(version),
            )

            return show_obj_rows

    def add_new_version(self, version: str) -> None:
        """
        Defines a new version in an existing application package.
        """
        console = self._workspace_ctx.console

        # Make the version a valid identifier, adding quotes if necessary
        version = to_identifier(version)
        sql_executor = get_sql_executor()
        with sql_executor.use_role(self.role):
            console.step(
                f"Defining a new version {version} in application package {self.name}"
            )
            add_version_query = dedent(
                f"""\
                    alter application package {self.name}
                        add version {version}
                        using @{self.stage_fqn}
                """
            )
            sql_executor.execute_query(add_version_query, cursor_class=DictCursor)
            console.message(
                f"Version {version} created for application package {self.name}."
            )

    def add_new_patch_to_version(self, version: str, patch: Optional[int] = None):
        """
        Add a new patch, optionally a custom one, to an existing version in an application package.
        """
        console = self._workspace_ctx.console

        # Make the version a valid identifier, adding quotes if necessary
        version = to_identifier(version)
        sql_executor = get_sql_executor()
        with sql_executor.use_role(self.role):
            console.step(
                f"Adding new patch to version {version} defined in application package {self.name}"
            )
            add_version_query = dedent(
                f"""\
                    alter application package {self.name}
                        add patch {patch if patch else ""} for version {version}
                        using @{self.stage_fqn}
                """
            )
            result_cursor = sql_executor.execute_query(
                add_version_query, cursor_class=DictCursor
            )

            show_row = result_cursor.fetchall()[0]
            new_patch = show_row["patch"]
            console.message(
                f"Patch {new_patch} created for version {version} defined in application package {self.name}."
            )

    def check_index_changes_in_git_repo(
        self, policy: PolicyBase, interactive: bool
    ) -> None:
        """
        Checks if the project root, i.e. the native apps project is a git repository. If it is a git repository,
        it also checks if there any local changes to the directory that may not be on the application package stage.
        """

        from git import Repo
        from git.exc import InvalidGitRepositoryError

        console = self._workspace_ctx.console

        try:
            repo = Repo(self.project_root, search_parent_directories=True)
            assert repo.git_dir is not None

            # Check if the repo has any changes, including untracked files
            if repo.is_dirty(untracked_files=True):
                console.warning(
                    "Changes detected in the git repository. "
                    "(Rerun your command with --skip-git-check flag to ignore this check)"
                )
                repo.git.execute(["git", "status"])

                user_prompt = (
                    "You have local changes in this repository that are not part of a previous commit. "
                    "Do you still want to continue?"
                )
                if not policy.should_proceed(user_prompt):
                    if interactive:
                        console.message("Not creating a new version.")
                        raise typer.Exit(0)
                    else:
                        console.message(
                            "Cannot create a new version non-interactively without --force."
                        )
                        raise typer.Exit(1)

        except InvalidGitRepositoryError:
            pass  # not a git repository, which is acceptable

    def get_existing_app_pkg_info(self) -> Optional[dict]:
        """
        Check for an existing application package by the same name as in project definition, in account.
        It executes a 'show application packages like' query and returns the result as single row, if one exists.
        """
        sql_executor = get_sql_executor()
        with sql_executor.use_role(self.role):
            return sql_executor.show_specific_object(
                "application packages", self.name, name_col=NAME_COL
            )

    def get_app_pkg_distribution_in_snowflake(self) -> str:
        """
        Returns the 'distribution' attribute of a 'describe application package' SQL query, in lowercase.
        """
        sql_executor = get_sql_executor()
        with sql_executor.use_role(self.role):
            try:
                desc_cursor = sql_executor.execute_query(
                    f"describe application package {self.name}"
                )
            except ProgrammingError as err:
                generic_sql_error_handler(err)

            if desc_cursor.rowcount is None or desc_cursor.rowcount == 0:
                raise SnowflakeSQLExecutionError()
            else:
                for row in desc_cursor:
                    if row[0].lower() == "distribution":
                        return row[1].lower()
        raise ObjectPropertyNotFoundError(
            property_name="distribution",
            object_type="application package",
            object_name=self.name,
        )

    def verify_project_distribution(
        self,
        expected_distribution: Optional[str] = None,
    ) -> bool:
        """
        Returns true if the 'distribution' attribute of an existing application package in snowflake
        is the same as the the attribute specified in project definition file.
        """
        model = self._entity_model
        workspace_ctx = self._workspace_ctx

        actual_distribution = (
            expected_distribution
            if expected_distribution
            else self.get_app_pkg_distribution_in_snowflake()
        )
        project_def_distribution = model.distribution.lower()
        if actual_distribution != project_def_distribution:
            workspace_ctx.console.warning(
                dedent(
                    f"""\
                    Application package {self.name} in your Snowflake account has distribution property {actual_distribution},
                    which does not match the value specified in project definition file: {project_def_distribution}.
                    """
                )
            )
            return False
        return True

    @contextmanager
    def use_package_warehouse(self):
        if self.warehouse:
            with get_sql_executor().use_warehouse(self.warehouse):
                yield
        else:
            raise ClickException(
                dedent(
                    f"""\
                Application package warehouse cannot be empty.
                Please provide a value for it in your connection information or your project definition file.
                """
                )
            )

    def create_app_package(self) -> None:
        """
        Creates the application package with our up-to-date stage if none exists.
        """
        model = self._entity_model
        console = self._workspace_ctx.console

        # 1. Check for existing application package
        show_obj_row = self.get_existing_app_pkg_info()

        if show_obj_row:
            # 2. Check distribution of the existing application package
            actual_distribution = self.get_app_pkg_distribution_in_snowflake()
            if not self.verify_project_distribution(
                expected_distribution=actual_distribution
            ):
                console.warning(
                    f"Continuing to execute `snow app run` on application package {self.name} with distribution '{actual_distribution}'."
                )

            # 3. If actual_distribution is external, skip comment check
            if actual_distribution == INTERNAL_DISTRIBUTION:
                row_comment = show_obj_row[COMMENT_COL]

                if row_comment not in ALLOWED_SPECIAL_COMMENTS:
                    raise ApplicationPackageAlreadyExistsError(self.name)

            return

        # If no application package pre-exists, create an application package, with the specified distribution in the project definition file.
        sql_executor = get_sql_executor()
        with sql_executor.use_role(self.role):
            console.step(f"Creating new application package {self.name} in account.")
            sql_executor.execute_query(
                dedent(
                    f"""\
                    create application package {self.name}
                        comment = {SPECIAL_COMMENT}
                        distribution = {model.distribution}
                """
                )
            )

    def execute_post_deploy_hooks(self):
        console = self._workspace_ctx.console

        get_cli_context().metrics.set_counter_default(
            CLICounterField.POST_DEPLOY_SCRIPTS, 0
        )

        if self.post_deploy_hooks:
            with self.use_package_warehouse():
                execute_post_deploy_hooks(
                    console=console,
                    project_root=self.project_root,
                    post_deploy_hooks=self.post_deploy_hooks,
                    deployed_object_type="application package",
                    database_name=self.name,
                )

    def validate_setup_script(
        self, use_scratch_stage: bool, interactive: bool, force: bool
    ):
        workspace_ctx = self._workspace_ctx
        console = workspace_ctx.console

        """Validates Native App setup script SQL."""
        with console.phase(f"Validating Snowflake Native App setup script."):
            validation_result = self.get_validation_result(
                use_scratch_stage=use_scratch_stage,
                force=force,
                interactive=interactive,
            )

            # First print warnings, regardless of the outcome of validation
            for warning in validation_result.get("warnings", []):
                console.warning(validation_item_to_str(warning))

            # Then print errors
            for error in validation_result.get("errors", []):
                # Print them as warnings for now since we're going to be
                # revamping CLI output soon
                console.warning(validation_item_to_str(error))

            # Then raise an exception if validation failed
            if validation_result["status"] == "FAIL":
                raise SetupScriptFailedValidation()

    def get_validation_result(
        self, use_scratch_stage: bool, interactive: bool, force: bool
    ):
        """Call system$validate_native_app_setup() to validate deployed Native App setup script."""
        stage_fqn = self.stage_fqn
        if use_scratch_stage:
            stage_fqn = self.scratch_stage_fqn
            self._deploy(
                bundle_map=None,
                prune=True,
                recursive=True,
                paths=[],
                print_diff=False,
                validate=False,
                stage_fqn=self.scratch_stage_fqn,
                interactive=interactive,
                force=force,
                run_post_deploy_hooks=False,
            )
        prefixed_stage_fqn = StageManager.get_standard_stage_prefix(stage_fqn)
        sql_executor = get_sql_executor()
        try:
            cursor = sql_executor.execute_query(
                f"call system$validate_native_app_setup('{prefixed_stage_fqn}')"
            )
        except ProgrammingError as err:
            if err.errno == DOES_NOT_EXIST_OR_NOT_AUTHORIZED:
                raise ApplicationPackageDoesNotExistError(self.name)
            generic_sql_error_handler(err)
        else:
            if not cursor.rowcount:
                raise SnowflakeSQLExecutionError()
            return json.loads(cursor.fetchone()[0])
        finally:
            if use_scratch_stage:
                self._workspace_ctx.console.step(
                    f"Dropping stage {self.scratch_stage_fqn}."
                )
                with sql_executor.use_role(self.role):
                    sql_executor.execute_query(
                        f"drop stage if exists {self.scratch_stage_fqn}"
                    )
