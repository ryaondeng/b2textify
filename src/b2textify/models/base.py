"""数据模型基类。

序列化约定（这是"存档可长期读取"的基础）：

- `to_dict()` 用 dataclasses.asdict，字段名即 JSON 键名 —— 属对外契约，改名等于破坏兼容
- `from_dict()` 容忍未知字段、容忍缺字段（用默认值）

第二条是有意为之：**只加字段、不改语义**。将来给某个模型加字段时，旧的 works 存档
仍然能被读出来（新字段取默认值），不需要写迁移器；只有当字段**语义**变了才需要迁移。
"""

from __future__ import annotations

import dataclasses
import types
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

T = TypeVar("T", bound="Serializable")

# Optional[X]（typing.Union）与 X | None（types.UnionType）都要认
_UNION_TYPES = (types.UnionType, Union)


class Serializable:
    """可序列化的 dataclass 基类。"""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)  # type: ignore[arg-type]

    @classmethod
    def from_dict(cls: type[T], data: dict[str, Any]) -> T:
        if not isinstance(data, dict):
            raise TypeError(f"{cls.__name__}.from_dict 需要 dict，收到 {type(data).__name__}")
        hints = get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):  # type: ignore[arg-type]
            if f.name in data:
                kwargs[f.name] = _coerce(hints.get(f.name, Any), data[f.name])
        return cls(**kwargs)


def _coerce(tp: Any, value: Any) -> Any:
    """按声明类型把 JSON 值还原成 Python 对象（支持嵌套 dataclass / list / dict / Optional）。"""
    if value is None:
        return None

    origin = get_origin(tp)
    if origin in _UNION_TYPES:  # Optional[X] / X | None
        args = [a for a in get_args(tp) if a is not type(None)]
        return _coerce(args[0], value) if args else value
    if origin is list:
        args = get_args(tp)
        inner = args[0] if args else Any
        return [_coerce(inner, item) for item in value]
    if origin is dict:
        args = get_args(tp)
        if len(args) == 2 and _is_serializable(args[1]):
            return {key: args[1].from_dict(item) for key, item in value.items()}
        return dict(value)

    if _is_serializable(tp):
        return tp.from_dict(value)
    if tp is float and isinstance(value, int):
        return float(value)
    return value


def _is_serializable(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, Serializable)
