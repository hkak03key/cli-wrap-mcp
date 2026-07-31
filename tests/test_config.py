"""config ロード (YAML → ServerSpec) のテスト。

実行: uv run pytest
"""
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers import JOB_YAML, MINIMAL, call_tool, load_yaml
from cli_wrap_mcp.rendering import render_argv
from cli_wrap_mcp.server import build_server
from cli_wrap_mcp.spec import ConfigError, DEFAULT_TIMEOUT_SEC


class LoadConfigTest(unittest.TestCase):
    def test_minimal_config_loads(self):
        spec = load_yaml(MINIMAL)
        self.assertEqual("test", spec.name)
        self.assertEqual(["echo"], list(spec.tools))
        tool = spec.tools["echo"]
        self.assertEqual("sync", tool.mode)
        self.assertEqual(DEFAULT_TIMEOUT_SEC, tool.timeout_sec)
        self.assertTrue(tool.params["msg"].required)

    def test_missing_server_section_is_error(self):
        with self.assertRaises(ConfigError):
            load_yaml('tools:\n  - {name: x, argv: ["true"]}\n')

    def test_no_tools_is_error(self):
        with self.assertRaises(ConfigError):
            load_yaml('server: {name: t}\n')

    def test_undefined_placeholder_is_error(self):
        with self.assertRaisesRegex(ConfigError, "undefined placeholders"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["echo", "{nope}"]}\n'
            )

    def test_format_spec_in_placeholder_is_error(self):
        with self.assertRaisesRegex(ConfigError, "format spec"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{msg:>5}"]\n'
                '    params: {msg: {type: string}}\n'
            )

    def test_positional_placeholder_is_error(self):
        with self.assertRaisesRegex(ConfigError, "positional"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["echo", "{}"]}\n'
            )

    def test_attribute_access_placeholder_is_error(self):
        with self.assertRaisesRegex(ConfigError, "attribute/index"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{msg.__class__}"]\n'
                '    params: {msg: {type: string}}\n'
            )

    def test_invalid_param_name_is_error(self):
        with self.assertRaisesRegex(ConfigError, "invalid param name"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["true"]\n'
                '    params: {"bad-name": {type: string}}\n'
            )

    def test_unknown_param_type_is_error(self):
        with self.assertRaisesRegex(ConfigError, "unknown type"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{n}"]\n'
                '    params: {n: {type: float}}\n'
            )

    def test_unknown_tool_key_is_error(self):
        with self.assertRaisesRegex(ConfigError, "unknown keys"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"], shell: true}\n'
            )

    def test_optional_param_without_default_referenced_in_argv_is_error(self):
        with self.assertRaisesRegex(ConfigError, "optional but has no default"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{msg}"]\n'
                '    params: {msg: {type: string, required: false}}\n'
            )

    def test_duplicate_tool_name_is_error(self):
        with self.assertRaisesRegex(ConfigError, "duplicate tool name"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"]}\n'
                '  - {name: x, argv: ["true"]}\n'
            )

    def test_unknown_mode_is_error(self):
        with self.assertRaisesRegex(ConfigError, "unknown mode"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"], mode: async}\n'
            )

    def test_default_type_mismatch_is_error(self):
        with self.assertRaisesRegex(ConfigError, "does not match type"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{n}"]\n'
                '    params: {n: {type: integer, default: "10"}}\n'
            )

    def test_invalid_regex_pattern_is_error(self):
        with self.assertRaisesRegex(ConfigError, "invalid pattern"):
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
        with self.assertRaisesRegex(ConfigError, "entire argv element"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: run\n'
                '    argv: ["gcloud", "--flags={args}"]\n'
                '    params: {args: {type: array}}\n'
            )

    def test_array_placeholder_mixed_with_literal_is_error(self):
        with self.assertRaisesRegex(ConfigError, "entire argv element"):
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
        with self.assertRaisesRegex(ConfigError, "list of strings"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: run\n'
                '    argv: ["ls", "{args}"]\n'
                '    params: {args: {type: array, default: [1, 2]}}\n'
            )

    def test_array_enum_items_must_be_strings(self):
        with self.assertRaisesRegex(ConfigError, "must be strings"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: run\n'
                '    argv: ["ls", "{args}"]\n'
                '    params: {args: {type: array, enum: [1]}}\n'
            )

    def test_invalid_deny_pattern_is_error(self):
        with self.assertRaisesRegex(ConfigError, "invalid deny_pattern"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{m}"]\n'
                '    params: {m: {type: string, deny_pattern: "["}}\n'
            )

    def test_deny_pattern_on_integer_is_error(self):
        with self.assertRaisesRegex(ConfigError, "only supported for string/array"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["echo", "{n}"]\n'
                '    params: {n: {type: integer, deny_pattern: "-1"}}\n'
            )

class OutputModeConfigTest(unittest.TestCase):
    def test_invalid_output_mode_is_error(self):
        with self.assertRaisesRegex(ConfigError, "output_mode"):
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
        self.assertEqual("truncate", spec.tools["echo"].inline_on_large_output)

    def test_invalid_inline_on_large_output_is_error(self):
        with self.assertRaisesRegex(ConfigError, "inline_on_large_output"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"], inline_on_large_output: spill}\n'
            )

    def test_inline_settings_inherited_from_defaults(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'defaults: {inline_on_large_output: file, inline_max_output_bytes: 123}\n'
            'tools:\n'
            '  - {name: a, argv: ["true"]}\n'
            '  - {name: b, argv: ["true"], inline_on_large_output: truncate}\n'
        )
        self.assertEqual("file", spec.tools["a"].inline_on_large_output)
        self.assertEqual(123, spec.tools["a"].inline_max_output_bytes)
        self.assertEqual("truncate", spec.tools["b"].inline_on_large_output)

    def test_inline_keys_allowed_on_file_mode_tool(self):
        # file mode では inline_* は使われないが、defaults 運用のためエラーにしない
        spec = load_yaml(
            'server: {name: t}\n'
            'defaults: {inline_on_large_output: file}\n'
            'tools:\n'
            '  - {name: x, argv: ["true"], output_mode: file, inline_max_output_bytes: 10}\n'
        )
        self.assertEqual("file", spec.tools["x"].output_mode)

    def test_unknown_defaults_key_is_error(self):
        with self.assertRaisesRegex(ConfigError, "defaults"):
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
        with self.assertRaisesRegex(ConfigError, "invalid env var name"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["true"]\n'
                '    env: {"BAD-NAME": v}\n'
            )

    def test_non_string_env_value_is_error(self):
        with self.assertRaisesRegex(ConfigError, "must be a string"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["true"]\n'
                '    env: {NUM: 1}\n'
            )

    def test_env_not_mapping_is_error(self):
        with self.assertRaisesRegex(ConfigError, "env must be a mapping"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - name: x\n'
                '    argv: ["true"]\n'
                '    env: [FOO=bar]\n'
            )

    def test_defaults_env_validated(self):
        with self.assertRaisesRegex(ConfigError, "defaults.*invalid env var name"):
            load_yaml(
                'server: {name: t}\n'
                'defaults:\n'
                '  env: {"1BAD": v}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"]}\n'
            )

class FileOutputDirConfigTest(unittest.TestCase):
    """config レベル file_output_dir (defaults / tool) と出力ルートの解決。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "audit"
        self.cache = Path(self._tmp.name) / "cache"

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, server, name, args) -> str:
        return call_tool(server, name, args).content[0].text

    def test_relative_path_is_config_error(self):
        with self.assertRaisesRegex(ConfigError, "absolute path"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: x, argv: ["true"], file_output_dir: rel/dir}\n'
            )

    def test_defaults_inherited_and_tool_wins(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'defaults: {file_output_dir: /srv/default}\n'
            'tools:\n'
            '  - {name: a, argv: ["true"]}\n'
            '  - {name: b, argv: ["true"], file_output_dir: /srv/b}\n'
        )
        self.assertEqual("/srv/default", spec.tools["a"].file_output_dir)
        self.assertEqual("/srv/b", spec.tools["b"].file_output_dir)

    def test_file_mode_writes_under_configured_root(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: say\n'
            '    output_mode: file\n'
            f'    file_output_dir: {self.root}\n'
            f'    argv: ["{sys.executable}", "-c", "print(\'hi\')"]\n'
        )
        server = build_server(spec, cache_dir=self.cache)
        out = self.call(server, "say", {})
        self.assertIn(str(self.root / "outputs"), out)
        dirs = list((self.root / "outputs").iterdir())
        self.assertEqual(1, len(dirs))
        self.assertEqual(b"hi\n", (dirs[0] / "stdout.log").read_bytes())
        self.assertFalse(self.cache.exists())  # cache 側には何も書かれない

    def test_per_call_param_wins_over_configured_root(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: say\n'
            '    output_mode: file\n'
            f'    file_output_dir: {self.root}\n'
            f'    argv: ["{sys.executable}", "-c", "print(\'hi\')"]\n'
        )
        server = build_server(spec, cache_dir=self.cache)
        dest = str(Path(self._tmp.name) / "per-call")
        out = self.call(server, "say", {"file_output_dir": dest})
        self.assertIn(dest, out)
        self.assertEqual(1, len(list(Path(dest).iterdir())))
        self.assertFalse(self.root.exists())

    def test_jobs_live_under_configured_root(self):
        spec = load_yaml(
            'server: {name: t}\n'
            'tools:\n'
            '  - name: task\n'
            '    mode: job\n'
            f'    file_output_dir: {self.root}\n'
            f'    argv: ["{sys.executable}", "-c", "print(\'job-out\')"]\n'
        )
        server = build_server(spec, cache_dir=self.cache)
        out = self.call(server, "task_start", {})
        self.assertIn("job started:", out)
        self.assertIn(str(self.root / "jobs"), out)
        jobs = list((self.root / "jobs").iterdir())
        self.assertEqual(1, len(jobs))
        self.assertFalse(self.cache.exists())

class JobConfigTest(unittest.TestCase):

    def test_job_mode_loads(self):
        spec = load_yaml(JOB_YAML)
        self.assertEqual("job", spec.tools["task"].mode)

    def test_job_tools_registered_as_four_tools(self):
        spec = load_yaml(JOB_YAML)
        with tempfile.TemporaryDirectory() as tmp:
            server = build_server(spec, cache_dir=Path(tmp))
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
            render_argv(spec.tools["task"], {"args": ["a", "b"]}),
        )

    def test_exposed_name_collision_is_error(self):
        with self.assertRaisesRegex(ConfigError, "collision"):
            load_yaml(
                'server: {name: t}\n'
                'tools:\n'
                '  - {name: task, mode: job, argv: ["true"]}\n'
                '  - {name: task_start, argv: ["true"]}\n'
            )


if __name__ == "__main__":
    unittest.main()
