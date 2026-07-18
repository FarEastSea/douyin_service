import ast
import json
from json import JSONDecodeError
from typing import Any, Optional


def normalize_image_urls(raw_value: Any) -> list[str]:
    if raw_value is None:
        return []

    if isinstance(raw_value, (list, tuple)):
        return [text for text in (str(item).strip() for item in raw_value) if text]

    if not isinstance(raw_value, str):
        text = str(raw_value).strip()
        return [text] if text else []

    text = raw_value.strip()
    if not text:
        return []

    parsed = _parse_structured_value(text)
    if isinstance(parsed, str):
        parsed_text = parsed.strip()
        return [parsed_text] if parsed_text else []
    if isinstance(parsed, (list, tuple)):
        return [item for item in (str(entry).strip() for entry in parsed) if item]
    if parsed is not None:
        parsed_text = str(parsed).strip()
        return [parsed_text] if parsed_text else []

    line_parts = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    if len(line_parts) > 1:
        return line_parts

    if "," in text:
        comma_parts = [part.strip() for part in text.split(",") if part.strip()]
        if len(comma_parts) > 1:
            return comma_parts

    return [text]


def prepare_image_urls_for_storage(raw_value: Any) -> list[str]:
    if raw_value is None:
        return []

    if isinstance(raw_value, (list, tuple)):
        return [text for text in (str(item).strip() for item in raw_value) if text]

    if not isinstance(raw_value, str):
        text = str(raw_value).strip()
        return [text] if text else []

    text = raw_value.strip()
    if not text:
        return []

    parsed = _parse_structured_value(text)
    if isinstance(parsed, str):
        parsed_text = parsed.strip()
        return [parsed_text] if parsed_text else []
    if isinstance(parsed, (list, tuple)):
        return [item for item in (str(entry).strip() for entry in parsed) if item]
    if parsed is not None:
        parsed_text = str(parsed).strip()
        return [parsed_text] if parsed_text else []

    return [text]


def normalize_optional_urls(raw_value: Any) -> list[Optional[str]]:
    """解析按图集索引对齐的可空 URL 列表，保留其中的空占位。"""
    if raw_value is None:
        return []

    parsed = _parse_structured_value(raw_value.strip()) if isinstance(raw_value, str) else raw_value
    if not isinstance(parsed, (list, tuple)):
        return []

    return [
        text if item is not None and (text := str(item).strip()) else None
        for item in parsed
    ]


def prepare_optional_urls_for_storage(raw_value: Any) -> list[Optional[str]]:
    if not isinstance(raw_value, (list, tuple)):
        return []
    return [
        text if item is not None and (text := str(item).strip()) else None
        for item in raw_value
    ]


def _parse_structured_value(text: str) -> Any:
    try:
        return json.loads(text)
    except JSONDecodeError:
        pass

    try:
        return ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return None