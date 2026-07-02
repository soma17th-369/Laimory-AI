"""Photo Event Agent 검증 (LLM 은 fake 주입)."""

from app.agents.events.photo import PhotoEventAgent
from app.schemas import EventSourceType
from tests.fixtures.fake_llm import FakeLLM, fragment, result_json
from tests.fixtures.requests import DAY_START, HOUR, make_request, photo


def test_empty_photos_skips_llm():
    fake = FakeLLM([result_json()])
    result = PhotoEventAgent(llm=fake).generate(make_request())
    assert result.fragments == []
    assert fake.calls == []


def test_missing_photos_skips_llm():
    fake = FakeLLM([result_json()])
    req = make_request().model_copy(update={"photos": None})
    result = PhotoEventAgent(llm=fake).generate(req)
    assert result.candidates == []
    assert result.fragments == []
    assert result.warnings == []
    assert fake.calls == []


def test_photo_inferred_as_fragment():
    final = result_json(fragments=[fragment("PHOTO", "photo-1", "음식 사진")])
    fake = FakeLLM([final])
    req = make_request(photos=[photo("photo-1", DAY_START + HOUR)])

    result = PhotoEventAgent(llm=fake).generate(req)

    # 단일 호출 agent.
    assert len(fake.calls) == 1
    assert "라이프로그" in fake.calls[0].system
    assert "photo-1" in fake.calls[0].prompt
    assert result.candidates == []
    assert len(result.fragments) == 1
    assert result.fragments[0].source_type is EventSourceType.PHOTO
    assert result.fragments[0].source_id == "photo-1"
