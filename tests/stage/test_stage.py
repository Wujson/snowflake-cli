from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from snowcli.cli.stage.manager import StageManager
from snowflake.connector.cursor import DictCursor

from tests.testing_utils.fixtures import *

STAGE_MANAGER = "snowcli.cli.stage.manager.StageManager"


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_list(mock_execute, runner, mock_cursor):
    mock_execute.return_value = mock_cursor(["row"], [])
    result = runner.invoke_with_config(["stage", "list", "-c", "empty", "stageName"])
    assert result.exit_code == 0, result.output
    mock_execute.assert_called_once_with("ls @stageName")


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_get(mock_execute, runner, mock_cursor):
    mock_execute.return_value = mock_cursor(["row"], [])
    with TemporaryDirectory() as tmp_dir:
        result = runner.invoke_with_config(
            ["stage", "get", "-c", "empty", "stageName", str(tmp_dir)]
        )
    assert result.exit_code == 0, result.output
    mock_execute.assert_called_once_with(
        f"get @stageName file://{Path(tmp_dir).resolve()}/"
    )


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_get_default_path(mock_execute, runner, mock_cursor):
    mock_execute.return_value = mock_cursor(["row"], [])
    result = runner.invoke_with_config(["stage", "get", "-c", "empty", "stageName"])
    assert result.exit_code == 0, result.output
    mock_execute.assert_called_once_with(
        f'get @stageName file://{Path(".").resolve()}/'
    )


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_put(mock_execute, runner, mock_cursor):
    mock_execute.return_value = mock_cursor(["row"], [])
    with TemporaryDirectory() as tmp_dir:
        result = runner.invoke_with_config(
            [
                "stage",
                "put",
                "-c",
                "empty",
                "--overwrite",
                "--parallel",
                42,
                str(tmp_dir),
                "stageName",
            ]
        )
    assert result.exit_code == 0, result.output
    mock_execute.assert_called_once_with(
        f"put file://{Path(tmp_dir).resolve()}/* @stageName auto_compress=false parallel=42 overwrite=True"
    )


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_put_star(mock_execute, runner, mock_cursor):
    mock_execute.return_value = mock_cursor(["row"], [])
    with TemporaryDirectory() as tmp_dir:
        result = runner.invoke_with_config(
            [
                "stage",
                "put",
                "-c",
                "empty",
                "--overwrite",
                "--parallel",
                42,
                str(tmp_dir) + "/*.py",
                "stageName",
            ]
        )
    assert result.exit_code == 0, result.output
    mock_execute.assert_called_once_with(
        f"put file://{Path(tmp_dir).resolve()}/*.py @stageName auto_compress=false parallel=42 overwrite=True"
    )


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_create(mock_execute, runner, mock_cursor):
    mock_execute.return_value = mock_cursor(["row"], [])
    result = runner.invoke_with_config(["stage", "create", "-c", "empty", "stageName"])
    assert result.exit_code == 0, result.output
    mock_execute.assert_called_once_with("create stage if not exists stageName")


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_drop(mock_execute, runner, mock_cursor):
    mock_execute.return_value = mock_cursor(["row"], [])
    result = runner.invoke_with_config(["stage", "drop", "-c", "empty", "stageName"])
    assert result.exit_code == 0, result.output
    mock_execute.assert_called_once_with("drop stage stageName")


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_remove(mock_execute, runner, mock_cursor):
    mock_execute.return_value = mock_cursor(["row"], [])
    result = runner.invoke_with_config(
        ["stage", "remove", "-c", "empty", "stageName", "my/file/foo.csv"]
    )
    assert result.exit_code == 0, result.output
    mock_execute.assert_called_once_with("remove @stageName/my/file/foo.csv")


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_internal_remove(mock_execute, mock_cursor):
    mock_execute.return_value = mock_cursor([{"CURRENT_ROLE()": "old_role"}], [])
    sm = StageManager()
    sm._remove("stageName", "my/file/foo.csv", "new_role")
    expected = [
        mock.call("select current_role()", cursor_class=DictCursor),
        mock.call("use role new_role"),
        mock.call("remove @stageName/my/file/foo.csv"),
        mock.call("use role old_role"),
    ]
    assert mock_execute.mock_calls == expected


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_internal_remove_no_role_change(mock_execute, mock_cursor):
    mock_execute.return_value = mock_cursor([{"CURRENT_ROLE()": "old_role"}], [])
    sm = StageManager()
    sm._remove("stageName", "my/file/foo.csv", "old_role")
    expected = [
        mock.call("select current_role()", cursor_class=DictCursor),
        mock.call("remove @stageName/my/file/foo.csv"),
    ]
    assert mock_execute.mock_calls == expected


@mock.patch(f"{STAGE_MANAGER}._execute_query")
def test_stage_internal_put(mock_execute, mock_cursor):
    mock_execute.return_value = mock_cursor([{"CURRENT_ROLE()": "old_role"}], [])
    with TemporaryDirectory() as tmp_dir:
        sm = StageManager()
        sm._put(Path(tmp_dir).resolve(), "stageName", "new_role")
        expected = [
            mock.call("select current_role()", cursor_class=DictCursor),
            mock.call("use role new_role"),
            mock.call(
                f"put file://{Path(tmp_dir).resolve()} @stageName auto_compress=false parallel=4 overwrite=False"
            ),
            mock.call("use role old_role"),
        ]
        assert mock_execute.mock_calls == expected