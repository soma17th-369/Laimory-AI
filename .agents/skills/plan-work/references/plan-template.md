# 실행계획 템플릿

아래 골격을 복사해 실제 내용으로 채운다. 빈 설명용 주석은 제거한다.

```markdown
---
title: 작업 제목
status: draft
created_at: YYYY-MM-DDTHH:MM:SS+09:00
updated_at: YYYY-MM-DDTHH:MM:SS+09:00
approved_at:
approved_by:
completed_at:
issue:
branch:
---

# 작업 제목

## 목표

이 작업으로 달성할 결과를 적는다.

## 범위

- 포함할 변경
- 포함할 변경

## 제외 범위

- 이번 작업에서 하지 않을 것

## 현재 구조

- 확인한 관련 파일과 현재 동작
- 보존해야 할 계약과 기존 사용자 변경

## 실행 단계

- [ ] 1. 구체적인 변경 단계
- [ ] 2. 구체적인 변경 단계
- [ ] 3. 테스트와 검증

## 검증

- 실행할 테스트·정적 검사·수동 확인
- 완료를 판단할 기준

## 위험 및 결정 사항

- 사용자가 확인해야 할 선택
- 실패 가능성과 롤백 또는 대응 방법

## 승인 기록

- 승인 상태: 대기
- 승인 내용:
```

상태는 `draft → approved → in_progress → completed` 순서로 바꾼다. 중단 시에는 `blocked` 또는 `cancelled`를 사용한다. 승인 뒤 계획이 실질적으로 변경되면 `draft`로 되돌리고 재승인받는다.
