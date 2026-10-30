import asyncio
import json
import logging
import os
import sys
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

KAKAO_GW_URL = "wss://bot-gateway.kakao.com/gateway"
KAKAO_API_URL = "https://kapi.kakao.com"
# 카카오 SimpleText 계약상 text는 최대 1000자
SIMPLE_TEXT_MAX_CHARS = 1000

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

    async def reply(self, user_text: str) -> str:
        try:
            async with self.limiter:
                response = await self.client.beta.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=CLAUDE_MAX_TOKENS,
                    system=self.system_prompt,
                    messages=[{"role": "user", "content": user_text}],
                    output_config={"effort": CLAUDE_EFFORT},
                    # 안전 분류기가 거절하면 서버가 대체 모델로 다시 시도
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
        except anthropic.APIStatusError as e:
            log.error("claude status error — %d %s", e.status_code, e.message)
            return self.fallback_reply
        except anthropic.APIConnectionError as e:
            log.error("claude connection error — %s", e)
            return self.fallback_reply

        if response.stop_reason == "refusal":
            log.warning("claude refusal — request_id=%s", response._request_id)
            return self.fallback_reply

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text[:SIMPLE_TEXT_MAX_CHARS] or self.fallback_reply


# ── 메시지 처리 ──────────────────────────────────────────────────────
async def on_message(d: dict, agent: ClaudeAgent) -> None:
    """MESSAGE_CREATE 이벤트를 받아 Claude 답변을 callback으로 돌려준다."""
    content = d.get("content", "")
    callback_token = d.get("callbackToken", "")
    log.info("MESSAGE_CREATE — content=%s", content[:100])

    if not callback_token:
        log.error("callbackToken 누락 — skip")
        return

    answer = await agent.reply(content)
    try:
        await send_callback(callback_token, answer)
    except httpx.HTTPError as e:
        # 별도 Task에서 돌기 때문에 여기서 잡지 않으면 예외가 로그 없이 사라짐
        log.error("callback request failed — %s", e)


# ── 봇 callback ──────────────────────────────────────────
async def send_callback(callback_token: str, text: str) -> None:
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
async def gateway_loop(session: Session, agent: ClaudeAgent) -> None:
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
            _receive(ws, session, agent),
        )


async def _heartbeat(ws, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await ws.send(json.dumps({"op": OP_HEARTBEAT}))


async def _receive(ws, session: Session, agent: ClaudeAgent) -> None:
    async for raw in ws:
        payload = json.loads(raw)
        if payload.get("s") is not None:
            session.last_seq = payload["s"]
        if payload.get("op") == OP_DISPATCH and payload.get("t") == "MESSAGE_CREATE":
            # Claude 응답을 기다리는 동안에도 다음 이벤트를 계속 받도록 메시지마다 Task로 분리
            task = asyncio.create_task(on_message(payload["d"], agent))
            _inflight.add(task)
            task.add_done_callback(_inflight.discard)


def _should_resume(code: int | None) -> bool:
    if code in (CLOSE_AUTH_FAILED, CLOSE_INVALID_PAYLOAD, CLOSE_REPLACED):
        log.error("close %s — 재연결 중단 (프로토콜/인증 오류)", code)
        raise SystemExit(1)
    if code == CLOSE_SESSION_TIMEOUT:
        return False
    return True


async def run(session: Session, agent: ClaudeAgent) -> None:
    while True:
        try:
            await gateway_loop(session, agent)
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

    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    fallback_reply = FALLBACK_REPLY_PATH.read_text(encoding="utf-8").strip()

    # Claude 클라이언트는 앱 수명 동안 하나만 두고, 종료할 때 연결을 닫음
    async with AsyncAnthropic(timeout=CLAUDE_TIMEOUT_SEC, max_retries=CLAUDE_MAX_RETRIES) as client:
        agent = ClaudeAgent(client, system_prompt, fallback_reply, MAX_CONCURRENT_REPLIES)
        log.info("bot starting — gateway=%s, model=%s, effort=%s",
                 KAKAO_GW_URL, CLAUDE_MODEL, CLAUDE_EFFORT)
        await run(Session(), agent)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[bot] stopped")
