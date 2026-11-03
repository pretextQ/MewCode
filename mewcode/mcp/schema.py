"""Validate complete local MCP schemas without coercion or remote reference retrieval."""
from __future__ import annotations

from typing import Any, ClassVar

from jsonschema import validators  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, create_model, model_validator


def _check_refs(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {'$ref', '$dynamicRef', '$recursiveRef'} and (
                not isinstance(child, str) or not child.startswith('#')
            ):
                raise ValueError('MCP schema external references are not supported')
            _check_refs(child)
    elif isinstance(value, list):
        for child in value:
            _check_refs(child)


class MCPParams(BaseModel):
    model_config = ConfigDict(extra='allow', strict=True)
    _validator: ClassVar[Any]

    @model_validator(mode='before')
    @classmethod
    def validate_schema(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError('MCP arguments must be an object')
        try:
            cls._validator.validate(value)
        except Exception as exc:
            # Avoid echoing full parameter values (possibly credentials).
            path = '.'.join(str(p) for p in getattr(exc, 'absolute_path', []))
            raise ValueError(f'MCP schema validation failed at {path or "<root>"}') from None
        return value


def build_params_model(tool_name: str, schema: dict[str, Any]) -> type[BaseModel]:
    _check_refs(schema)
    validator = validators.validator_for(schema)
    validator.check_schema(schema)
    # All actual constraints are enforced above, retaining nested/union schemas,
    # explicit nulls and additional properties rather than rewriting their types.
    # Retain every input key as extra data, including names reserved by BaseModel.
    model = create_model(f'{tool_name}Params', __base__=MCPParams)
    model._validator = validator(schema)
    return model
