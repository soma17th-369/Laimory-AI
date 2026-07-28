"""구조화 출력(structured output) 공통 로직.

LLM 응답을 정해진 Pydantic 스키마로 강제하기 위한 골격이다. 핵심은 두 겹이다.

    1. provider 가 JSON 형태를 최대한 잠근다(형태: JSON·필수 필드·enum).
    2. 우리 Pydantic 스키마가 값·교차 규칙을 최종 검증한다(`ge/le`·`min_length`·
       `endTime≥startTime` 등은 provider 가 강제할 수 없다).

provider 가 형태를 아무리 잠가도 값·교차 규칙은 코드가 확정해야 하므로, 검증 실패
시에는 오류를 프롬프트에 붙여 한 번 더 요청한다(교정 재시도). 재시도까지 실패하면
``StructuredOutputError`` 를 던지고, 상위(각 Agent)가 이를 warning 으로 흡수한다.

이 모듈은 provider 종류를 모른다. 실제 호출은 ``structured_fn`` 콜러블로 주입받으므로,
``LLMProvider`` 와 테스트용 ``FakeLLM`` 이 같은 검증·재시도 로직을 공유한다.
"""

import json
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)

#: ``structured_fn`` 시그니처: (prompt, schema, *, system, temperature, **kwargs) -> str.
#: JSON 텍스트를 돌려줘야 한다. 형태 강제 여부는 구현마다 다르지만, 검증은 항상 아래가 한다.
StructuredFn = Callable[..., str]


class StructuredOutputError(Exception):
    """구조화 출력이 스키마 검증을 통과하지 못했다(재시도 소진 포함)."""

    def __init__(self, message: str, *, errors: str | None = None) -> None:
        self.errors = errors
        super().__init__(message)


def extract_json_object(text: str) -> str:
    """앞뒤 코드펜스·설명이 섞여 있어도 첫 ``{`` 부터 마지막 ``}`` 까지를 잘라낸다.

    최상위 계약이 모두 JSON object 라 object 경계만 본다. object 를 찾지 못하면
    ``StructuredOutputError`` 를 던져 재시도 대상이 되게 한다.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise StructuredOutputError("LLM 응답에서 JSON 객체를 찾지 못했습니다.")
    return text[start : end + 1]


def schema_hint(schema: type[BaseModel]) -> str:
    """스키마를 프롬프트에 실어 형태를 안내하는 지시문(provider 네이티브 강제가 없을 때).

    별칭(camelCase)이 실제 LLM 계약이므로 ``by_alias=True`` 로 스키마를 뽑는다.
    """

    schema_json = json.dumps(schema.model_json_schema(by_alias=True), ensure_ascii=False)
    return (
        "다음 JSON 스키마를 정확히 따르는 JSON 객체만 출력하세요. "
        "코드펜스·주석·설명 없이 JSON 만 출력합니다.\n"
        f"[JSON 스키마]\n{schema_json}"
    )


#: strict 스키마에서 떼어낼 값 제약·메타 키. provider 가 강제하지 못하거나(minLength·
#: ge/le 등) strict 모드가 거부하는(format·default) 것들이다. 이 값 규칙은 이후 우리
#: Pydantic 이 검증한다. 구조·필수·enum 은 남겨 provider 가 강제하게 둔다.
_STRICT_STRIP_KEYS = frozenset(
    {
        "default",
        "title",
        "examples",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)


def to_strict_schema(model: type[BaseModel]) -> dict | None:
    """Pydantic 모델의 JSON 스키마를 provider strict 모드용으로 변환한다.

    OpenAI ``json_schema(strict)`` / Gemini ``response_schema`` 가 **필수 필드와 enum 을
    강제**하도록, 모든 object 에 ``additionalProperties=false`` 를 박고 모든 property 를
    ``required`` 로 만든다(optional 은 이미 nullable 이라 모델이 ``null`` 을 낼 수 있다).
    provider 가 강제 못 하는 값 제약(minLength·ge/le·format 등)과 default 는 뗀다 —
    그 규칙은 이후 우리 Pydantic 이 검증한다.

    자유형 object(``dict[str, Any]`` 등 properties 없는 object)가 있으면 strict 모드가
    이를 거부하므로 ``None`` 을 돌려준다. 호출자는 그때 provider 의 일반 JSON 모드로
    떨어진다(형태는 프롬프트가 지시하고 검증은 Pydantic 이 한다).
    """

    schema = model.model_json_schema(by_alias=True)
    if not _strictify(schema):
        return None
    return schema


def _strictify(node: object) -> bool:
    """``node`` 를 strict 방언으로 in-place 변환한다. strict 불가면 False."""

    ok = True
    if isinstance(node, dict):
        for key in list(node):
            if key in _STRICT_STRIP_KEYS:
                del node[key]
        if node.get("type") == "object":
            if "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            else:
                ok = False  # 자유형 object(dict[str, Any]) — strict 불가
        for key, value in node.items():
            # properties/$defs 는 "이름 → 스키마" 매핑이다. 이 매핑 자체에
            # _strictify를 적용하면 title 같은 정상 필드/모델 이름을 JSON Schema의
            # 메타 키로 오인해 삭제한다. 각 값(실제 스키마)만 변환한다.
            if key in {"properties", "$defs"} and isinstance(value, dict):
                for child_schema in value.values():
                    if not _strictify(child_schema):
                        ok = False
            elif not _strictify(value):
                ok = False
    elif isinstance(node, list):
        for item in node:
            if not _strictify(item):
                ok = False
    return ok


def _repair_prompt(prompt: str, error: str) -> str:
    """검증 실패 오류를 붙여 교정 재시도를 요청하는 프롬프트."""

    return (
        f"{prompt}\n\n"
        "[직전 응답이 스키마 검증에 실패했습니다]\n"
        f"검증 오류:\n{error}\n\n"
        "위 오류를 고쳐, 지정된 스키마에 정확히 맞는 JSON 객체만 다시 출력하세요. "
        "코드펜스나 설명 없이 JSON 만."
    )


def run_structured(
    structured_fn: StructuredFn,
    prompt: str,
    schema: type[ModelT],
    *,
    system: str | None = None,
    temperature: float = 0.7,
    max_repairs: int = 1,
    **kwargs,
) -> ModelT:
    """``structured_fn`` 으로 JSON 을 받아 ``schema`` 로 검증한다.

    검증 실패 시 오류를 프롬프트에 붙여 최대 ``max_repairs`` 회 재요청한다. 모두
    실패하면 ``StructuredOutputError`` 를 던진다. provider 네이티브 강제가 있든 없든
    최종 계약 준수는 여기(우리 Pydantic 검증)가 보장한다.
    """

    attempt_prompt = prompt
    last_error: str | None = None
    for _ in range(max_repairs + 1):
        text = structured_fn(
            attempt_prompt,
            schema,
            system=system,
            temperature=temperature,
            **kwargs,
        )
        try:
            return schema.model_validate_json(extract_json_object(text))
        except (ValidationError, StructuredOutputError, ValueError) as exc:
            last_error = str(exc)
            attempt_prompt = _repair_prompt(prompt, last_error)

    raise StructuredOutputError(
        f"{schema.__name__} 스키마 검증에 {max_repairs + 1}회 모두 실패했습니다.",
        errors=last_error,
    )
