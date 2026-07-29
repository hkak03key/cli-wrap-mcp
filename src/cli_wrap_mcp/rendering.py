"""argv テンプレートの取り扱い: プレースホルダ列挙・値の検証・argv レンダリング。

パラメータ値は検証 (type / pattern fullmatch / deny_pattern / enum / dash guard) を
通過してから argv 要素に埋め込む。array param は要素全体の placeholder のみに展開を
許し、各 item に同じ検証を適用する。
"""
from __future__ import annotations

import re
import string
from typing import Any

from cli_wrap_mcp.spec import (
    PY_TYPES,
    ConfigError,
    ParamSpec,
    ParamValidationError,
    ToolSpec,
)


def placeholders(template: str) -> list[str]:
    """format テンプレート中のプレースホルダ名を列挙する。

    format spec / conversion / ネストしたフィールドアクセスは安全のため禁止する。
    """
    names: list[str] = []
    for _literal, field_name, format_spec, conversion in string.Formatter().parse(template):
        if field_name is None:
            continue
        if field_name == "":
            raise ConfigError(f"positional placeholder '{{}}' is not allowed: {template!r}")
        if format_spec or conversion:
            raise ConfigError(
                f"format spec / conversion is not allowed in placeholder: {template!r}"
            )
        if "." in field_name or "[" in field_name:
            raise ConfigError(
                f"attribute/index access is not allowed in placeholder: {template!r}"
            )
        names.append(field_name)
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
        if not names:
            argv.append(element)
            continue
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
        argv.append(element.format(**values))
    return argv
