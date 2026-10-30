# AI_TOP_100

> AI_TOP_100 [2026] 대회용 카카오톡 봇<br/>
> 교동마을(농산물)·갯마을(수산물) 조합 업무를 돕는 AI 에이전트를 카카오톡에서 응답하도록 붙이는 프로젝트<br/>
> pep8 스타일 가이드에 따름 ([PEP8 Style Guide](https://peps.python.org/pep-0008/))

## 사용 목적

대회에서는 마을 관계자 인터뷰 → 데이터 수집·검증·정제 → 조합 업무를 개선하는 AI 에이전트 제출 순서로 진행된다.
이 저장소는 그중 **에이전트가 카카오톡으로 요청을 받고 응답을 돌려주는 통로**를 맡는다.

- 지금은 받은 메시지를 그대로 돌려주는 에코 봇까지 구현됨 (`bot-test.py`)
- 에이전트 로직은 `on_message` 자리에 붙일 예정. 제출 형태와 심사 방식은 아직 확정 안됨
- 대회 일정·출제 패턴·작업 지침은 [00.context_guidelines.md](./00.context_guidelines.md) 참고

## 사용된 것 <sub><small>(Built With)</small></sub>

> Python 3.14 기반이고, 봇 게이트웨이와는 WebSocket, 응답 전송은 HTTP로 통신한다.

- 런타임
  - Python 3.14
- 라이브러리
  - [websockets](https://websockets.readthedocs.io/en/stable/) — 봇 게이트웨이 WebSocket 연결 (HELLO → IDENTIFY/RESUME → heartbeat → 이벤트 수신)
  - [HTTPX](https://www.python-httpx.org/) — `/v1/bot/callback`으로 응답 전송
  - [python-dotenv](https://github.com/theskumar/python-dotenv) — 로컬 실행 시 `.env`의 `BOT_TOKEN` 로딩
- 테스트
  - [pytest](https://docs.pytest.org/en/stable/)
  - [pytest-watcher](https://github.com/olzhasar/pytest-watcher) — 파일이 바뀔 때마다 pytest 자동 재실행
- 패키지 의존성 관리
  - [poetry](https://python-poetry.org/docs/) (2.5.1)
- 실행 환경
  - Docker, Docker Compose ([Compose Watch](https://docs.docker.com/compose/how-tos/file-watch/)로 핫리로딩)

# 시작하기

로컬 개발과 배포 모두 Docker 환경을 기준으로 한다.
브레이크포인트 디버깅이 필요할 때만 로컬 poetry 환경에서 직접 실행한다.

## 사전 준비

- Docker, Docker Compose
  - Compose Watch의 `sync+restart`를 쓰기 때문에 Compose 2.23.0 이상 필요
- poetry: python 패키지 의존성 관리 도구 [python-poetry](https://python-poetry.org/)
  - 의존성을 추가하거나 로컬에서 디버깅할 때만 설치해도 무방
- 카카오 계정 + 카카오톡 연동, 봇 토큰 (`BOT_TOKEN`)

## 설치 / 설정 <sub><small>(Installation / Setup)</small></sub>

1. 프로젝트 루트에 `.env` 생성
   ```sh
   BOT_TOKEN=발급받은_봇_토큰
   ```
   - `.env`는 `.dockerignore`로 이미지에서 빠지고, 실행할 때 compose의 `env_file`로 주입됨
   - 토큰은 커밋하지 않음

## 파일 구성

| 파일 | 역할 |
|---|---|
| `bot-test.py` | 게이트웨이 연결, heartbeat, `MESSAGE_CREATE` 수신 → callback 응답 |
| `Dockerfile` | `requirements-stage` → `base-stage` → `develop-stage` / `production-stage` |
| `compose.yaml` | 로컬·배포 공통 설정 (build context, init, env_file) |
| `compose.override.yaml` | 로컬 개발용. `docker compose up` 하면 자동으로 같이 읽힘 |
| `compose.production.yaml` | 배포용. `-f`로 직접 지정해야 읽힘 |
| `00.context_guidelines.md` | 대회 정보와 작업 지침 |

Dockerfile 스테이지를 나눈 이유는 테스트 의존성 때문이다.
`develop-stage`와 `production-stage`가 둘 다 `base-stage`를 상속해서, pytest는 개발 이미지에만 들어가고 운영 이미지에는 들어가지 않는다.

## 로컬 개발 실행

1. 봇 + 테스트 감시 실행
   ```sh
   docker compose up --build --watch
   ```
   - `bot`: 코드를 저장하면 컨테이너로 복사한 뒤 봇을 재시작함 (`sync+restart`)
   - `test`: 코드를 복사만 하고, 컨테이너 안의 `ptw`가 변경을 감지해서 pytest를 다시 돌림 (`sync`)
   - `poetry.lock`이 바뀌면 이미지를 다시 빌드함 (`rebuild`)
1. 정상 동작 확인
   - `bot` 로그에 `READY — bot id=...`가 찍히면 게이트웨이 연결 성공
   - 카카오톡으로 메시지를 보내면 `echo: {메시지}`가 돌아옴
   - `test` 로그에 pytest 결과가 나옴. 테스트 파일이 없으면 exit code 5(no tests collected)인데 이건 정상
1. 로그를 동기화 이벤트와 분리해서 보고 싶을 때
   ```sh
   docker compose watch
   docker compose logs -f bot
   ```

> :warning: `test` 서비스에는 `.env`를 주입하지 않는다. 테스트가 실제 카카오 게이트웨이를 호출하지 못하게 막기 위해서다.

## 브레이크포인트 디버깅

watch 모드에서는 저장할 때마다 봇 프로세스가 재시작돼서 디버거 연결이 끊긴다.
그래서 브레이크포인트가 필요할 때는 봇만 로컬에서 직접 띄운다.

1. 컨테이너 봇 정지 (테스트 감시는 계속 돌려도 됨)
   ```sh
   docker compose stop bot
   ```
   > :warning: 같은 토큰으로 두 군데서 연결하면 먼저 붙은 쪽이 close 4004(replaced)로 끊기고, 코드상 재연결하지 않고 종료된다.
1. 로컬 가상환경에 의존성 설치 후 실행
   ```sh
   poetry install
   poetry run python bot-test.py
   ```
   - IDE에서는 `.venv`를 인터프리터로 잡고 `bot-test.py`를 디버그 실행
   - `.env`는 `load_dotenv()`가 읽음. 이미 설정된 환경변수는 덮어쓰지 않음
1. 디버깅이 끝나면 컨테이너 봇 다시 시작
   ```sh
   docker compose start bot
   ```

## 의존성 추가

```sh
poetry add {패키지}                  # 런타임 의존성 (main)
poetry add --group test {패키지}     # 테스트 전용 의존성
```

- 빌드할 때 `poetry export`로 `requirements.txt`를 만들고, 해시 검사 모드로 설치함 (`--without-hashes`를 쓰지 않는 이유)
- watch 실행 중이면 `poetry.lock` 변경을 감지해서 이미지를 자동으로 다시 빌드함

## 배포

1. 서버 아키텍처 확인
   ```sh
   uname -m      # x86_64 → linux/amd64, aarch64 → compose.production.yaml의 platform을 linux/arm64로 변경
   ```
1. 배포 이미지 빌드 (로컬)
   ```sh
   export BOT_IMAGE_TAG=0.1.0
   docker compose -f compose.yaml -f compose.production.yaml build
   ```
   - `BOT_IMAGE_TAG`가 없으면 compose가 바로 실패함. 태그 없이 배포되는 실수를 막기 위해서다
1. 이미지와 설정 파일 전송
   ```sh
   docker save kakao-bot:$BOT_IMAGE_TAG | gzip | ssh {user}@{host} docker load
   ssh {user}@{host} 'mkdir -p ~/kakao-bot'
   scp compose.yaml compose.production.yaml .env {user}@{host}:~/kakao-bot/
   ```
1. 서버에서 실행
   ```sh
   cd ~/kakao-bot && chmod 600 .env
   BOT_IMAGE_TAG=0.1.0 docker compose -f compose.yaml -f compose.production.yaml up -d --no-build
   docker compose -f compose.yaml -f compose.production.yaml logs -f bot
   ```

배포 설정에서 정해둔 것:

- `restart: on-failure:3` — 네트워크 끊김은 코드 안에서 재연결함. 프로세스가 죽는 건 인증 실패(4001)·중복 연결(4004) 같은 치명 오류라서 무한 재시작하지 않게 3회로 제한
- `init: true` — `docker stop`의 SIGTERM이 python 프로세스까지 전달되게 함
- 로그는 `json-file` 10MB × 3개로 순환. 메시지마다 INFO 로그가 쌓여서 디스크가 차는 걸 막기 위해서다

## 게이트웨이 close code 처리

| code | 의미 | 동작 |
|---|---|---|
| 1001 | 서버 재시작 | RESUME으로 세션 복구 |
| 4001 | 토큰 오류 | 종료 (`.env`의 `BOT_TOKEN` 확인) |
| 4003 | 잘못된 payload | 종료 (프로토콜 버그) |
| 4004 | 중복 연결로 대체됨 | 종료 (같은 토큰으로 다른 곳에서 실행 중인지 확인) |
| 4009 | 세션 만료 | 세션 초기화 후 새로 IDENTIFY |

> 게이트웨이 프로토콜은 공개 문서를 찾지 못해서 `bot-test.py`에 구현된 내용을 기준으로 정리함. 대회 안내 자료와 다르면 그쪽을 따름 (확인 필요)
