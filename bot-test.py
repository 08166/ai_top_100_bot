import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import httpx
import websockets
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from websockets.exceptions import ConnectionClosed


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kakao-bot")

# ── Config ───────────────────────────────────────────────────────────
load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
BOT_TOKEN = os.getenv("BOT_TOKEN")

# 운영값은 .env로 바꿀 수 있게 두고, 기본값은 채팅 응답 속도 기준으로 정함
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "low")
CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "2048"))
CLAUDE_TIMEOUT_SEC = float(os.getenv("CLAUDE_TIMEOUT_SEC", "30"))
CLAUDE_MAX_RETRIES = int(os.getenv("CLAUDE_MAX_RETRIES", "1"))
MAX_CONCURRENT_REPLIES = int(os.getenv("MAX_CONCURRENT_REPLIES", "4"))
SYSTEM_PROMPT_PATH = BASE_DIR / os.getenv("SYSTEM_PROMPT_PATH", "prompts/system.txt")
FALLBACK_REPLY_PATH = BASE_DIR / os.getenv("FALLBACK_REPLY_PATH", "prompts/fallback.txt")
CONVERSATION_DB_PATH = BASE_DIR / os.getenv("CONVERSATION_DB_PATH", "data/conversations.db")
# 대회 자료는 git에 올리지 않으므로 프롬프트와 분리해 실행 시 읽음 (운영은 read-only 마운트)
KNOWLEDGE_DIR = BASE_DIR / os.getenv("KNOWLEDGE_DIR", "knowledge")
# 09(권한·조회 범위·증빙 발행 원문)를 빼면 본인 주문 확인·현금영수증·픽업 질문에 답하지 못하므로 포함.
# 10(인터뷰 진술 원문)은 문서와 충돌 시 문서가 우선이고 크기가 커서 제외
KNOWLEDGE_GLOB = os.getenv("KNOWLEDGE_GLOB", "0[0-9]_*.md")
# 멀티턴: 같은 사용자의 최근 대화만 이어 붙임. 오래된 대화까지 넣으면 입력이 커져 응답이 느려짐
HISTORY_MAX_TURNS = int(os.getenv("HISTORY_MAX_TURNS", "6"))
HISTORY_WINDOW_MIN = int(os.getenv("HISTORY_WINDOW_MIN", "30"))

KAKAO_GW_URL = "wss://bot-gateway.kakao.com/gateway"
KAKAO_API_URL = "https://kapi.kakao.com"
# 카카오 SimpleText 계약상 text는 최대 1000자
# 심사에서 500자를 넘는 말풍선은 채점에서 빠지므로 프롬프트는 480자, 전송은 500자로 막는다.
SIMPLE_TEXT_MAX_CHARS = 500

# ── Opcodes ──────────────────────────────────────────────────────────
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_READY = 3
OP_RESUME = 6
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# ── Close Code ───────────────────────────────────────────────────────
CLOSE_SERVER_RESTART = 1001   # Graceful shutdown → RESUME
CLOSE_AUTH_FAILED = 4001      # token 오류 → 중단
CLOSE_INVALID_PAYLOAD = 4003  # 프로토콜 버그 → 중단
CLOSE_REPLACED = 4004         # 중복 연결로 대체됨 → 중단
CLOSE_SESSION_TIMEOUT = 4009  # 세션 만료 → 새 IDENTIFY

RECONNECT_BACKOFF_SEC = 3.0

# 이벤트 루프는 Task를 약한 참조로만 들고 있어서, 끝나기 전에 GC되지 않게 여기서 참조를 유지
_inflight: set[asyncio.Task] = set()
# 대화방별 순서 보장용 Lock. 처리 중인 Task가 없으면 참조가 사라져 자동으로 정리됨
_conversation_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


# ── 대화 기록 ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AgentReply:
    """Claude 답변과 기록에 남길 처리 결과."""
    text: str
    outcome: str                 # ok | truncated | fallback | refusal | blocked | error
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # 캐시 적중 확인용. 적중하면 input_tokens에는 캐시 지점 뒤(대화 이력·질문)만 잡힘
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


class ConversationLog:
    """대화 한 건을 SQLite conversations 테이블에 한 행으로 저장한다."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS conversations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at     TEXT    NOT NULL,
            content         TEXT,
            event_json      TEXT    NOT NULL,
            reply           TEXT    NOT NULL,
            outcome         TEXT    NOT NULL,
            latency_ms      INTEGER,
            model           TEXT,
            request_id      TEXT,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            callback_status INTEGER
        )
    """
    # 예선 운영 DB는 SCHEMA 열만으로 만들어졌으므로 이후에 늘어난 열은 기존 행을 지우지 않고 ALTER로 붙임
    ADDED_COLUMNS = {
        "conversation_key": "TEXT",
        "cache_read_input_tokens": "INTEGER",
        "cache_creation_input_tokens": "INTEGER",
    }
    # 멀티턴 문맥으로 다시 쓸 수 있는 답만 고름. 오류 안내 문구는 문맥이 아님
    CONTEXT_OUTCOMES = ("ok", "truncated")

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 심사관 메시지가 담기므로 DB 파일을 소유자 전용(0600)으로 먼저 만들어 둠
        os.close(os.open(db_path, os.O_WRONLY | os.O_CREAT, 0o600))
        self.conn = sqlite3.connect(db_path)
        # 기록 중에도 회수 스크립트가 읽을 수 있게 WAL, 잠깐의 잠금 경합은 대기
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        with self.conn:
            self.conn.execute(self.SCHEMA)
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(conversations)")}
            for name, column_type in self.ADDED_COLUMNS.items():
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE conversations ADD COLUMN {name} {column_type}")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_key"
                " ON conversations (conversation_key, received_at)"
            )

    def record(self, entry: dict) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO conversations (
                       received_at, content, event_json, reply, outcome, latency_ms,
                       model, request_id, input_tokens, output_tokens, callback_status,
                       conversation_key, cache_read_input_tokens, cache_creation_input_tokens
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry["received_at"], entry["content"],
                    json.dumps(entry["event"], ensure_ascii=False, default=str),
                    entry["reply"], entry["outcome"], entry["latency_ms"],
                    entry["model"], entry["request_id"],
                    entry["input_tokens"], entry["output_tokens"], entry["callback_status"],
                    entry["conversation_key"],
                    entry["cache_read_input_tokens"], entry["cache_creation_input_tokens"],
                ),
            )

    def recent_turns(self, conversation_key: str, since: str, limit: int) -> list[tuple[str, str]]:
        """since(ISO UTC) 이후 같은 대화방의 (질문, 답) 쌍을 오래된 순으로 최대 limit개 돌려준다."""
        placeholders = ", ".join("?" for _ in self.CONTEXT_OUTCOMES)
        rows = self.conn.execute(
            f"""SELECT content, reply FROM conversations
                WHERE conversation_key = ? AND received_at >= ?
                  AND outcome IN ({placeholders}) AND content <> ''
                ORDER BY id DESC LIMIT ?""",
            (conversation_key, since, *self.CONTEXT_OUTCOMES, limit),
        ).fetchall()
        return list(reversed(rows))

    def close(self) -> None:
        self.conn.close()


# ── Claude 답변 생성 ─────────────────────────────────────────────────
class ClaudeAgent:
    """시스템 프롬프트를 들고 사용자 메시지 하나에 대한 Claude 답변을 만든다."""

    def __init__(self, client: AsyncAnthropic, system_prompt: str, fallback_reply: str,
                 max_concurrent: int) -> None:
        self.client = client
        self.system_prompt = system_prompt
        self.fallback_reply = fallback_reply
        # 동시에 몰려도 Claude 호출 수를 제한해서 rate limit과 비용 폭주를 막음
        self.limiter = asyncio.Semaphore(max_concurrent)

    async def reply(self, user_text: str,
                    history: list[tuple[str, str]] | None = None) -> AgentReply:
        # Messages API는 user/assistant가 번갈아 와야 하므로 (질문, 답) 쌍을 순서대로 펼침
        messages = [
            message
            for question, answer in history or []
            for message in ({"role": "user", "content": question},
                            {"role": "assistant", "content": answer})
        ]
        messages.append({"role": "user", "content": user_text})
        try:
            async with self.limiter:
                response = await self.client.beta.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=CLAUDE_MAX_TOKENS,
                    # 지식이 담긴 긴 시스템 프롬프트를 캐시해서 응답 지연(p95)을 줄임
                    system=[{"type": "text", "text": self.system_prompt,
                             "cache_control": {"type": "ephemeral"}}],
                    messages=messages,
                    output_config={"effort": CLAUDE_EFFORT},
                    # 안전 분류기가 거절하면 서버가 대체 모델로 다시 시도
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
        except anthropic.APIStatusError as e:
            log.error("claude status error — %d %s", e.status_code, e.message)
            return AgentReply(self.fallback_reply, "error")
        except anthropic.APIConnectionError as e:
            log.error("claude connection error — %s", e)
            return AgentReply(self.fallback_reply, "error")

        usage = (
            response._request_id, response.usage.input_tokens, response.usage.output_tokens,
            response.usage.cache_read_input_tokens, response.usage.cache_creation_input_tokens,
        )
        if response.stop_reason == "refusal":
            log.warning("claude refusal — request_id=%s", response._request_id)
            return AgentReply(self.fallback_reply, "refusal", *usage)

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            return AgentReply(self.fallback_reply, "fallback", *usage)
        # 프롬프트 원문이 새어 나가는 답은 보내지 않음 (OWASP LLM07 System Prompt Leakage)
        if any(marker in text for marker in PROMPT_LEAK_MARKERS):
            log.warning("prompt leak blocked — request_id=%s", response._request_id)
            return AgentReply(self.fallback_reply, "blocked", *usage)
        cleaned = sanitize_reply(text)
        if len(cleaned) > SIMPLE_TEXT_MAX_CHARS:
            log.warning("[SYSTEM] reply over %d chars (%d) — truncated, request_id=%s",
                        SIMPLE_TEXT_MAX_CHARS, len(cleaned), response._request_id)
            return AgentReply(cleaned[:SIMPLE_TEXT_MAX_CHARS], "truncated", *usage)
        return AgentReply(cleaned, "ok", *usage)


# SimpleText는 마크다운을 렌더링하지 않으므로 기호를 지워 글자 수도 줄임
MARKDOWN_PATTERN = re.compile(r"\*\*|__|`|^#{1,6}\s*|^\s*[-*]\s+", re.MULTILINE)
PROMPT_LEAK_MARKERS = ("[지식]", "[답변 원칙]", "=== 0")


def sanitize_reply(text: str) -> str:
    cleaned = MARKDOWN_PATTERN.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def build_system_prompt(instructions: str, knowledge_files: list[Path]) -> str:
    """응대 규칙 뒤에 지식 파일을 '=== 파일명 ===' 구획으로 이어 붙인다."""
    sections = [f"=== {path.stem} ===\n{path.read_text(encoding='utf-8')}" for path in knowledge_files]
    return "\n\n".join([instructions.rstrip(), *sections])


def conversation_key(d: dict) -> str | None:
    # 1:1 채널은 botUserKey, 그룹방은 botGroupKey로 사람(방)을 구분
    return d.get("botUserKey") or d.get("botGroupKey") or None


# ── 메시지 처리 ──────────────────────────────────────────────────────
async def on_message(d: dict, agent: ClaudeAgent, conversation_log: ConversationLog) -> None:
    """MESSAGE_CREATE 이벤트를 받아 Claude 답변을 callback으로 돌려주고 대화를 기록한다."""
    received_at = datetime.now(timezone.utc)
    started = time.monotonic()
    content = d.get("content", "")
    callback_token = d.get("callbackToken", "")
    log.info("MESSAGE_CREATE — content=%s", content[:100])

    if not callback_token:
        log.error("callbackToken 누락 — skip")
        return

    key = conversation_key(d)
    # 같은 고객이 연달아 보내면 앞 답이 기록된 뒤에 처리해야 다음 질문이 그 답을 문맥으로 봄
    lock = _conversation_locks.setdefault(key, asyncio.Lock()) if key else contextlib.nullcontext()
    async with lock:
        history: list[tuple[str, str]] = []
        if key:
            since = (received_at - timedelta(minutes=HISTORY_WINDOW_MIN)).isoformat()
            try:
                history = conversation_log.recent_turns(key, since, HISTORY_MAX_TURNS)
            except sqlite3.Error as e:
                # 문맥 조회가 실패해도 단발 답변은 보내야 하므로 빈 문맥으로 진행
                log.error("conversation history read failed — %s", e)

        try:
            reply = await agent.reply(content, history)
        except Exception:
            # 메시지마다 별도 Task라 예외가 새면 로그도 콜백도 없이 끝나 고객은 무응답을 겪음.
            # 예상 못 한 예외라도 안내 문구는 보내고 원인은 traceback으로 남김
            log.exception("reply generation failed")
            reply = AgentReply(agent.fallback_reply, "error")
        callback_status: int | None = None
        try:
            callback_status = await send_callback(callback_token, reply.text)
        except httpx.HTTPError as e:
            # 별도 Task에서 돌기 때문에 여기서 잡지 않으면 예외가 로그 없이 사라짐
            log.error("callback request failed — %s", e)

        try:
            conversation_log.record({
                "received_at": received_at.isoformat(),
                "content": content,
                # callbackToken은 1회용 자격증명이라 남기지 않음
                "event": {k: v for k, v in d.items() if k != "callbackToken"},
                "reply": reply.text,
                "outcome": reply.outcome,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "model": CLAUDE_MODEL,
                "request_id": reply.request_id,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "cache_read_input_tokens": reply.cache_read_input_tokens,
                "cache_creation_input_tokens": reply.cache_creation_input_tokens,
                "callback_status": callback_status,
                "conversation_key": key,
            })
        except sqlite3.Error as e:
            # 기록 실패가 답장 흐름을 막지 않게 로그만 남김
            log.error("conversation log write failed — %s", e)


# ── 봇 callback ──────────────────────────────────────────
async def send_callback(callback_token: str, text: str) -> int:
    """봇 callback 엔드포인트로 카카오 스킬 응답(simpleText)을 전송한다."""
    url = f"{KAKAO_API_URL}/v1/bot/callback"
    body = {
        "skillResponse": {
            "version": "2.0",
            "template": {"outputs": [
                    {"simpleText": {"text": text}},
                ]
            },
        },
    }
    headers = {
        "X-Bot-Callback-Token": callback_token,
        "Authorization": f"KakaoAK {BOT_TOKEN}"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=body, headers=headers)
    if resp.status_code != 200:
        log.error("callback failed — %d %s", resp.status_code, resp.text)
    return resp.status_code


# ── 세션 상태 ────────────────────────────────────────────────────────
class Session:
    """RESUME에 필요한 session_id와 마지막으로 받은 시퀀스(seq)를 보관한다."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.last_seq: int = 0

    def can_resume(self) -> bool:
        return self.session_id is not None

    def reset(self) -> None:
        self.session_id = None
        self.last_seq = 0


# ── WebSocket 게이트웨이 ─────────────────────────────────────────────
async def gateway_loop(session: Session, agent: ClaudeAgent,
                       conversation_log: ConversationLog) -> None:
    """게이트웨이에 한 번 연결해 핸드셰이크 후 heartbeat·수신 루프를 동시에 돈다."""
    log.info("connecting to %s", KAKAO_GW_URL)
    async with websockets.connect(KAKAO_GW_URL) as ws:
        # 1. HELLO 수신 — heartbeat_interval 확인
        hello = json.loads(await ws.recv())
        heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0

        if session.can_resume():
            # 재연결: RESUME으로 세션 복구
            log.info("RESUME — session=%s, seq=%d", session.session_id, session.last_seq)
            await ws.send(json.dumps({"op": OP_RESUME, "d": {
                "token": BOT_TOKEN,
                "session_id": session.session_id,
                "seq": session.last_seq,
            }}))
        else:
            # 최초 연결: IDENTIFY → READY
            await ws.send(json.dumps({"op": OP_IDENTIFY, "d": {
                "token": BOT_TOKEN,
            }}))
            ready = json.loads(await ws.recv())
            session.session_id = ready["d"]["session_id"]
            session.last_seq = 0
            log.info("READY — bot id=%s, session=%s",
                     ready["d"]["bot"]["id"], session.session_id)

        # 2. heartbeat + 이벤트 수신을 동시에 실행
        await asyncio.gather(
            _heartbeat(ws, heartbeat_interval),
            _receive(ws, session, agent, conversation_log),
        )


async def _heartbeat(ws, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await ws.send(json.dumps({"op": OP_HEARTBEAT}))


async def _receive(ws, session: Session, agent: ClaudeAgent,
                   conversation_log: ConversationLog) -> None:
    async for raw in ws:
        payload = json.loads(raw)
        if payload.get("s") is not None:
            session.last_seq = payload["s"]
        if payload.get("op") == OP_DISPATCH and payload.get("t") == "MESSAGE_CREATE":
            # Claude 응답을 기다리는 동안에도 다음 이벤트를 계속 받도록 메시지마다 Task로 분리
            task = asyncio.create_task(on_message(payload["d"], agent, conversation_log))
            _inflight.add(task)
            task.add_done_callback(_inflight.discard)


def _should_resume(code: int | None) -> bool:
    if code in (CLOSE_AUTH_FAILED, CLOSE_INVALID_PAYLOAD, CLOSE_REPLACED):
        log.error("close %s — 재연결 중단 (프로토콜/인증 오류)", code)
        raise SystemExit(1)
    if code == CLOSE_SESSION_TIMEOUT:
        return False
    return True


async def run(session: Session, agent: ClaudeAgent, conversation_log: ConversationLog) -> None:
    while True:
        try:
            await gateway_loop(session, agent, conversation_log)
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
            reason = e.rcvd.reason if e.rcvd else ""
            log.warning("connection closed — code=%s reason=%s", code, reason)
            if not _should_resume(code):
                session.reset()
        except OSError as e:
            log.warning("connect failed — %s", e)

        log.info("reconnecting in %.1fs …", RECONNECT_BACKOFF_SEC)
        await asyncio.sleep(RECONNECT_BACKOFF_SEC)


async def main():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN 환경변수를 설정하세요")
        sys.exit(1)
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY 환경변수를 설정하세요")
        sys.exit(1)

    knowledge_files = sorted(KNOWLEDGE_DIR.glob(KNOWLEDGE_GLOB))
    if not knowledge_files:
        # 종료하면 restart 정책 때문에 재시작만 반복하며 무응답이 되므로, 경고를 남기고
        # 시스템 프롬프트만으로 계속 응답한다 (지식 파일은 scp로 별도 전달)
        log.error("지식 파일이 없습니다 — %s/%s, 시스템 프롬프트만으로 응답", KNOWLEDGE_DIR, KNOWLEDGE_GLOB)
    system_prompt = build_system_prompt(SYSTEM_PROMPT_PATH.read_text(encoding="utf-8"), knowledge_files)
    fallback_reply = FALLBACK_REPLY_PATH.read_text(encoding="utf-8").strip()

    # Claude 클라이언트는 앱 수명 동안 하나만 두고, 종료할 때 연결을 닫음
    async with AsyncAnthropic(timeout=CLAUDE_TIMEOUT_SEC, max_retries=CLAUDE_MAX_RETRIES) as client:
        agent = ClaudeAgent(client, system_prompt, fallback_reply, MAX_CONCURRENT_REPLIES)
        conversation_log = ConversationLog(CONVERSATION_DB_PATH)
        log.info("bot starting — gateway=%s, model=%s, effort=%s, db=%s, knowledge=%d files",
                 KAKAO_GW_URL, CLAUDE_MODEL, CLAUDE_EFFORT, CONVERSATION_DB_PATH, len(knowledge_files))
        try:
            await run(Session(), agent, conversation_log)
        finally:
            conversation_log.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[bot] stopped")
