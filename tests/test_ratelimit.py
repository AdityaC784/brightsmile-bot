from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from tests.test_graph import FakeAnswerer, ScriptedUtility

from bot.api.main import create_app
from bot.api.ratelimit import RateLimiter, client_ip
from bot.config import load_yaml
from bot.graph.builder import build_graph
from bot.graph.nodes import Nodes


def test_per_chat_minute_limit_then_window_passes():
    rl = RateLimiter(per_chat_per_minute=2, per_chat_per_day=100, per_ip_per_day=100)
    assert rl.check("t1", "ip", now=0) is None
    assert rl.check("t1", "ip", now=1) is None
    assert rl.check("t1", "ip", now=2) == "chat_minute"
    assert rl.check("t2", "ip", now=2) is None  # another chat is unaffected
    assert rl.check("t1", "ip", now=61) is None  # a minute later


def test_per_chat_day_and_per_ip_day_limits():
    rl = RateLimiter(per_chat_per_minute=100, per_chat_per_day=3, per_ip_per_day=4)
    for i in range(3):
        assert rl.check("t1", "ip-a", now=i * 120) is None
    assert rl.check("t1", "ip-a", now=500) == "chat_day"
    assert rl.check("t2", "ip-a", now=600) is None  # 4th message from this visitor
    assert rl.check("t3", "ip-a", now=700) == "ip_day"  # new chats don't get around it
    assert rl.check("t3", "ip-b", now=700) is None
    assert rl.check("t1", "ip-a", now=86_400 + 1) is None  # a day after the first message


def test_blocked_messages_are_not_counted():
    rl = RateLimiter(per_chat_per_minute=1, per_chat_per_day=100, per_ip_per_day=100)
    assert rl.check("t1", "ip", now=0) is None
    for s in range(1, 30):
        assert rl.check("t1", "ip", now=s) == "chat_minute"
    assert rl.check("t1", "ip", now=60.5) is None


def test_client_ip_prefers_forwarded_for():
    assert client_ip({"x-forwarded-for": "203.0.113.7, 10.0.0.1"}, "10.0.0.1") == "203.0.113.7"
    assert client_ip({}, "127.0.0.1") == "127.0.0.1"
    assert client_ip({}, None) == "unknown"


def _app(*utility_replies):
    graph = build_graph(
        Nodes(utility=ScriptedUtility(*utility_replies), answerer=FakeAnswerer()),
        checkpointer=InMemorySaver(),
    )
    return create_app(graph, persist_turns=False)


def test_over_the_limit_gets_the_limit_message_without_calling_the_llm():
    app = _app()  # no scripted replies: any LLM call would raise
    with TestClient(app) as c:
        app.state.limiter = RateLimiter(per_chat_per_minute=0, per_chat_per_day=9, per_ip_per_day=9)
        r = c.post("/v1/chat?stream=false", json={"thread_id": "rl-1", "message": "Hi"})
    body = r.json()
    assert r.status_code == 200 and body["behavior"] == "abstain"
    assert body["answer"] == load_yaml("generation")["rate_limit_answer"].strip()
    assert "112" in body["answer"]  # the emergency route is always included


def test_llm_failure_gets_the_error_message_with_the_emergency_route():
    app = _app()  # the classifier call fails (nothing scripted)
    with TestClient(app) as c:
        r = c.post("/v1/chat?stream=false", json={"thread_id": "err-1", "message": "Hello?"})
    answer = r.json()["answer"]
    assert answer == load_yaml("generation")["error_answer"].strip()
    assert "technical problem" in answer and "112" in answer
    assert answer != load_yaml("generation")["fallback_answer"].strip()
