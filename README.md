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
| `docker-compose.yaml` | 로컬·배포 공통 설정 (build context, init, env_file) |
| `docker-compose.override.yaml` | 로컬 개발용. `docker compose up` 하면 자동으로 같이 읽힘 |
| `docker-compose.production.yaml` | 배포용. `-f`로 직접 지정해야 읽힘 |
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

레지스트리에 이미지를 올리지 않고, 서버에서 저장소를 받아 직접 빌드한다.
서버 아키텍처에 맞춰 네이티브로 빌드되기 때문에 이미지 태그나 `platform`을 따로 지정하지 않는다.

1. 최초 1회: 저장소 복제 + `.env` 생성
   ```sh
   git clone {저장소 주소} ~/symposium/ai_top_100_bot
   cd ~/symposium/ai_top_100_bot
   # .env는 git에 올리지 않으므로 서버에 직접 만들거나 scp로 복사
   chmod 600 .env
   ```
1. 지식 파일 전달: 대회 자료(`knowledge/`)는 git에 올리지 않고 scp로 보낸다. 컨테이너에 읽기 전용으로 마운트되며, 컨테이너 사용자(`bot`)가 읽을 수 있게 `644`로 둔다
   ```sh
   scp knowledge/0*.md {서버}:~/symposium/ai_top_100_bot/knowledge/
   ssh {서버} 'chmod 644 ~/symposium/ai_top_100_bot/knowledge/*.md'
   ```
   - 시스템 프롬프트에는 `KNOWLEDGE_GLOB`(기본 `0[0-9]_*.md`, 00~09)에 맞는 파일만 들어감. `_text/`(원문 텍스트), `_small/`(이미지), `10_`(인터뷰 진술 원문)은 크기와 우선순위 때문에 넣지 않음. 파일이 하나도 없으면 에러 로그(`지식 파일이 없습니다`)를 남기고 시스템 프롬프트만으로 응답하므로, 배포 후 로그에서 `knowledge=N files`가 0이 아닌지 확인한다
1. 배포 (코드 변경 시마다)
   ```sh
   cd ~/symposium/ai_top_100_bot
   git pull
   docker compose -f docker-compose.yaml -f docker-compose.production.yaml up -d --build
   docker compose -f docker-compose.yaml -f docker-compose.production.yaml logs -f bot
   ```
   - 로그에 `READY — bot id=...`가 찍히면 게이트웨이 연결 성공
   - 로컬에서 같은 토큰으로 봇을 띄워둔 상태면 close 4004로 충돌하니 로컬은 먼저 내림

> :warning: `docker system prune -af --volumes`는 배포 명령에 붙여 쓰지 않는다. 실행 중이 아닌 모든 컨테이너·이미지·볼륨을 지워서, 서버의 다른 서비스 데이터까지 날아갈 수 있고 빌드 캐시도 매번 사라진다. 디스크 정리가 필요하면 `docker image prune -f` 정도로 범위를 좁힌다. 특히 대화 기록 볼륨(`conversation-logs`)이 같이 지워지므로 `docker compose down -v`도 쓰지 않는다.

## 대화 기록

메시지 한 건마다 받은 메시지, 이벤트 원본(`callbackToken` 제외, `event_json`), 봇 답변, 처리 결과(`ok`/`truncated`/`fallback`/`refusal`/`blocked`/`error`), 응답 시간, Claude `request_id`·토큰 수, callback 응답 코드, 대화방 키(`conversation_key` = `botUserKey` 또는 `botGroupKey`)를 SQLite `/app/data/conversations.db`의 `conversations` 테이블에 한 행씩 저장한다.
WAL 모드라서 봇이 기록하는 중에도 읽을 수 있다. 배포 환경에서는 named volume `conversation-logs`에 저장돼서 재빌드·재시작해도 유지된다. 심사관 메시지가 담기므로 DB 파일은 `0600`으로 만들고 git·이미지에서 제외한다(`data/`). 외부 공유 금지.

```sh
# 최근 5건 확인 (읽기 전용 연결, 이미지에 sqlite3 CLI가 없어서 python 사용)
docker compose -f docker-compose.yaml -f docker-compose.production.yaml exec bot \
  python -c "import sqlite3; c=sqlite3.connect('file:/app/data/conversations.db?mode=ro', uri=True); print(c.execute('select id, received_at, outcome, latency_ms, content from conversations order by id desc limit 5').fetchall())"

# 심사 종료 후 회수: WAL 내용까지 포함한 스냅샷을 /tmp에 만든 뒤 복사 (볼륨은 건드리지 않음)
docker compose -f docker-compose.yaml -f docker-compose.production.yaml exec bot \
  python -c "import sqlite3; s=sqlite3.connect('/app/data/conversations.db'); d=sqlite3.connect('/tmp/judge.db'); s.backup(d); d.close(); s.close()"
docker compose -f docker-compose.yaml -f docker-compose.production.yaml cp bot:/tmp/judge.db ./judge.db
```

배포 설정에서 정해둔 것:

- `restart: unless-stopped` — 심사 기간(10/31 16:00 ~ 11/1 24:00)에는 봇을 손대지 않고 계속 살려둬야 한다. `on-failure`는 서버·Docker 재부팅 시 컨테이너를 다시 띄우지 않아서, 비정상 종료와 데몬 재시작 모두에서 재기동하는 `unless-stopped`로 둔다. 서버에서 `systemctl is-enabled docker`가 `enabled`인지도 확인한다
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
