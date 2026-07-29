"""パラメータ検証と argv レンダリングのテスト (インジェクション拒否を含む)。

実行: uv run pytest
"""
import unittest

from cli_wrap_mcp.rendering import render_argv, validate_param
from cli_wrap_mcp.spec import ParamSpec, ParamValidationError, ToolSpec


class ValidateParamTest(unittest.TestCase):
    def spec(self, **kwargs) -> ParamSpec:
        return ParamSpec(name="p", **kwargs)

    def test_type_mismatch_rejected(self):
        with self.assertRaisesRegex(ParamValidationError, "expected string"):
            validate_param(self.spec(type="string"), 42)
        with self.assertRaisesRegex(ParamValidationError, "expected integer"):
            validate_param(self.spec(type="integer"), "42")

    def test_bool_is_not_integer(self):
        with self.assertRaisesRegex(ParamValidationError, "expected integer"):
            validate_param(self.spec(type="integer"), True)

    def test_pattern_must_fullmatch(self):
        spec = self.spec(type="string", pattern=r"[a-z]+/[a-z]+")
        self.assertEqual("own/repo", validate_param(spec, "own/repo"))
        # 部分一致は拒否 (fullmatch)
        with self.assertRaisesRegex(ParamValidationError, "does not match pattern"):
            validate_param(spec, "own/repo; rm -rf /")
        with self.assertRaisesRegex(ParamValidationError, "does not match pattern"):
            validate_param(spec, "prefix own/repo")

    def test_enum_rejects_unlisted_value(self):
        spec = self.spec(type="string", enum=["open", "closed"])
        self.assertEqual("open", validate_param(spec, "open"))
        with self.assertRaisesRegex(ParamValidationError, "not in enum"):
            validate_param(spec, "merged")

    def test_boolean_renders_lowercase(self):
        spec = self.spec(type="boolean")
        self.assertEqual("true", validate_param(spec, True))
        self.assertEqual("false", validate_param(spec, False))

    # --- 引数インジェクション対策 ---------------------------------------

    def test_dash_prefix_rejected_by_default(self):
        with self.assertRaisesRegex(ParamValidationError, "starting with '-'"):
            validate_param(self.spec(type="string"), "--help")

    def test_negative_integer_rejected_by_default(self):
        with self.assertRaisesRegex(ParamValidationError, "starting with '-'"):
            validate_param(self.spec(type="integer"), -1)

    def test_dash_prefix_allowed_when_opted_in(self):
        spec = self.spec(type="string", allow_dash_prefix=True)
        self.assertEqual("--help", validate_param(spec, "--help"))

    # --- deny_pattern (blocklist) ---------------------------------------

    def test_deny_pattern_rejects_fullmatch_only(self):
        spec = self.spec(type="string", deny_pattern=r"forbidden")
        with self.assertRaisesRegex(ParamValidationError, "denied by deny_pattern"):
            validate_param(spec, "forbidden")
        # 部分一致は拒否しない (fullmatch)
        self.assertEqual("forbidden-ish", validate_param(spec, "forbidden-ish"))

    # --- array param -----------------------------------------------------

    def test_array_returns_item_list(self):
        spec = self.spec(type="array")
        self.assertEqual(
            ["compute", "instances", "list"],
            validate_param(spec, ["compute", "instances", "list"]),
        )
        self.assertEqual([], validate_param(spec, []))

    def test_array_rejects_non_list(self):
        with self.assertRaisesRegex(ParamValidationError, "expected array"):
            validate_param(self.spec(type="array"), "compute instances list")

    def test_array_rejects_non_string_item(self):
        with self.assertRaisesRegex(ParamValidationError, r"'p'\[1\].*expected string"):
            validate_param(self.spec(type="array"), ["ok", 42])

    def test_array_item_pattern_fullmatch(self):
        spec = self.spec(type="array", pattern=r"[a-z-]+")
        self.assertEqual(["a", "b-c"], validate_param(spec, ["a", "b-c"]))
        with self.assertRaisesRegex(ParamValidationError, "does not match pattern"):
            validate_param(spec, ["ok", "not ok"])

    def test_array_item_dash_guard_and_opt_in(self):
        with self.assertRaisesRegex(ParamValidationError, "starting with '-'"):
            validate_param(self.spec(type="array"), ["--force"])
        spec = self.spec(type="array", allow_dash_prefix=True)
        self.assertEqual(["--force"], validate_param(spec, ["--force"]))

    def test_array_item_deny_pattern(self):
        spec = self.spec(
            type="array",
            allow_dash_prefix=True,
            deny_pattern=r"--(project|flags-file)(=.*)?",
        )
        self.assertEqual(
            ["compute", "--zone=asia-northeast1-a"],
            validate_param(spec, ["compute", "--zone=asia-northeast1-a"]),
        )
        for bad in ("--project", "--project=other", "--flags-file=/tmp/x.yml"):
            with self.assertRaisesRegex(ParamValidationError, "denied by deny_pattern"):
                validate_param(spec, [bad])

    def test_array_item_enum(self):
        spec = self.spec(type="array", enum=["a", "b"])
        self.assertEqual(["a", "b"], validate_param(spec, ["a", "b"]))
        with self.assertRaisesRegex(ParamValidationError, "not in enum"):
            validate_param(spec, ["c"])

class RenderArgvTest(unittest.TestCase):
    def tool(self, argv, **params) -> ToolSpec:
        return ToolSpec(
            name="t", description="", argv=argv,
            params={name: spec for name, spec in params.items()},
        )

    def test_basic_substitution(self):
        tool = self.tool(
            ["gh", "api", "repos/{repo}"],
            repo=ParamSpec(name="repo", type="string"),
        )
        self.assertEqual(
            ["gh", "api", "repos/own/repo"],
            render_argv(tool, {"repo": "own/repo"}),
        )

    def test_default_applied_when_omitted(self):
        tool = self.tool(
            ["gh", "pr", "list", "--limit", "{limit}"],
            limit=ParamSpec(name="limit", type="integer", default=10),
        )
        self.assertEqual(
            ["gh", "pr", "list", "--limit", "10"],
            render_argv(tool, {}),
        )
        self.assertEqual(
            ["gh", "pr", "list", "--limit", "5"],
            render_argv(tool, {"limit": 5}),
        )

    def test_none_argument_falls_back_to_default(self):
        tool = self.tool(
            ["echo", "{m}"],
            m=ParamSpec(name="m", type="string", default="hi"),
        )
        self.assertEqual(["echo", "hi"], render_argv(tool, {"m": None}))

    def test_missing_required_param_is_error(self):
        tool = self.tool(
            ["echo", "{m}"],
            m=ParamSpec(name="m", type="string"),
        )
        with self.assertRaisesRegex(ParamValidationError, "required"):
            render_argv(tool, {})

    def test_value_stays_single_argv_element(self):
        # 空白やシェルメタ文字を含む値も 1 argv 要素のまま (shell 経路なし)
        tool = self.tool(
            ["echo", "{m}"],
            m=ParamSpec(name="m", type="string"),
        )
        payload = "a b; rm -rf / && echo $(pwd) | cat"
        self.assertEqual(["echo", payload], render_argv(tool, {"m": payload}))

    def test_injection_dash_value_rejected_at_render(self):
        tool = self.tool(
            ["gh", "pr", "view", "{number}"],
            number=ParamSpec(name="number", type="string"),
        )
        with self.assertRaisesRegex(ParamValidationError, "starting with '-'"):
            render_argv(tool, {"number": "--web"})

    def test_array_expands_with_forced_flag_after(self):
        tool = self.tool(
            ["gcloud", "{args}", "--project=pinned"],
            args=ParamSpec(name="args", type="array"),
        )
        self.assertEqual(
            ["gcloud", "compute", "instances", "list", "--project=pinned"],
            render_argv(tool, {"args": ["compute", "instances", "list"]}),
        )

    def test_empty_array_expands_to_zero_elements(self):
        tool = self.tool(
            ["ls", "{args}"],
            args=ParamSpec(name="args", type="array", required=False, default=[]),
        )
        self.assertEqual(["ls"], render_argv(tool, {}))

    def test_array_item_with_spaces_stays_single_element(self):
        tool = self.tool(
            ["echo", "{args}"],
            args=ParamSpec(name="args", type="array"),
        )
        self.assertEqual(
            ["echo", "a b; rm -rf /"],
            render_argv(tool, {"args": ["a b; rm -rf /"]}),
        )

    def test_missing_required_array_is_error(self):
        tool = self.tool(
            ["ls", "{args}"],
            args=ParamSpec(name="args", type="array"),
        )
        with self.assertRaisesRegex(ParamValidationError, "required"):
            render_argv(tool, {})

    def test_array_in_non_exact_element_rejected_at_render(self):
        # ロード時検証を通らない ToolSpec 直組みへの防御
        tool = self.tool(
            ["echo", "--x={args}"],
            args=ParamSpec(name="args", type="array"),
        )
        with self.assertRaisesRegex(ParamValidationError, "entire argv element"):
            render_argv(tool, {"args": ["v"]})

    def test_multiple_placeholders_in_one_element(self):
        tool = self.tool(
            ["gh", "api", "repos/{repo}/pulls/{number}"],
            repo=ParamSpec(name="repo", type="string"),
            number=ParamSpec(name="number", type="integer"),
        )
        self.assertEqual(
            ["gh", "api", "repos/o/r/pulls/12"],
            render_argv(tool, {"repo": "o/r", "number": 12}),
        )


if __name__ == "__main__":
    unittest.main()
