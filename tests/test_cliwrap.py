"""cliwrap のテスト (config ロード / 検証 / argv レンダリング / インジェクション拒否)。

実行: uv run pytest
"""
import sys
import tempfile
import unittest
from pathlib import Path

from cli_wrap_mcp import cliwrap


def load_yaml(text: str) -> cliwrap.ServerSpec:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fp:
        fp.write(text)
        path = fp.name
    try:
        return cliwrap.load_config(path)
    finally:
        Path(path).unlink()


MINIMAL = """
server:
  name: test
tools:
  - name: echo
    description: echo a message
    argv: ["echo", "{msg}"]
    params:
      msg:
        type: string
        description: message
"""


class LoadConfigTest(unittest.TestCase):
    def test_minimal_config_loads(self):
        spec = load_yaml(MINIMAL)
        self.assertEqual("test", spec.name)
        self.assertEqual(["echo"], list(spec.tools))
        tool = spec.tools["echo"]
        self.assertEqual("sync", tool.mode)
        self.assertEqual(cliwrap.DEFAULT_TIMEOUT_SEC, tool.timeout_sec)
        self.assertTrue(tool.params["msg"].required)

    def test_missing_server_section_is_error(self):
        with self.assertRaises(cliwrap.ConfigError):
            load_yaml('tools:\n  - {name: x, argv: ["true"]}\n')

    def test_no_tools_is_error(self):
        with self.assertRaises(cliwrap.ConfigError):
            load_yaml('server: {name: t}\n')

    def test_undefined_placeholder_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "undefined placeholders"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["echo", "{nope}"]}\n'
            )

    def test_format_spec_in_placeholder_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "format spec"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{msg:>5}"]\n'
                '    params: {msg: {type: string}}\n'
            )

    def test_positional_placeholder_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "positional"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["echo", "{}"]}\n'
            )

    def test_attribute_access_placeholder_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "attribute/index"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{msg.__class__}"]\n'
                '    params: {msg: {type: string}}\n'
            )

    def test_invalid_param_name_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "invalid param name"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["true"]\n'
                '    params: {"bad-name": {type: string}}\n'
            )

    def test_unknown_param_type_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "unknown type"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{n}"]\n'
                '    params: {n: {type: float}}\n'
            )

    def test_unknown_tool_key_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "unknown keys"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"], shell: true}\n'
            )

    def test_optional_param_without_default_referenced_in_argv_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "optional but has no default"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{msg}"]\n'
                '    params: {msg: {type: string, required: false}}\n'
            )

    def test_duplicate_tool_name_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "duplicate tool name"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"]}\n'
                '  - {name: x, argv: ["true"]}\n'
            )

    def test_unknown_mode_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "unknown mode"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"], mode: async}\n'
            )

    def test_default_type_mismatch_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "does not match type"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{n}"]\n'
                '    params: {n: {type: integer, default: "10"}}\n'
            )

    def test_invalid_regex_pattern_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "invalid pattern"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{m}"]\n'
                '    params: {m: {type: string, pattern: "["}}\n'
            )


class ArrayConfigTest(unittest.TestCase):
    def test_array_param_loads(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: run\n'
            '    argv: ["gcloud", "{args}"]\n'
            '    params:\n'
            '      args:\n'
            '        type: array\n'
            '        allow_dash_prefix: true\n'
            '        deny_pattern: "--project(=.*)?"\n'
        )
        p = spec.tools["run"].params["args"]
        self.assertEqual("array", p.type)
        self.assertEqual("--project(=.*)?", p.deny_pattern)

    def test_embedded_array_placeholder_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "entire argv element"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: run\n'
                '    argv: ["gcloud", "--flags={args}"]\n'
                '    params: {args: {type: array}}\n'
            )

    def test_array_placeholder_mixed_with_literal_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "entire argv element"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: run\n'
                '    argv: ["gcloud", "{sub}{args}"]\n'
                '    params:\n'
                '      sub: {type: string}\n'
                '      args: {type: array}\n'
            )

    def test_optional_array_gets_empty_default(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: run\n'
            '    argv: ["ls", "{args}"]\n'
            '    params: {args: {type: array, required: false}}\n'
        )
        self.assertEqual([], spec.tools["run"].params["args"].default)

    def test_array_default_must_be_string_list(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "list of strings"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: run\n'
                '    argv: ["ls", "{args}"]\n'
                '    params: {args: {type: array, default: [1, 2]}}\n'
            )

    def test_array_enum_items_must_be_strings(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "must be strings"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: run\n'
                '    argv: ["ls", "{args}"]\n'
                '    params: {args: {type: array, enum: [1]}}\n'
            )

    def test_invalid_deny_pattern_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "invalid deny_pattern"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{m}"]\n'
                '    params: {m: {type: string, deny_pattern: "["}}\n'
            )

    def test_deny_pattern_on_integer_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "only supported for string/array"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{n}"]\n'
                '    params: {n: {type: integer, deny_pattern: "-1"}}\n'
            )


class ValidateParamTest(unittest.TestCase):
    def spec(self, **kwargs) -> cliwrap.ParamSpec:
        return cliwrap.ParamSpec(name="p", **kwargs)

    def test_type_mismatch_rejected(self):
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "expected string"):
            cliwrap.validate_param(self.spec(type="string"), 42)
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "expected integer"):
            cliwrap.validate_param(self.spec(type="integer"), "42")

    def test_bool_is_not_integer(self):
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "expected integer"):
            cliwrap.validate_param(self.spec(type="integer"), True)

    def test_pattern_must_fullmatch(self):
        spec = self.spec(type="string", pattern=r"[a-z]+/[a-z]+")
        self.assertEqual("own/repo", cliwrap.validate_param(spec, "own/repo"))
        # 部分一致は拒否 (fullmatch)
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "does not match pattern"):
            cliwrap.validate_param(spec, "own/repo; rm -rf /")
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "does not match pattern"):
            cliwrap.validate_param(spec, "prefix own/repo")

    def test_enum_rejects_unlisted_value(self):
        spec = self.spec(type="string", enum=["open", "closed"])
        self.assertEqual("open", cliwrap.validate_param(spec, "open"))
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "not in enum"):
            cliwrap.validate_param(spec, "merged")

    def test_boolean_renders_lowercase(self):
        spec = self.spec(type="boolean")
        self.assertEqual("true", cliwrap.validate_param(spec, True))
        self.assertEqual("false", cliwrap.validate_param(spec, False))

    # --- 引数インジェクション対策 ---------------------------------------

    def test_dash_prefix_rejected_by_default(self):
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "starting with '-'"):
            cliwrap.validate_param(self.spec(type="string"), "--help")

    def test_negative_integer_rejected_by_default(self):
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "starting with '-'"):
            cliwrap.validate_param(self.spec(type="integer"), -1)

    def test_dash_prefix_allowed_when_opted_in(self):
        spec = self.spec(type="string", allow_dash_prefix=True)
        self.assertEqual("--help", cliwrap.validate_param(spec, "--help"))

    # --- deny_pattern (blocklist) ---------------------------------------

    def test_deny_pattern_rejects_fullmatch_only(self):
        spec = self.spec(type="string", deny_pattern=r"forbidden")
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "denied by deny_pattern"):
            cliwrap.validate_param(spec, "forbidden")
        # 部分一致は拒否しない (fullmatch)
        self.assertEqual("forbidden-ish", cliwrap.validate_param(spec, "forbidden-ish"))

    # --- array param -----------------------------------------------------

    def test_array_returns_item_list(self):
        spec = self.spec(type="array")
        self.assertEqual(
            ["compute", "instances", "list"],
            cliwrap.validate_param(spec, ["compute", "instances", "list"]),
        )
        self.assertEqual([], cliwrap.validate_param(spec, []))

    def test_array_rejects_non_list(self):
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "expected array"):
            cliwrap.validate_param(self.spec(type="array"), "compute instances list")

    def test_array_rejects_non_string_item(self):
        with self.assertRaisesRegex(cliwrap.ParamValidationError, r"'p'\[1\].*expected string"):
            cliwrap.validate_param(self.spec(type="array"), ["ok", 42])

    def test_array_item_pattern_fullmatch(self):
        spec = self.spec(type="array", pattern=r"[a-z-]+")
        self.assertEqual(["a", "b-c"], cliwrap.validate_param(spec, ["a", "b-c"]))
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "does not match pattern"):
            cliwrap.validate_param(spec, ["ok", "not ok"])

    def test_array_item_dash_guard_and_opt_in(self):
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "starting with '-'"):
            cliwrap.validate_param(self.spec(type="array"), ["--force"])
        spec = self.spec(type="array", allow_dash_prefix=True)
        self.assertEqual(["--force"], cliwrap.validate_param(spec, ["--force"]))

    def test_array_item_deny_pattern(self):
        spec = self.spec(
            type="array",
            allow_dash_prefix=True,
            deny_pattern=r"--(project|flags-file)(=.*)?",
        )
        self.assertEqual(
            ["compute", "--zone=asia-northeast1-a"],
            cliwrap.validate_param(spec, ["compute", "--zone=asia-northeast1-a"]),
        )
        for bad in ("--project", "--project=other", "--flags-file=/tmp/x.yml"):
            with self.assertRaisesRegex(cliwrap.ParamValidationError, "denied by deny_pattern"):
                cliwrap.validate_param(spec, [bad])

    def test_array_item_enum(self):
        spec = self.spec(type="array", enum=["a", "b"])
        self.assertEqual(["a", "b"], cliwrap.validate_param(spec, ["a", "b"]))
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "not in enum"):
            cliwrap.validate_param(spec, ["c"])


class RenderArgvTest(unittest.TestCase):
    def tool(self, argv, **params) -> cliwrap.ToolSpec:
        return cliwrap.ToolSpec(
            name="t", description="", argv=argv,
            params={name: spec for name, spec in params.items()},
        )

    def test_basic_substitution(self):
        tool = self.tool(
            ["gh", "api", "repos/{repo}"],
            repo=cliwrap.ParamSpec(name="repo", type="string"),
        )
        self.assertEqual(
            ["gh", "api", "repos/own/repo"],
            cliwrap.render_argv(tool, {"repo": "own/repo"}),
        )

    def test_default_applied_when_omitted(self):
        tool = self.tool(
            ["gh", "pr", "list", "--limit", "{limit}"],
            limit=cliwrap.ParamSpec(name="limit", type="integer", default=10),
        )
        self.assertEqual(
            ["gh", "pr", "list", "--limit", "10"],
            cliwrap.render_argv(tool, {}),
        )
        self.assertEqual(
            ["gh", "pr", "list", "--limit", "5"],
            cliwrap.render_argv(tool, {"limit": 5}),
        )

    def test_none_argument_falls_back_to_default(self):
        tool = self.tool(
            ["echo", "{m}"],
            m=cliwrap.ParamSpec(name="m", type="string", default="hi"),
        )
        self.assertEqual(["echo", "hi"], cliwrap.render_argv(tool, {"m": None}))

    def test_missing_required_param_is_error(self):
        tool = self.tool(
            ["echo", "{m}"],
            m=cliwrap.ParamSpec(name="m", type="string"),
        )
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "required"):
            cliwrap.render_argv(tool, {})

    def test_value_stays_single_argv_element(self):
        # 空白やシェルメタ文字を含む値も 1 argv 要素のまま (shell 経路なし)
        tool = self.tool(
            ["echo", "{m}"],
            m=cliwrap.ParamSpec(name="m", type="string"),
        )
        payload = "a b; rm -rf / && echo $(pwd) | cat"
        self.assertEqual(["echo", payload], cliwrap.render_argv(tool, {"m": payload}))

    def test_injection_dash_value_rejected_at_render(self):
        tool = self.tool(
            ["gh", "pr", "view", "{number}"],
            number=cliwrap.ParamSpec(name="number", type="string"),
        )
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "starting with '-'"):
            cliwrap.render_argv(tool, {"number": "--web"})

    def test_array_expands_with_forced_flag_after(self):
        tool = self.tool(
            ["gcloud", "{args}", "--project=pinned"],
            args=cliwrap.ParamSpec(name="args", type="array"),
        )
        self.assertEqual(
            ["gcloud", "compute", "instances", "list", "--project=pinned"],
            cliwrap.render_argv(tool, {"args": ["compute", "instances", "list"]}),
        )

    def test_empty_array_expands_to_zero_elements(self):
        tool = self.tool(
            ["ls", "{args}"],
            args=cliwrap.ParamSpec(name="args", type="array", required=False, default=[]),
        )
        self.assertEqual(["ls"], cliwrap.render_argv(tool, {}))

    def test_array_item_with_spaces_stays_single_element(self):
        tool = self.tool(
            ["echo", "{args}"],
            args=cliwrap.ParamSpec(name="args", type="array"),
        )
        self.assertEqual(
            ["echo", "a b; rm -rf /"],
            cliwrap.render_argv(tool, {"args": ["a b; rm -rf /"]}),
        )

    def test_missing_required_array_is_error(self):
        tool = self.tool(
            ["ls", "{args}"],
            args=cliwrap.ParamSpec(name="args", type="array"),
        )
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "required"):
            cliwrap.render_argv(tool, {})

    def test_array_in_non_exact_element_rejected_at_render(self):
        # ロード時検証を通らない ToolSpec 直組みへの防御
        tool = self.tool(
            ["echo", "--x={args}"],
            args=cliwrap.ParamSpec(name="args", type="array"),
        )
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "entire argv element"):
            cliwrap.render_argv(tool, {"args": ["v"]})

    def test_multiple_placeholders_in_one_element(self):
        tool = self.tool(
            ["gh", "api", "repos/{repo}/pulls/{number}"],
            repo=cliwrap.ParamSpec(name="repo", type="string"),
            number=cliwrap.ParamSpec(name="number", type="integer"),
        )
        self.assertEqual(
            ["gh", "api", "repos/o/r/pulls/12"],
            cliwrap.render_argv(tool, {"repo": "o/r", "number": 12}),
        )


class RunSyncTest(unittest.TestCase):
    def tool(self, argv, **kwargs) -> cliwrap.ToolSpec:
        return cliwrap.ToolSpec(name="t", description="", argv=argv, **kwargs)

    def test_stdout_returned(self):
        tool = self.tool([sys.executable, "-c", "print('hello')"])
        self.assertEqual("hello\n", cliwrap.run_sync(tool, tool.argv))

    def test_output_truncated_with_note(self):
        tool = self.tool(
            [sys.executable, "-c", "print('x' * 1000)"], max_output_bytes=100,
        )
        out = cliwrap.run_sync(tool, tool.argv)
        self.assertIn("output truncated at 100 bytes", out)
        self.assertLess(len(out), 300)

    def test_nonzero_exit_returns_error_with_stderr_tail(self):
        tool = self.tool(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        )
        out = cliwrap.run_sync(tool, tool.argv)
        self.assertIn("exited with code 3", out)
        self.assertIn("boom", out)

    def test_timeout_returns_error(self):
        tool = self.tool(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout_sec=1,
        )
        out = cliwrap.run_sync(tool, tool.argv)
        self.assertIn("timed out after 1s", out)

    def test_missing_binary_returns_error(self):
        tool = self.tool(["/nonexistent/binary"])
        out = cliwrap.run_sync(tool, tool.argv)
        self.assertIn("failed to execute", out)


class FileOutputModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.file_dir = Path(self._tmp.name) / "outputs"

    def tearDown(self):
        self._tmp.cleanup()

    def tool(self, output_mode="file", max_output_bytes=100) -> cliwrap.ToolSpec:
        return cliwrap.ToolSpec(
            name="t", description="",
            argv=[sys.executable, "-c", "print('x' * 1000)"],
            max_output_bytes=max_output_bytes,
            output_mode=output_mode,
        )

    def test_overflow_writes_file_holding_full_output(self):
        tool = self.tool()
        out = cliwrap.run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertIn("full output saved to file", out)
        self.assertIn("1001 bytes", out)  # 総バイト数 (1000 + 改行)
        self.assertIn("offset/limit", out)
        files = list(self.file_dir.iterdir())
        self.assertEqual(1, len(files))
        self.assertTrue(files[0].name.startswith("t-"))
        self.assertIn(str(files[0]), out)  # 絶対パスが返り値に含まれる
        self.assertEqual(b"x" * 1000 + b"\n", files[0].read_bytes())  # 全量・無切り詰め

    def test_under_limit_stays_inline(self):
        tool = self.tool(max_output_bytes=5000)
        out = cliwrap.run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertEqual("x" * 1000 + "\n", out)  # 従来どおり本文をそのまま返す
        self.assertFalse(self.file_dir.exists())

    def test_inline_mode_never_writes_file(self):
        tool = self.tool(output_mode="inline")
        out = cliwrap.run_sync(tool, tool.argv, file_dir=self.file_dir)
        self.assertIn("output truncated at 100 bytes", out)
        self.assertFalse(self.file_dir.exists())

    def test_file_mode_without_dir_falls_back_to_truncate(self):
        tool = self.tool()
        out = cliwrap.run_sync(tool, tool.argv, file_dir=None)
        self.assertIn("output truncated at 100 bytes", out)


class OutputModeConfigTest(unittest.TestCase):
    def test_invalid_output_mode_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "output_mode"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"], output_mode: spill}\n'
            )

    def test_defaults_inherited_and_overridable(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'defaults: {output_mode: file}\n'
            'tools:\n'
            '  - {name: a, argv: ["true"]}\n'
            '  - {name: b, argv: ["true"], output_mode: inline}\n'
        )
        self.assertEqual("file", spec.tools["a"].output_mode)
        self.assertEqual("inline", spec.tools["b"].output_mode)

    def test_default_is_inline(self):
        spec = load_yaml(MINIMAL)
        self.assertEqual("inline", spec.tools["echo"].output_mode)

    def test_unknown_defaults_key_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "defaults"):
            load_yaml(
                'server: {name: t}\n'
                'defaults: {timeout: 5}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"]}\n'
            )

class EnvConfigTest(unittest.TestCase):
    def test_tool_env_loads(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: x\n'
            '    argv: ["true"]\n'
            '    env: {FOO: bar}\n'
        )
        self.assertEqual({"FOO": "bar"}, spec.tools["x"].env)

    def test_defaults_env_merged_and_tool_wins(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'defaults:\n'
            '  env: {SHARED: base, PROJECT: default-proj}\n'
            'tools:\n'
            '  - {name: a, argv: ["true"]}\n'
            '  - name: b\n'
            '    argv: ["true"]\n'
            '    env: {PROJECT: other-proj}\n'
        )
        self.assertEqual({"SHARED": "base", "PROJECT": "default-proj"}, spec.tools["a"].env)
        self.assertEqual({"SHARED": "base", "PROJECT": "other-proj"}, spec.tools["b"].env)

    def test_invalid_env_var_name_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "invalid env var name"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["true"]\n'
                '    env: {"BAD-NAME": v}\n'
            )

    def test_non_string_env_value_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "must be a string"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["true"]\n'
                '    env: {NUM: 1}\n'
            )

    def test_env_not_mapping_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "env must be a mapping"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["true"]\n'
                '    env: [FOO=bar]\n'
            )

    def test_defaults_env_validated(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "defaults.*invalid env var name"):
            load_yaml(
                'server: {name: t}\n'
                'defaults:\n'
                '  env: {"1BAD": v}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"]}\n'
            )


class EnvExecTest(unittest.TestCase):
    PRINT_VAR = "import os; print(os.environ.get('CLIWRAP_TEST_VAR', '(unset)'))"

    def tool(self, env) -> cliwrap.ToolSpec:
        return cliwrap.ToolSpec(
            name="t", description="", argv=[sys.executable, "-c", self.PRINT_VAR], env=env,
        )

    def test_run_sync_forces_env_var(self):
        tool = self.tool({"CLIWRAP_TEST_VAR": "forced"})
        self.assertEqual("forced\n", cliwrap.run_sync(tool, tool.argv))

    def test_forced_env_overrides_inherited(self):
        import os

        os.environ["CLIWRAP_TEST_VAR"] = "parent"
        self.addCleanup(os.environ.pop, "CLIWRAP_TEST_VAR", None)
        tool = self.tool({"CLIWRAP_TEST_VAR": "forced"})
        self.assertEqual("forced\n", cliwrap.run_sync(tool, tool.argv))

    def test_parent_env_still_inherited_alongside_forced(self):
        import os

        os.environ["CLIWRAP_OTHER_VAR"] = "inherited"
        self.addCleanup(os.environ.pop, "CLIWRAP_OTHER_VAR", None)
        tool = cliwrap.ToolSpec(
            name="t", description="",
            argv=[sys.executable, "-c", "import os; print(os.environ['CLIWRAP_OTHER_VAR'])"],
            env={"CLIWRAP_TEST_VAR": "forced"},
        )
        self.assertEqual("inherited\n", cliwrap.run_sync(tool, tool.argv))

    def test_no_env_config_inherits_parent(self):
        import os

        os.environ["CLIWRAP_TEST_VAR"] = "parent"
        self.addCleanup(os.environ.pop, "CLIWRAP_TEST_VAR", None)
        tool = self.tool({})
        self.assertEqual("parent\n", cliwrap.run_sync(tool, tool.argv))

    def test_job_start_forces_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = cliwrap.JobManager("testsrv", cache_dir=Path(tmp))
            tool = cliwrap.ToolSpec(
                name="j", description="", argv=[], mode="job",
                env={"CLIWRAP_TEST_VAR": "forced"},
            )
            msg = jobs.start(tool, [sys.executable, "-c", self.PRINT_VAR])
            job_id = msg.splitlines()[0].removeprefix("job started: ")
            import time

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state, _rc = jobs._poll(job_id)
                if state != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(
                "forced\n", (jobs.jobs_dir / job_id / "stdout.log").read_text(),
            )


class OutputDirTest(unittest.TestCase):
    """全 sync ツールに自動注入される output_dir param の挙動。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = str(Path(self._tmp.name) / "dest")

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, server, name, args) -> str:
        import anyio

        result = anyio.run(lambda: server.call_tool(name, args))
        content = result[0] if isinstance(result, tuple) else result
        return content[0].text

    def server(self):
        return cliwrap.build_server(load_yaml(MINIMAL), cache_dir=Path(self._tmp.name))

    def test_output_dir_forces_file_even_for_small_output(self):
        out = self.call(self.server(), "echo", {"msg": "hi", "output_dir": self.dest})
        self.assertIn("full output saved to file", out)
        files = list(Path(self.dest).iterdir())  # dir は mkdir -p 相当で自動作成
        self.assertEqual(1, len(files))
        self.assertTrue(files[0].name.startswith("echo-"))
        self.assertIn(str(files[0]), out)
        self.assertEqual(b"hi\n", files[0].read_bytes())  # 小出力でも全量ファイル化

    def test_without_output_dir_behaves_as_before(self):
        out = self.call(self.server(), "echo", {"msg": "hi"})
        self.assertEqual("hi\n", out)
        self.assertFalse(Path(self.dest).exists())

    def test_relative_output_dir_rejected(self):
        out = self.call(self.server(), "echo", {"msg": "hi", "output_dir": "rel/dir"})
        self.assertIn("error: output_dir must be an absolute path", out)

    def test_write_failure_returns_error_string(self):
        # 既存ファイルの下に dir は作れない → OSError → エラー文字列
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("file")
        out = self.call(
            self.server(), "echo", {"msg": "hi", "output_dir": str(blocker / "sub")},
        )
        self.assertIn("error: failed to write output", out)

    def test_output_dir_in_schema_as_optional(self):
        import anyio

        tools = anyio.run(self.server().list_tools)
        schema = tools[0].inputSchema
        self.assertIn("output_dir", schema["properties"])
        self.assertNotIn("output_dir", schema.get("required", []))

    def test_job_tools_not_injected(self):
        import anyio

        spec = load_yaml(JobConfigTest.JOB_YAML)
        server = cliwrap.build_server(spec, cache_dir=Path(self._tmp.name))
        for tool in anyio.run(server.list_tools):
            self.assertNotIn("output_dir", tool.inputSchema["properties"], tool.name)

    def test_output_dir_wins_over_truncate_for_large_output(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: big\n'
            '    description: big\n'
            '    max_output_bytes: 100\n'
            f'    argv: ["{sys.executable}", "-c", "print(\'x\' * 1000)"]\n'
        )
        server = cliwrap.build_server(spec, cache_dir=Path(self._tmp.name))
        out = self.call(server, "big", {"output_dir": self.dest})
        self.assertIn("1001 bytes", out)
        files = list(Path(self.dest).iterdir())
        self.assertEqual(b"x" * 1000 + b"\n", files[0].read_bytes())  # 切り詰めなし

    def test_reserved_param_name_is_config_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "reserved"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{output_dir}"]\n'
                '    params: {output_dir: {type: string}}\n'
            )


class JobModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.jobs = cliwrap.JobManager("testsrv", cache_dir=Path(self._tmp.name))
        self.tool = cliwrap.ToolSpec(
            name="j", description="", argv=[], mode="job", max_output_bytes=10_000,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def wait_exit(self, job_id, timeout=10.0):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state, rc = self.jobs._poll(job_id)
            if state != "running":
                return state, rc
            time.sleep(0.05)
        raise AssertionError("job did not exit in time")

    def job_id_from(self, message: str) -> str:
        first = message.splitlines()[0]
        self.assertTrue(first.startswith("job started: "), message)
        return first.removeprefix("job started: ")

    def test_start_status_result_lifecycle(self):
        msg = self.jobs.start(
            self.tool, [sys.executable, "-c", "print('job-out'); import sys; sys.exit(0)"],
        )
        job_id = self.job_id_from(msg)
        state, rc = self.wait_exit(job_id)
        self.assertEqual(("exited", 0), (state, rc))
        status = self.jobs.status(job_id)
        self.assertIn("exited", status)
        self.assertIn("job-out", status)
        result = self.jobs.result(job_id, self.tool.max_output_bytes)
        self.assertIn("job-out", result)
        jdir = self.jobs.jobs_dir / job_id
        for name in ("stdout.log", "stderr.log", "pid", "meta.json", "exit_code"):
            self.assertTrue((jdir / name).exists(), name)

    def test_result_while_running_says_running(self):
        msg = self.jobs.start(self.tool, [sys.executable, "-c", "import time; time.sleep(30)"])
        job_id = self.job_id_from(msg)
        try:
            self.assertIn("still running", self.jobs.result(job_id, 1000))
            self.assertIn("running", self.jobs.status(job_id))
        finally:
            self.jobs.cancel(job_id)

    def test_cancel_terminates_job(self):
        msg = self.jobs.start(self.tool, [sys.executable, "-c", "import time; time.sleep(30)"])
        job_id = self.job_id_from(msg)
        out = self.jobs.cancel(job_id)
        self.assertIn("SIGTERM", out)
        state, rc = self.wait_exit(job_id)
        self.assertEqual("exited", state)
        self.assertNotEqual(0, rc)

    def test_nonzero_exit_result_includes_stderr(self):
        msg = self.jobs.start(
            self.tool,
            [sys.executable, "-c", "import sys; sys.stderr.write('job-err'); sys.exit(2)"],
        )
        job_id = self.job_id_from(msg)
        self.wait_exit(job_id)
        result = self.jobs.result(job_id, 1000)
        self.assertIn("exit code 2", result)
        self.assertIn("job-err", result)

    # --- job_id インジェクション (パストラバーサル) 対策 -------------------

    def test_malformed_job_id_rejected(self):
        for bad in ("../../etc/passwd", "x; rm -rf /", "20260715T000000-XYZ!!", ""):
            with self.assertRaisesRegex(cliwrap.ParamValidationError, "invalid job_id"):
                self.jobs.status(bad)

    def test_unknown_but_wellformed_job_id_is_error(self):
        with self.assertRaisesRegex(cliwrap.ParamValidationError, "unknown job_id"):
            self.jobs.status("20260101T000000-abc123")


class JobConfigTest(unittest.TestCase):
    JOB_YAML = (
        'server: {name: t}\n'
        'tools:\n'
        '  - name: task\n'
        '    mode: job\n'
        '    argv: ["sleep", "{sec}"]\n'
        '    params: {sec: {type: integer, default: 1}}\n'
    )

    def test_job_mode_loads(self):
        spec = load_yaml(self.JOB_YAML)
        self.assertEqual("job", spec.tools["task"].mode)

    def test_job_tools_registered_as_four_tools(self):
        spec = load_yaml(self.JOB_YAML)
        with tempfile.TemporaryDirectory() as tmp:
            server = cliwrap.build_server(spec, cache_dir=Path(tmp))
            import anyio

            names = sorted(t.name for t in anyio.run(server.list_tools))
        self.assertEqual(
            ["task_cancel", "task_result", "task_start", "task_status"], names,
        )

    def test_job_tool_with_array_param_loads_and_renders(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: task\n'
            '    mode: job\n'
            '    argv: ["do-work", "{args}"]\n'
            '    params: {args: {type: array}}\n'
        )
        self.assertEqual(
            ["do-work", "a", "b"],
            cliwrap.render_argv(spec.tools["task"], {"args": ["a", "b"]}),
        )

    def test_exposed_name_collision_is_error(self):
        with self.assertRaisesRegex(cliwrap.ConfigError, "collision"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: task, mode: job, argv: ["true"]}\n'
                '  - {name: task_start, argv: ["true"]}\n'
            )


class BuildServerTest(unittest.TestCase):
    def test_tools_registered_on_fastmcp(self):
        spec = load_yaml(MINIMAL)
        server = cliwrap.build_server(spec)
        import anyio

        tools = anyio.run(server.list_tools)
        self.assertEqual(["echo"], [t.name for t in tools])
        schema = tools[0].inputSchema
        self.assertEqual(["msg", "output_dir"], list(schema["properties"]))
        self.assertEqual(["msg"], schema.get("required", []))
        self.assertEqual("string", schema["properties"]["msg"]["type"])

    def test_call_tool_executes_argv(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: pyprint\n'
            '    description: print\n'
            f'    argv: ["{sys.executable}", "-c", "print(\'ok:\' + \'{{msg}}\')"]\n'
            '    params: {msg: {type: string}}\n'
        )
        server = cliwrap.build_server(spec)
        import anyio

        result = anyio.run(lambda: server.call_tool("pyprint", {"msg": "ping"}))
        content = result[0] if isinstance(result, tuple) else result
        self.assertIn("ok:ping", content[0].text)

    ARRAY_YAML = (
        'server: {name: t}\n'
        'tools:\n'
        '  - name: pyargs\n'
        '    description: print argv\n'
        f'    argv: ["{sys.executable}", "-c", "import sys; print(sys.argv[1:])", "{{args}}"]\n'
        '    params:\n'
        '      args: {type: array, required: false}\n'
    )

    def test_array_param_in_input_schema(self):
        server = cliwrap.build_server(load_yaml(self.ARRAY_YAML))
        import anyio

        schema = anyio.run(server.list_tools)[0].inputSchema
        self.assertNotIn("args", schema.get("required", []))
        prop = schema["properties"]["args"]
        # optional array は list[str] | None なので anyOf 形になる
        variants = prop.get("anyOf", [prop])
        array_variants = [v for v in variants if v.get("type") == "array"]
        self.assertEqual(1, len(array_variants))
        self.assertEqual({"type": "string"}, array_variants[0]["items"])

    def test_call_tool_with_array_argument(self):
        server = cliwrap.build_server(load_yaml(self.ARRAY_YAML))
        import anyio

        result = anyio.run(lambda: server.call_tool("pyargs", {"args": ["a", "b c"]}))
        content = result[0] if isinstance(result, tuple) else result
        self.assertIn("['a', 'b c']", content[0].text)

    def test_call_tool_with_array_omitted_uses_empty_default(self):
        server = cliwrap.build_server(load_yaml(self.ARRAY_YAML))
        import anyio

        result = anyio.run(lambda: server.call_tool("pyargs", {}))
        content = result[0] if isinstance(result, tuple) else result
        self.assertIn("[]", content[0].text)


if __name__ == "__main__":
    unittest.main()
