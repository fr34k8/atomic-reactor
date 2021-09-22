"""
Copyright (c) 2021 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from dataclasses import dataclass
from typing import Optional, ClassVar

import flexmock
import pytest

from osbs.exceptions import OsbsValidationException

from atomic_reactor import util
from atomic_reactor.tasks import common

TASK_ARGS = {
    "build_dir": "/build",
    "context_dir": "/context",
    "config_file": "config.yaml",
}

USER_PARAMS_STR = '{"a": "b"}'
USER_PARAMS_FILE = "user_params.json"
USER_PARAMS = {"a": "b"}

TASK_ARGS_WITH_USER_PARAMS = {**TASK_ARGS, "user_params": USER_PARAMS_STR}


class TestPluginsDef:
    """Tests for the PluginsDef class."""

    def test_create_valid(self):
        plugins = common.PluginsDef(build=[{"name": "some_plugin"}])
        assert plugins.prebuild == []
        assert plugins.build == [{"name": "some_plugin"}]
        assert plugins.prepublish == []
        assert plugins.postbuild == []
        assert plugins.exit == []

    def test_create_invalid(self):
        with pytest.raises(OsbsValidationException, match="1 is not of type 'boolean'"):
            common.PluginsDef(prebuild=[{"name": "some_plugin", "required": 1}])


class TestTaskParams:
    """Tests for the TaskParams class."""

    def mock_read_user_params(self, schema="schemas/user_params.json"):
        (
            flexmock(util)
            .should_receive("read_yaml")
            .with_args(USER_PARAMS_STR, schema)
            .and_return(USER_PARAMS)
        )

    def mock_read_user_params_file(self, schema="schemas/user_params.json"):
        (
            flexmock(util)
            .should_receive("read_yaml_from_file_path")
            .with_args(USER_PARAMS_FILE, schema)
            .and_return(USER_PARAMS)
        )

    def check_attrs(self, params):
        for attr, value in TASK_ARGS.items():
            assert getattr(params, attr) == value
        assert params.user_params == USER_PARAMS

    def test_missing_user_params(self):
        with pytest.raises(ValueError, match="Did not receive user params"):
            common.TaskParams.from_cli_args({**TASK_ARGS})

    def test_drop_known_unset_arg(self):
        self.mock_read_user_params()
        with pytest.raises(TypeError, match=r"__init__\(\) missing .* 'config_file'"):
            common.TaskParams.from_cli_args({**TASK_ARGS_WITH_USER_PARAMS, "config_file": None})

    def test_keep_unknown_unset_arg(self):
        self.mock_read_user_params()
        err = r"__init__\(\) got an unexpected keyword argument 'unknown'"
        with pytest.raises(TypeError, match=err):
            common.TaskParams.from_cli_args({**TASK_ARGS_WITH_USER_PARAMS, "unknown": None})

    def test_user_params_from_str(self):
        self.mock_read_user_params()
        params = common.TaskParams.from_cli_args(TASK_ARGS_WITH_USER_PARAMS)
        self.check_attrs(params)

    def test_user_params_from_file(self):
        self.mock_read_user_params_file()
        params = common.TaskParams.from_cli_args(
            {**TASK_ARGS, "user_params_file": USER_PARAMS_FILE}
        )
        self.check_attrs(params)

    def test_from_cli_args_inheritance(self):
        # TaskParams is a base class, test that the from_cli_args method behaves as expected
        #   when inherited

        @dataclass(frozen=True)
        class ChildTaskParams(common.TaskParams):
            user_params_schema: ClassVar[str] = "schemas/some_schema.json"

            a: int
            b: Optional[int] = None

        self.mock_read_user_params("schemas/some_schema.json")
        params = ChildTaskParams.from_cli_args({**TASK_ARGS_WITH_USER_PARAMS, "a": 1})
        self.check_attrs(params)
        assert params.a == 1
        assert params.b is None