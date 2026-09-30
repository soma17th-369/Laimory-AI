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

각 자연어 필드와 ``customAttributes`` 값 하나는 500자다(#121). ``customAttributes`` 의
**개수는 제한하지 않는다** — 한 번 나온 정보도 남겨 이후 기록으로 보완하는 것이 갱신
정책이라, 개수를 세면 새 정보를 담으려고 옛 정보를 버리게 된다. 문서가 끝없이 커지는
것은 개수가 아니라 전체 크기 상한
(:data:`app.services.user_memory_limits.USER_MEMORY_MAX_CHARS`)이 막는다.

상한은 프롬프트 토큰을 지키려는 것이지 의미 규칙이 아니다. 넘치면 자르지 않고
거절한다 — 잘린 문장은 뜻이 달라지고, 그걸 근거로 쓴 해석은 되돌릴 방법이 없다.
"""

import json
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from app.schemas.common import CamelModel

#: 이 서버가 해석할 수 있는 유일한 User Memory 계약 버전.
SCHEMA_VERSION = "1.0"

#: 고정 자연어 필드 하나의 최대 길이.
NARRATIVE_MAX_LENGTH = 500

#: ``customAttributes`` 값 하나의 최대 길이. 개수 상한은 없다.
CUSTOM_ATTRIBUTE_MAX_LENGTH = 500

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
        default_factory=dict, alias="customAttributes"
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


#: 고정 자연어 필드의 alias → Python 속성 이름.
_ATTRIBUTE_BY_ALIAS: dict[str, str] = {
    (field.alias or name): name
    for name, field in UserMemory.model_fields.items()
    if (field.alias or name) in NARRATIVE_FIELDS
}

#: 변경 목록에서 ``customAttributes`` 의 속성 하나를 가리키는 ``item`` 의 접두사.
#: ``customAttributes.반려동물`` 은 키가 ``반려동물`` 인 속성이다.
CUSTOM_ATTRIBUTE_ITEM_PREFIX = "customAttributes."


# 아래 세 클래스는 **LLM 출력 계약**이다. Pydantic 은 클래스 docstring 을 JSON schema 의
# ``description`` 으로 내보내고 그 schema 가 구조화 출력 요청에 실려 provider 로 간다.
# 그래서 docstring 에는 모델이 읽어도 되는 한두 줄만 두고, 설계 이유는 주석으로 적는다.


class UserMemoryChangeAction(str, Enum):
    """항목 하나를 어떻게 바꿀지."""

    ADD = "추가"
    UPDATE = "수정"
    REMOVE = "삭제"


# ``추가`` 와 ``수정`` 은 같게 적용한다(그 항목을 ``text`` 로 둔다). 비어 있던 항목인지
# 내용이 있던 항목인지를 모델이 잘못 짚었다고 변경을 버릴 이유가 없다. 둘을 나눠 받는
# 것은 모델이 무엇을 하려는지 스스로 말하게 하기 위해서다.
#
# ``text`` 는 덧붙일 조각이 아니라 기존 내용과 합친 결과다. 코드는 문장을 이어 붙이지
# 않는다 — 겹치는 말을 합치고 충돌하는 말을 바꾸는 것은 의미 판단이다.
#
# 길이 제한은 항목 값 하나에 걸리고 저장 문서와 같은 값이다. 그래서 변경을 통과한 값은
# 적용한 뒤에도 문서 계약을 어기지 않는다.
#
# ``reason`` 은 모델이 ``text`` 를 쓰기 **전에** 적는 판단 과정이다. 추론이 꺼진 모델은
# 출력 말고는 생각할 자리가 없다. 이유 없이 변경만 받았을 때 실측에서 (1) 알게 된 것 하나를
# 여러 항목에 되풀이해 적고 (2) 주제가 다른 내용을 있던 속성에 몰아 적고 (3) 바꿀 것이
# 없는 항목을 같은 문장으로 다시 냈다. Repair 의 ``toolCalls[].reason`` 과 같은 자리다.
#
# **코드는 ``reason`` 을 읽지 않는다.** 저장하지도 않고 적용에 쓰지도 않는다. 그래서
# 비어 있어도 거절하지 않는다 — 생각을 적는 자리가 비었다고 변경까지 버릴 이유가 없다.
class UserMemoryChange(CamelModel):
    """변경 한 건. ``item`` 을 ``action`` 대로 바꾼다.

    ``item`` 은 고정 필드 이름이거나 ``customAttributes.<키>`` 다. ``reason`` 은 왜
    바꾸는지 한 문장이다. ``text`` 는 바꾼 뒤 그 항목에 남을 전체 문장이고, 삭제에서는
    ``null`` 이다.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    #: 선언 순서가 곧 모델이 쓰는 순서다. 이유가 문장보다 먼저 와야 그것을 보고 쓴다.
    item: str = Field(min_length=1)
    action: UserMemoryChangeAction
    reason: str = ""
    text: str | None = None

    @property
    def attribute_key(self) -> str | None:
        """``customAttributes`` 의 속성을 가리키면 그 키, 고정 필드면 ``None``."""

        if self.item.startswith(CUSTOM_ATTRIBUTE_ITEM_PREFIX):
            return self.item[len(CUSTOM_ATTRIBUTE_ITEM_PREFIX) :]
        return None

    @model_validator(mode="after")
    def validate_change(self) -> "UserMemoryChange":
        """가리키는 항목이 있는지, 넣을 문장이 있고 길이 안인지 본다.

        오류 문장은 교정 재시도 프롬프트에 그대로 실린다. 항목 이름과 숫자만 적고
        ``text`` 는 인용하지 않는다.
        """

        key = self.attribute_key
        if key is None and self.item not in NARRATIVE_FIELDS:
            raise ValueError(
                "item 은 고정 필드 이름이거나 "
                f"`{CUSTOM_ATTRIBUTE_ITEM_PREFIX}<키>` 여야 합니다."
            )
        if key is not None and not key.strip():
            raise ValueError("customAttributes 의 키가 비어 있습니다.")

        if self.action is UserMemoryChangeAction.REMOVE:
            return self

        text = (self.text or "").strip()
        if not text:
            raise ValueError("추가·수정에는 비어 있지 않은 text 가 필요합니다.")
        if len(text) > self.max_length:
            raise ValueError(
                f"항목 하나의 text 는 {self.max_length}자 이하여야 합니다({len(text)}자)."
            )
        return self

    @property
    def max_length(self) -> int:
        """이 변경이 가리키는 항목 값의 길이 제한."""

        if self.attribute_key is None:
            return NARRATIVE_MAX_LENGTH
        return CUSTOM_ATTRIBUTE_MAX_LENGTH


# v3 세트의 LLM 출력 계약이다(#121). 갱신할 때마다 모델이 문서 전체를 다시 쓰면 건드릴
# 이유가 없던 항목까지 조금씩 달라지거나 빠진다. 그래서 모델에게는 "어느 항목을
# 추가·수정·삭제할지" 만 받고 코드가 그것을 기존 문서에 끼워 넣는다. 목록에 없는 항목은
# 글자 하나 바뀌지 않는다.
#
# **문서 모양으로 받지 않는다.** 처음에는 문서와 같은 모양에 바꾸지 않는 필드만 ``null``
# 로 두게 했다. 그 모양은 모델에게 "새 문서를 만든다" 로 읽혀, 실측에서 바꾸지 않는
# 속성까지 같은 값으로 다시 적었다. 변경 목록에는 바꾸지 않는 항목을 적을 자리가 없다.
#
# 이것은 **AI 서버 안의 계약**이다. App Server 로 나가는 것은 언제나 적용을 마친 문서
# 전체(``UserMemory``)이고 저장 형식은 달라지지 않는다.
class UserMemoryPatch(CamelModel):
    """기존 프로필에 적용할 변경 목록. 바꿀 항목만 담고, 바꿀 것이 없으면 빈 목록이다."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    changes: list[UserMemoryChange] = Field(default_factory=list)

    def apply_to(self, memory: UserMemory | None) -> UserMemory:
        """기존 문서에 변경 목록을 적용한 새 문서를 돌려준다. 원본은 바꾸지 않는다.

        ``memory`` 가 ``None`` 이면 빈 문서에 적용한다. 기존 문서가 없는 것과 비어
        있는 것을 가를 이유가 없다 — 어느 쪽이든 채울 것만 채우면 된다.

        같은 항목이 목록에 두 번 나오면 뒤의 것이 이긴다. 없는 속성을 지우라는 것은
        아무 일도 하지 않는다.
        """

        base = memory if memory is not None else UserMemory()
        update: dict[str, Any] = {}
        attributes = dict(base.custom_attributes)

        for change in self.changes:
            removing = change.action is UserMemoryChangeAction.REMOVE
            text = "" if removing else (change.text or "").strip()
            key = change.attribute_key
            if key is None:
                update[_ATTRIBUTE_BY_ALIAS[change.item]] = text
            elif removing:
                attributes.pop(key, None)
            else:
                attributes[key] = text

        update["custom_attributes"] = attributes
        return base.model_copy(update=update)


def _assert_field_catalog_matches_model() -> None:
    """필드를 더하고 :data:`NARRATIVE_FIELDS` 갱신을 잊으면 import 시점에 터뜨린다.

    이 튜플이 모델과 갈리면 새 필드가 조용히 프롬프트에서 빠진다. 값은 들어와
    있는데 Agent 는 못 보는 상태라, 결과만 봐서는 원인을 찾기 어렵다. 변경 목록이
    가리킬 수 있는 고정 필드도 이 튜플이 정한다.
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
