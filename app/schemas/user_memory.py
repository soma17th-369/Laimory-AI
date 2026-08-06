"""사용자 메모리 스키마 v1.0 (#65).

App Server 가 사용자의 확정된 기록에서 뽑아 유지하는 **압축 프로필**이다. 하루치
수집 원본과 달리 사건 데이터가 아니라 타임라인의 해석과 표현을 돕는 **보조
context** 다. 그래서 이 값만으로 사건·장소·인물 관계를 확정하지 않는다(사용 원칙은
Agent 프롬프트가 갖는다).

## 왜 고정 스키마인가

전에는 ``extra="allow"`` 자유형이었다. AI 가 채우는 값이라 형태를 열어 뒀는데,
소비 측이 무엇이 오는지 모르면 프롬프트 projection 을 안정적으로 만들 수 없고
결정론 코드가 우연히 특정 키에 기대게 된다. v1.0 은 최상위 필드를 고정하고,
자유도는 :attr:`UserMemory.custom_attributes` 안으로 가둔다.

``extra="forbid"`` 라서 모르는 최상위 필드는 검증에서 걸린다. 이 실패는 타임라인
생성을 멈추지 않는다 — 입력 조회 경계가 흡수하고(코드 1106) User Memory 없이
진행한다. 보조 context 하나 때문에 하루 기록을 버리지 않는다.

## 크기

각 자연어 필드 200자, ``customAttributes`` 는 5개·값당 150자다. 상한은 프롬프트
토큰을 지키려는 것이지 의미 규칙이 아니다. 넘치면 자르지 않고 거절한다 — 잘린
문장은 뜻이 달라지고, 그걸 근거로 쓴 해석은 되돌릴 방법이 없다.
"""

import json
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, StringConstraints

from app.schemas.common import CamelModel

#: 이 서버가 해석할 수 있는 유일한 User Memory 계약 버전.
SCHEMA_VERSION = "1.0"

#: 고정 자연어 필드 하나의 최대 길이.
NARRATIVE_MAX_LENGTH = 200

#: ``customAttributes`` 최대 개수와 값 하나의 최대 길이.
CUSTOM_ATTRIBUTE_MAX_COUNT = 5
CUSTOM_ATTRIBUTE_MAX_LENGTH = 150

NarrativeText = Annotated[str, StringConstraints(max_length=NARRATIVE_MAX_LENGTH)]
CustomAttributeText = Annotated[
    str, StringConstraints(max_length=CUSTOM_ATTRIBUTE_MAX_LENGTH)
]

#: 프롬프트에 실리는 자연어 필드(alias). **이 튜플 순서가 곧 프롬프트 키 순서다** —
#: 같은 메모리는 언제나 같은 문자열이 되어야 캐시·재현·diff 가 의미를 갖는다.
NARRATIVE_FIELDS: tuple[str, ...] = (
    "basicProfile",
    "lifeContext",
    "relationships",
    "personality",
    "values",
    "preferences",
    "routines",
    "currentFocus",
    "emotionalPatterns",
    "memoryStyle",
)

#: 프롬프트에 싣지 않는 메타데이터 필드(alias). 의미 생성에 쓰이지 않는다.
METADATA_FIELDS: tuple[str, ...] = ("schemaVersion", "updatedAt")


class UserMemory(CamelModel):
    """사용자 압축 프로필 v1.0 (보조 context).

    필드는 두 갈래로 쓰인다. ``basicProfile``·``lifeContext``·``relationships``·
    ``routines``·``currentFocus`` 는 상황과 사건 맥락을 해석하는 데,
    ``personality``·``values``·``preferences``·``emotionalPatterns``·
    ``memoryStyle`` 은 중요도 판단과 표현 선택에 참고한다. 이 구분은 프롬프트가
    지키며 코드는 강제하지 않는다 — 판단은 의미의 영역이다.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    #: 계약 버전. ``"1.0"`` 만 받는다. 다른 값은 검증에서 걸려 흡수 경로로 간다.
    schema_version: Literal["1.0"] = Field(
        default=SCHEMA_VERSION, alias="schemaVersion"
    )
    #: 마지막 갱신 시각(ISO 8601). 갱신 전략(#64)의 값이라 프롬프트에는 싣지 않는다.
    updated_at: str | None = Field(default=None, alias="updatedAt")

    basic_profile: NarrativeText = Field(default="", alias="basicProfile")
    life_context: NarrativeText = Field(default="", alias="lifeContext")
    relationships: NarrativeText = Field(default="")
    personality: NarrativeText = Field(default="")
    values: NarrativeText = Field(default="")
    preferences: NarrativeText = Field(default="")
    routines: NarrativeText = Field(default="")
    current_focus: NarrativeText = Field(default="", alias="currentFocus")
    emotional_patterns: NarrativeText = Field(default="", alias="emotionalPatterns")
    memory_style: NarrativeText = Field(default="", alias="memoryStyle")

    #: 고정 필드로 담기지 않는 값. **키는 AI 가 만든다** — 결정론 코드가 특정 키의
    #: 존재를 전제하지 않는다.
    custom_attributes: dict[str, CustomAttributeText] = Field(
        default_factory=dict,
        alias="customAttributes",
        max_length=CUSTOM_ATTRIBUTE_MAX_COUNT,
    )

    def prompt_payload(self) -> dict[str, Any]:
        """프롬프트에 실을 최소 형태로 접는다.

        빈 필드와 빈 ``customAttributes`` 는 뺀다. 빈 값을 남기면 모델이 "이 항목은
        비어 있다" 는 사실 자체를 근거로 삼을 수 있고, 토큰도 그만큼 쓴다.
        메타데이터(:data:`METADATA_FIELDS`)도 싣지 않는다.
        """

        dumped = self.model_dump(by_alias=True)
        payload: dict[str, Any] = {
            name: dumped[name] for name in NARRATIVE_FIELDS if dumped[name]
        }
        if self.custom_attributes:
            payload["customAttributes"] = {
                key: value for key, value in self.custom_attributes.items() if value
            }
        return payload

    def trace_summary(self) -> dict[str, Any]:
        """본문 없이 남길 수 있는 비식별 메타데이터.

        로그·관측에는 이것만 나간다. 어떤 값이 들어 있었는지는 여기서 알 수 없고,
        계약 버전과 채워진 정도만 알 수 있다.
        """

        payload = self.prompt_payload()
        narrative_count = sum(1 for name in NARRATIVE_FIELDS if name in payload)
        return {
            "schemaVersion": self.schema_version,
            "filledFieldCount": narrative_count,
            "customAttributeCount": len(self.custom_attributes),
            # 프롬프트에 실제로 실리는 문자열 길이다. 토큰 수를 재려면 provider 별
            # tokenizer 가 필요해 여기서는 provider 와 무관한 문자 수만 남긴다.
            "serializedChars": len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        }


def _assert_field_catalog_matches_model() -> None:
    """필드를 더하고 :data:`NARRATIVE_FIELDS` 갱신을 잊으면 import 시점에 터뜨린다.

    이 튜플이 모델과 갈리면 새 필드가 조용히 프롬프트에서 빠진다. 값은 들어와
    있는데 Agent 는 못 보는 상태라, 결과만 봐서는 원인을 찾기 어렵다.
    """

    declared = {
        field.alias or name for name, field in UserMemory.model_fields.items()
    }
    catalog = set(NARRATIVE_FIELDS) | set(METADATA_FIELDS) | {"customAttributes"}
    if declared != catalog:
        raise RuntimeError(
            "UserMemory 필드와 projection 카탈로그가 다릅니다: "
            f"모델에만 있음={sorted(declared - catalog)}, "
            f"카탈로그에만 있음={sorted(catalog - declared)}."
        )


_assert_field_catalog_matches_model()
