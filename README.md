# 몽타주 시뮬레이터 — AI_TOP_100 [2025] 본선 2번 재현

작년 본선 문제 **"몽타주를 그려라"**를 로컬에서 그대로 연습하기 위한 채점 서버입니다.
실제 대회 API와 정답 얼굴은 공개되지 않아, 같은 규칙으로 동작하는 "AI 목격자 카오"를 재구성했습니다.

## 원래 문제 규칙 → 이 시뮬레이터

| 원래 규칙 | 구현 |
|---|---|
| 텍스트 목격 진술 제공 | 정답 얼굴의 특징값으로 진술 자동 생성 (`case/statement.txt`) |
| 제출 이미지에 유사도 + 부위별 피드백 | 전체 유사도(0~100) + 눈·눈썹·코·입·얼굴형·나이·성별 점수 |
| 1분당 1회 제출 | `--cooldown 60` (기본값) |
| 1024×1024, PNG/JPEG, 10MB 이하 | 서버에서 엄격히 검사, 위반 시 거부 |
| 동일 이미지(파일 해시) 재제출 무효 | sha256 비교, 평가 안 함 (쿨다운도 소모 안 함) |
| 이력은 최근 4건, 각 이력에 최고 유사도 표시 | `/history` 동일 |
| 최고 유사도 1장으로 최종 평가 | `reveal.py`에서 최고점 기준 |

### 채점 방식 (원래 대회의 내부 방식은 비공개라 추정해서 설계)
- **전체 인상(identity)**: ArcFace 얼굴 인식 임베딩의 코사인 유사도
- **부위별 점수**: 68개 얼굴 랜드마크로 측정한 비율(눈 크기, 미간, 코 길이·폭, 입 너비, 입술 두께, 얼굴 비율, 턱 등)을 정답과 비교. 차이는 얼굴 풀의 표준편차로 정규화
- **유사도 = 0.5 × identity + 0.5 × 부위 가중평균**, 성별이 다르면 ×0.6
- 그래서 부위 비율만 맞춰서는 절반까지만 오르고, **전체 인상까지 비슷해야** 고득점이 나옵니다

## 설치 (macOS, Apple Silicon 기준)

```bash
cd montage-sim
python3.11 -m venv .venv && source .venv/bin/activate   # 3.10~3.13 가능
pip install -r requirements.txt
```
- `insightface` 설치 중 컴파일 오류가 나면 `xcode-select --install`로 Command Line Tools를 깔고 다시 시도하세요.
- 첫 실행 때 얼굴 모델(buffalo_l, 약 280MB)을 GitHub에서 자동으로 받습니다 (`~/.insightface/`).

## 사용법

### 1. 사건 만들기 (정답 얼굴을 몰래 정함)
```bash
python setup_case.py            # thispersondoesnotexist.com 에서 가상 인물 20장 자동 수집 → 1장을 랜덤으로 정답 지정
```
- 수집한 얼굴은 화면에 띄우지 않고, 정답 1장만 `case/.answer/`에 남기고 나머지는 삭제합니다.
- 사이트가 막혀 있으면 `python setup_case.py --source dir --dir <폴더>` (본인이 보지 않은 사진이어야 연습이 됩니다. 다른 사람에게 폴더를 채워 달라고 부탁하는 게 가장 좋아요).
- 새 사건: `python setup_case.py --force`

> ⚠ **`case/case.json`과 `case/.answer/`는 연습이 끝날 때까지 열지 마세요.** 정답 특징값이 그대로 들어 있습니다.

### 2. 서버 실행
```bash
python server.py                     # 대회와 동일: 60초 쿨다운, 힌트 레벨 1
python server.py --time-limit 60     # 첫 제출부터 60분 제한 (실전 감각)
python server.py --hint 0            # 점수만 (가장 어려움)
python server.py --hint 2            # "코를 더 짧게" 같은 방향까지 (입문용)
```

### 3. 몽타주 생성 → 제출
이미지는 원하는 AI 도구(ChatGPT, Gemini, Midjourney, Stable Diffusion 등)로 만듭니다.
```bash
python submit.py montage_01.png          # 제출
python submit.py montage_01.webp --fix   # 1024x1024 PNG로 변환해서 제출
python submit.py --history               # 최근 4건 + 최고 유사도
python submit.py --status                # 다음 제출까지 남은 시간
python submit.py --statement             # 진술 다시 보기
```
curl로 직접 호출해도 됩니다:
```bash
curl -F "file=@montage_01.png" http://127.0.0.1:8000/submit
```

### 4. 정답 공개 & 회고
```bash
python reveal.py     # 정답 vs 최고점 몽타주를 case/reveal.png 로 나란히 저장 + 전체 점수 추이 출력
```
모든 제출 이미지는 `case/submissions/`에, 기록은 `case/submissions.jsonl`에 남습니다.

## 연습할 때 볼 포인트 (출제 의도)
1. **진술 → 프롬프트 설계**: 모호한 묘사를 생성 모델이 이해하는 구체적 지시로 바꾸는 능력
2. **피드백 해석**: 부위별 점수가 무엇을 뜻하는지 가설을 세우고 한 번에 한두 변수만 바꾸기
3. **쿨다운 안에서의 전략**: 1분 1회라서 무작위 반복보다 실험 설계가 중요
4. **일관성 유지**: 좋아진 요소를 잃지 않도록 같은 인물을 유지한 채 부분 수정 (seed 고정, 이미지 편집/inpainting, 참조 이미지 활용 등)
5. **자동화 판단**: 생성 API가 있다면 생성 → 제출 → 피드백 반영을 스크립트로 묶을지 결정

## 한계
- 실제 대회의 정답 얼굴, 채점 모델, 가중치와는 다릅니다. 피드백 루프 연습용입니다.
- 진술은 자동 생성이라 실제 목격자 진술보다 단서가 정형화돼 있습니다.
- 나이·성별은 모델 추정치라 몇 살 오차가 있습니다.
