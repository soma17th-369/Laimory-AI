# Location Event Agent 검토(review) 프롬프트

아래는 1차 추론 초안(JSON)입니다. 과한 추론을 검토해 보수적으로 정리하세요.

- 근거가 약한 event 는 confidence 를 낮추거나 fragments 로 옮깁니다.
- inferenceLevel 이 UNCERTAIN 인데 uncertainty 가 비어 있으면 채웁니다.
- 입력에 없는 sourceId 를 참조하는 항목은 제거합니다.

system 프롬프트에 정의된 동일한 JSON 형식으로만 다시 출력하세요.

[초안]
{{DRAFT}}
