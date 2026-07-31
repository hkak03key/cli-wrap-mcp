"""argv テンプレートの取り扱い: プレースホルダ列挙・値の検証・argv レンダリング。

パラメータ値は検証 (type / pattern fullmatch / deny_pattern / enum / dash guard) を
通過してから argv 要素に埋め込む。array param は要素全体の placeholder のみに展開を
許し、各 item に同じ検証を適用する。

置換は str.format ではなく単一パスの明示的な置換で行う (issue #6)。argv 要素のうち
「`{` に隣接せず、param 名の形をした `{name}`」だけが placeholder で、それ以外の
波括弧はすべてリテラル。したがって jq の `{a: .b}`・Go template の `{{range .}}`・
awk の `{print $1}` はそのまま書ける。リテラルの `{name}` (awk の `{print}`、
curl の `%{http_code}` など) は `\\{name}` と escape する。
"""
from __future__ import annotations

import re
from typing import Any

from cli_wrap_mcp.spec import (
    PARAM_NAME_PATTERN,
    PY_TYPES,
    ParamSpec,
    ParamValidationError,
    ToolSpec,
)


# 未定義 placeholder は「波括弧を書こうとして踏む」のが大半なので、
# 「書けない」ではなく「こう書けば書ける」まで案内する (issue #6)
BRACE_HINT = r" (write '\{name}' for a literal brace)"

# placeholder の字形。直前が `{` のものは placeholder ではない
# (Go template の `{{end}}` を置換しないため。右隣の `}` は見ない: 見てしまうと
#  jq の `{"count": {n}}` のように `}` が続くだけの placeholder が黙って literal になる)
_PLACEHOLDER = rf"(?<!\{{)\{{{PARAM_NAME_PATTERN}\}}"
# 左から 1 回だけ走査する。placeholder の直前の `\` の連なりは escape として解釈し、
# `\\` 2 つでリテラルの `\` 1 つ、余った `\` 1 つが placeholder を escape する
# (`\{a}` -> `{a}`、`\\{a}` -> `\` + 置換、`\\\{a}` -> `\` + `{a}`)。
# placeholder が続かない `\` は素の文字のまま (BRE の `s/a\{2,3\}/x/` は無傷)
_ELEMENT_RE = re.compile(
    rf"(?P<backslashes>\\+)(?P<escaped>{_PLACEHOLDER})"
    rf"|(?P<placeholder>{_PLACEHOLDER})"
)


def _substitute(template: str, resolve) -> str:
    """argv 要素を 1 パス走査し、placeholder を resolve(name) の返り値で置き換える。"""
    def replace(m: re.Match[str]) -> str:
        if (placeholder := m.group("placeholder")) is not None:
            return resolve(placeholder[1:-1])
        escaped = m.group("escaped")
        count = len(m.group("backslashes"))
        literal = "\\" * (count // 2)
        return literal + (escaped if count % 2 else resolve(escaped[1:-1]))

    return _ELEMENT_RE.sub(replace, template)


def placeholders(template: str) -> list[str]:
    """argv 要素中のプレースホルダ名を列挙する (escape された `\\{name}` は含まない)。"""
    names: list[str] = []

    def collect(name: str) -> str:
        names.append(name)
        return ""

    _substitute(template, collect)
    return names


def _validate_item(spec: ParamSpec, item: Any, index: int) -> str:
    """array param の 1 item を検証する (enum / pattern / deny / dash guard は per-item)。"""
    label = f"{spec.name!r}[{index}]"
    if not isinstance(item, str):
        raise ParamValidationError(
            f"param {label}: expected string item, got {type(item).__name__}"
        )
    if spec.enum is not None and item not in spec.enum:
        raise ParamValidationError(f"param {label}: value {item!r} is not in enum {spec.enum}")
    if spec.pattern is not None and not re.fullmatch(spec.pattern, item):
        raise ParamValidationError(
            f"param {label}: value {item!r} does not match pattern {spec.pattern!r}"
        )
    if spec.deny_pattern is not None and re.fullmatch(spec.deny_pattern, item):
        raise ParamValidationError(
            f"param {label}: value {item!r} is denied by deny_pattern {spec.deny_pattern!r}"
        )
    if item.startswith("-") and not spec.allow_dash_prefix:
        raise ParamValidationError(
            f"param {label}: value starting with '-' is rejected "
            "(set allow_dash_prefix = true in config to allow)"
        )
    return item


def validate_param(spec: ParamSpec, value: Any) -> str | list[str]:
    """値を検証し、argv に埋め込む文字列表現 (array param は item リスト) を返す。"""
    if spec.type == "array":
        if not isinstance(value, list):
            raise ParamValidationError(
                f"param {spec.name!r}: expected array of strings, got {type(value).__name__}"
            )
        return [_validate_item(spec, item, i) for i, item in enumerate(value)]
    py_type = PY_TYPES[spec.type]
    if not isinstance(value, py_type) or (py_type is int and isinstance(value, bool)):
        raise ParamValidationError(
            f"param {spec.name!r}: expected {spec.type}, got {type(value).__name__}"
        )
    if spec.enum is not None and value not in spec.enum:
        raise ParamValidationError(
            f"param {spec.name!r}: value {value!r} is not in enum {spec.enum}"
        )
    if spec.pattern is not None and not re.fullmatch(spec.pattern, value):
        raise ParamValidationError(
            f"param {spec.name!r}: value {value!r} does not match pattern {spec.pattern!r}"
        )
    if spec.deny_pattern is not None and re.fullmatch(spec.deny_pattern, value):
        raise ParamValidationError(
            f"param {spec.name!r}: value {value!r} is denied by "
            f"deny_pattern {spec.deny_pattern!r}"
        )
    if spec.type == "boolean":
        rendered = "true" if value else "false"
    else:
        rendered = str(value)
    # 引数インジェクション対策: `-` 始まりの値はオプションとして解釈されうるので既定で拒否
    if rendered.startswith("-") and not spec.allow_dash_prefix:
        raise ParamValidationError(
            f"param {spec.name!r}: value starting with '-' is rejected "
            "(set allow_dash_prefix = true in config to allow)"
        )
    return rendered


def render_argv(tool: ToolSpec, arguments: dict[str, Any]) -> list[str]:
    """検証済みパラメータで argv テンプレートを埋め、実行可能な argv を返す。

    array param は placeholder 単独の要素を N 要素に展開する (0 要素も可)。
    placeholder 以外の波括弧はリテラル (モジュール docstring 参照)。
    """
    rendered_params: dict[str, str | list[str]] = {}
    for pname, spec in tool.params.items():
        if pname in arguments and arguments[pname] is not None:
            value = arguments[pname]
        elif spec.has_default:
            value = spec.default
        elif spec.required:
            raise ParamValidationError(f"param {pname!r} is required")
        else:
            continue  # 未参照の optional (ロード時に argv 参照は禁止済み)
        rendered_params[pname] = validate_param(spec, value)

    argv: list[str] = []
    for element in tool.argv:
        names = placeholders(element)
        values = {n: rendered_params[n] for n in names}
        list_names = [n for n, v in values.items() if isinstance(v, list)]
        if list_names:
            # ロード時に検証済みだが、ToolSpec 直組みに対する防御として再確認する
            name = list_names[0]
            if len(names) != 1 or element != "{" + name + "}":
                raise ParamValidationError(
                    f"param {name!r}: array value must fill an entire argv element"
                )
            argv.extend(values[name])
            continue
        argv.append(_substitute(element, lambda name: values[name]))
    return argv
