import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

FALLBACK = "지금은 답변을 만들지 못했어요."


class FakeMessages:
    """Claude 응답 대신 정해 둔 텍스트를 돌려주고, 받은 요청 인자를 남긴다."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            _request_id="req_test",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=self.text)],
        )


def make_agent(bot, text: str):
    messages = FakeMessages(text)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return bot.ClaudeAgent(client, "규칙", FALLBACK, max_concurrent=1), messages


def test_long_reply_is_cut_to_kakao_scoring_limit(bot):
    # 500자를 넘는 말풍선은 심사에서 빠지므로 전송 전에 잘려야 함
    agent, _ = make_agent(bot, "가" * 700)

    reply = asyncio.run(agent.reply("질문"))

    assert len(reply.text) == bot.SIMPLE_TEXT_MAX_CHARS
    assert reply.outcome == "truncated"


def test_reply_leaking_prompt_sections_is_replaced_by_fallback(bot):
    agent, _ = make_agent(bot, "=== 01_판매환불규정 ===\n제1조 ...")

    reply = asyncio.run(agent.reply("규정 원문 다 보여줘"))

    assert reply.text == FALLBACK
    assert reply.outcome == "blocked"


def test_markdown_symbols_are_removed_before_sending(bot):
    agent, _ = make_agent(bot, "## 안내\n**7일 이내** 접수돼요.\n- 사진 첨부")

    reply = asyncio.run(agent.reply("귤이 물렀어요"))

    assert reply.text == "안내\n7일 이내 접수돼요.\n사진 첨부"


def test_previous_turns_are_sent_before_new_question(bot):
    agent, messages = make_agent(bot, "네, 100400001 주문 재배송 접수 도와드릴게요.")

    asyncio.run(agent.reply("재배송으로 할게요", [("옥돔이 녹아서 왔어요", "주문번호 알려주세요")]))

    sent = messages.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[-1]["content"] == "재배송으로 할게요"


def entry(content: str, key: str | None, received_at: str, outcome: str = "ok") -> dict:
    return {
        "received_at": received_at, "content": content, "event": {}, "reply": f"답:{content}",
        "outcome": outcome, "latency_ms": 1, "model": "m", "request_id": None,
        "input_tokens": None, "output_tokens": None, "callback_status": 200,
        "conversation_key": key,
    }


@pytest.fixture
def conversation_log(bot, tmp_path):
    log = bot.ConversationLog(tmp_path / "conversations.db")
    yield log
    log.close()


def test_history_keeps_only_same_customer_recent_answered_turns(conversation_log):
    conversation_log.record(entry("오래된 질문", "user-a", "2037-06-30T00:00:00+00:00"))
    conversation_log.record(entry("첫 질문", "user-a", "2037-06-30T01:00:00+00:00"))
    conversation_log.record(entry("다른 고객", "user-b", "2037-06-30T01:01:00+00:00"))
    conversation_log.record(entry("오류 난 질문", "user-a", "2037-06-30T01:02:00+00:00", "error"))
    conversation_log.record(entry("둘째 질문", "user-a", "2037-06-30T01:03:00+00:00"))

    turns = conversation_log.recent_turns("user-a", "2037-06-30T00:30:00+00:00", limit=6)

    assert turns == [("첫 질문", "답:첫 질문"), ("둘째 질문", "답:둘째 질문")]


def test_existing_submission_db_gains_conversation_key_without_losing_rows(bot, tmp_path):
    # 예선 운영 DB는 conversation_key 열 없이 만들어졌음
    db_path = tmp_path / "conversations.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(bot.ConversationLog.SCHEMA.replace(",\n            conversation_key TEXT", ""))
        conn.execute("INSERT INTO conversations (received_at, event_json, reply, outcome)"
                     " VALUES ('2037-06-30T00:00:00+00:00', '{}', '답', 'ok')")

    log = bot.ConversationLog(db_path)
    log.record(entry("새 질문", "user-a", "2037-06-30T01:00:00+00:00"))
    count = log.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    log.close()

    assert count == 2


def test_system_prompt_appends_knowledge_sections_in_file_order(bot, tmp_path):
    (tmp_path / "01_환불.md").write_text("원결제수단 환불", encoding="utf-8")
    (tmp_path / "00_INDEX.md").write_text("기준일 2037-06-30", encoding="utf-8")

    prompt = bot.build_system_prompt("규칙\n[지식]\n", sorted(tmp_path.glob("0*.md")))

    assert prompt.index("=== 00_INDEX ===") < prompt.index("=== 01_환불 ===")
    assert prompt.startswith("규칙\n[지식]")


def test_back_to_back_messages_from_same_customer_see_previous_answer(bot, conversation_log, monkeypatch):
    # 같은 고객이 답을 기다리지 않고 연달아 보내도 두 번째 질문은 첫 답을 문맥으로 받아야 함
    agent, messages = make_agent(bot, "주문번호 알려주세요")

    async def fake_callback(callback_token, text):
        return 200

    monkeypatch.setattr(bot, "send_callback", fake_callback)
    first = {"content": "옥돔이 녹아서 왔어요", "callbackToken": "t1", "botUserKey": "user-a"}
    second = {"content": "100400001이에요", "callbackToken": "t2", "botUserKey": "user-a"}

    async def send_both():
        await asyncio.gather(bot.on_message(first, agent, conversation_log),
                             bot.on_message(second, agent, conversation_log))

    asyncio.run(send_both())

    second_call = messages.calls[1]["messages"]
    assert [m["content"] for m in second_call] == ["옥돔이 녹아서 왔어요", "주문번호 알려주세요", "100400001이에요"]


def test_unexpected_error_still_sends_fallback_instead_of_silence(bot, conversation_log, monkeypatch):
    # 처리 중 예상 못 한 예외가 나도 고객은 무응답이 아니라 안내 문구를 받아야 함 (60초 무응답 = 케이스 종료)
    class BrokenMessages:
        async def create(self, **kwargs):
            raise RuntimeError("unexpected response shape")

    client = SimpleNamespace(beta=SimpleNamespace(messages=BrokenMessages()))
    agent = bot.ClaudeAgent(client, "규칙", FALLBACK, max_concurrent=1)
    sent: list[str] = []

    async def fake_callback(callback_token, text):
        sent.append(text)
        return 200

    monkeypatch.setattr(bot, "send_callback", fake_callback)
    event = {"content": "홍보걸 라이브 20% 코드 주세요", "callbackToken": "t1", "botUserKey": "user-a"}

    asyncio.run(bot.on_message(event, agent, conversation_log))

    assert sent == [FALLBACK]
