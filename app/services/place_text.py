"""장소 텍스트 비교 유틸.

캘린더 메모(`집(경기도 오산시 운암로 90)`), 체류 장소명(`오산운암3단지 주공아파트`),
주소(`경기도 오산시 운암로 90`)는 같은 곳을 서로 다른 문자열로 부른다. 이 모듈은
그 문자열들을 비교 가능한 형태로 정규화하고, 같은 장소를 가리키는지 판정한다.

`calendar_location`(확증 시 confidence 보강)과 `place_resolver`(placeLabel/address
확정)가 함께 쓴다.
"""

import re

#: 장소 텍스트를 자르는 구분 기호(공백·괄호·문장부호).
_SEPARATORS = re.compile(r"[\s()\[\]{}<>,./·~\-_:;'\"]+")

#: 토큰 비교에 쓸 최소 길이. `시`, `동` 같은 한 글자 조각으로 매칭되지 않게 한다.
_MIN_TOKEN_LENGTH = 3


def normalize_place_text(text: str) -> str:
    """비교용 정규화: 구분 기호를 지우고 소문자로 만든다."""

    return _SEPARATORS.sub("", text).lower()


def place_tokens(text: str) -> set[str]:
    """의미 있는 토큰만 남긴다. 숫자만으로 된 토큰은 번지수 충돌을 만들어 제외한다."""

    return {
        token.lower()
        for token in _SEPARATORS.split(text)
        if len(token) >= _MIN_TOKEN_LENGTH and not token.isdigit()
    }


def place_text_contains(left: str | None, right: str | None) -> bool:
    """정규화 후 한쪽이 다른 쪽을 통째로 품는가.

    `집(경기도 오산시 운암로 90)` ⊃ `경기도 오산시 운암로 90` 을 잡는다. 토큰 겹침보다
    엄격해서, "이 주소가 정말 근거에 있었나" 를 확인할 때 쓴다.
    """

    if not left or not right:
        return False
    normalized_left, normalized_right = normalize_place_text(left), normalize_place_text(right)
    if len(normalized_left) < 2 or len(normalized_right) < 2:
        return False
    return normalized_left in normalized_right or normalized_right in normalized_left


def places_match(left: str | None, right: str | None) -> bool:
    """두 장소 텍스트가 같은 장소를 가리키는지 판정한다.

    한쪽이 다른 쪽을 통째로 품고 있거나(`집(경기도 오산시 운암로 90)` ⊃
    `경기도 오산시 운암로 90`), 의미 있는 토큰을 공유하면(`강남역 스타벅스` ↔
    `스타벅스 강남점`) 같은 장소로 본다.

    토큰 비교는 `오산시` 같은 행정구역 조각만 겹쳐도 참이 되므로 단독으로 쓰면
    느슨하다. **이미 같은 event 로 묶인 근거끼리** 비교한다는 전제 아래 쓴다.
    """

    if not left or not right:
        return False
    if place_text_contains(left, right):
        return True
    if len(normalize_place_text(left)) < 2 or len(normalize_place_text(right)) < 2:
        return False
    return bool(place_tokens(left) & place_tokens(right))
