"""パラメータ検証と argv レンダリングのテスト (インジェクション拒否を含む)。

実行: uv run pytest
"""
import unittest

from cli_wrap_mcp.rendering import placeholders, render_argv, validate_param
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

    # --- 波括弧の扱い (placeholder は `{param 名}` のみ、他はリテラル) ------

    def test_jq_object_is_literal(self):
        tool = self.tool(["jq", "{name: .title, sha: .headRefOid}"])
        self.assertEqual(
            ["jq", "{name: .title, sha: .headRefOid}"],
            render_argv(tool, {}),
        )

    def test_go_template_is_literal_without_escaping(self):
        # `{` に隣接する `{name}` は placeholder ではない (Go template がそのまま書ける)
        tool = self.tool(
            ["gh", "pr", "list", "--template", '{{range .}}{{.number}}{{"\\n"}}{{end}}'],
        )
        self.assertEqual(
            ["gh", "pr", "list", "--template", '{{range .}}{{.number}}{{"\\n"}}{{end}}'],
            render_argv(tool, {}),
        )

    def test_brace_shaped_literals_pass_through(self):
        for element in (
            "{print $1}",                                  # awk
            "[print(f'line {i:03d}') for i in range(10)]",  # Python f-string
            "{ sha }",                                     # jq shorthand (空白あり)
            "{{name: .name}}",                             # 0.2.x で素通ししていた綴り
            "{}", "{p.__class__}", "a{", "a}",             # 対にならない/式に見える形
            r"s/a\{2,3\}/x/",                              # BRE の interval (escape 対象外)
        ):
            with self.subTest(element=element):
                self.assertEqual(
                    ["cmd", element], render_argv(self.tool(["cmd", element]), {}),
                )

    def test_literal_placeholder_shape_is_written_with_backslash(self):
        # `{識別子}` そのものを渡したいときだけ escape が要る
        tool = self.tool(["curl", "-w", r"%\{http_code}", "-o", "/dev/null"])
        self.assertEqual(
            ["curl", "-w", "%{http_code}", "-o", "/dev/null"],
            render_argv(tool, {}),
        )

    def test_escape_and_placeholder_coexist_in_one_element(self):
        # raw opt-out ではなく文字単位の escape にした理由がこれ (混在要素で効く)
        tool = self.tool(
            ["curl", "-w", r"%\{http_code} {fmt}"],
            fmt=ParamSpec(name="fmt", type="string"),
        )
        self.assertEqual(
            ["curl", "-w", "%{http_code} json"],
            render_argv(tool, {"fmt": "json"}),
        )

    def test_double_backslash_keeps_backslash_and_substitutes(self):
        # 0.2.x で `\{repo}` が持っていた意味 (リテラルの `\` + 置換) の綴り。
        # `\{repo}` 自体は escape になったので、これが 0.2.x と唯一非互換な綴り
        tool = self.tool(
            ["echo", r"\\{repo}"],
            repo=ParamSpec(name="repo", type="string"),
        )
        self.assertEqual(["echo", r"\own/repo"], render_argv(tool, {"repo": "own/repo"}))

    def test_placeholder_followed_by_brace_is_substituted(self):
        # 右隣の `}` は placeholder 判定に影響しない (jq の `{"count": {n}}`)
        tool = self.tool(
            ["jq", "-n", '{"count": {n}}'],
            n=ParamSpec(name="n", type="integer"),
        )
        self.assertEqual(["jq", "-n", '{"count": 3}'], render_argv(tool, {"n": 3}))

    def test_double_brace_stays_literal_next_to_placeholder(self):
        # 0.2.x はこの綴りを unescape していた (移行注記の対象)
        tool = self.tool(
            ["jq", "-n", '{{"count": {n}}}'],
            n=ParamSpec(name="n", type="integer"),
        )
        self.assertEqual(["jq", "-n", '{{"count": 3}}'], render_argv(tool, {"n": 3}))

    def test_backslash_run_before_placeholder_consumes_one(self):
        tool = self.tool(["echo", "PLACEHOLDER"], repo=ParamSpec(name="repo", type="string"))
        for element, expected in (
            (r"\{repo}", "{repo}"),          # escape
            (r"\\{repo}", r"\own/repo"),     # リテラルの `\` + 置換
            (r"\\\{repo}", r"\{repo}"),      # リテラルの `\` + escape
            (r"\\\\{repo}", r"\\own/repo"),
            (r"\\{repo}}", r"\own/repo}"),   # 右隣の `}` は判定に影響しない
        ):
            with self.subTest(element=element):
                tool.argv = ["echo", element]
                self.assertEqual(
                    ["echo", expected], render_argv(tool, {"repo": "own/repo"}),
                )

    def test_consecutive_escapes(self):
        tool = self.tool(["awk", r"\{print} \{next}"])
        self.assertEqual(["awk", "{print} {next}"], render_argv(tool, {}))

    def test_escape_before_non_placeholder_brace_is_passed_through(self):
        # `\` が特別なのは placeholder の直前だけ。それ以外は素の文字として渡る
        tool = self.tool(["awk", r"\{print $1}"])
        self.assertEqual(["awk", r"\{print $1}"], render_argv(tool, {}))

    def test_reported_names_match_substituted_sites(self):
        # placeholders() の列挙と実際の置換箇所がずれると未定義検出に穴があく
        element = r"{a}{{a}}\{a}{a}}%\{a} {b}"
        tool = self.tool(
            ["echo", element],
            a=ParamSpec(name="a", type="string"),
            b=ParamSpec(name="b", type="string"),
        )
        self.assertEqual(["a", "a", "b"], placeholders(element))
        self.assertEqual(
            [r"echo", r"A{{a}}{a}A}%{a} B"],
            render_argv(tool, {"a": "A", "b": "B"}),
        )

    def test_element_without_braces_is_unchanged(self):
        tool = self.tool(["echo", "a b; rm -rf /", "--flag=v"])
        self.assertEqual(["echo", "a b; rm -rf /", "--flag=v"], render_argv(tool, {}))

    def test_param_value_containing_brace_is_not_reinterpreted(self):
        # 値側の波括弧は再解釈されない (置換は 1 パス)
        tool = self.tool(
            ["echo", "{m}"],
            m=ParamSpec(name="m", type="string"),
        )
        self.assertEqual(["echo", r"{x} {{y}} \{z}"], render_argv(tool, {"m": r"{x} {{y}} \{z}"}))

    def test_array_item_containing_brace_is_not_reinterpreted(self):
        tool = self.tool(
            ["echo", "{args}"],
            args=ParamSpec(name="args", type="array"),
        )
        self.assertEqual(
            ["echo", "{name: .name}", "{other}"],
            render_argv(tool, {"args": ["{name: .name}", "{other}"]}),
        )

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
