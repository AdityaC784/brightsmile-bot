"""LangGraph flow tests with scripted LLMs and an in-memory checkpointer (no network, no DB)."""

import json
from dataclasses import dataclass, field

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from bot.graph.builder import build_graph, new_turn
from bot.graph.nodes import Nodes
from bot.llm.base import ChatResponse


class ScriptedUtility:
    """Returns scripted JSON per call, in order; records the prompts it saw."""

    def __init__(self, *replies: dict):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def chat_json(self, system, messages, schema, **_):
        self.prompts.append(messages[-1].content)
        reply = self.replies.pop(0)
        return schema.model_validate(reply), ChatResponse(json.dumps(reply), "fake", "fake")


@dataclass
class FakeResult:
    answer: str
    behavior: str = "answer"
    citations: list = field(default_factory=lambda: ["DENT-019#x"])
    confidence: float | None = 1.0
    fallback_reason: str | None = None
    repaired: bool = False


class FakeAnswerer:
    def __init__(self):
        self.calls: list[dict] = []

    def answer(self, question, history, *, query=None):
        self.calls.append({"question": question, "history": history, "query": query})
        return FakeResult(answer=f"grounded answer to: {query}")


def _graph(*utility_replies):
    util, ans = ScriptedUtility(*utility_replies), FakeAnswerer()
    graph = build_graph(Nodes(utility=util, answerer=ans), checkpointer=InMemorySaver())
    return graph, util, ans


def _ask(graph, thread, text):
    return graph.invoke(new_turn(thread, text), {"configurable": {"thread_id": thread}})


def test_support_first_turn_skips_rewrite():
    graph, util, ans = _graph({"intent": "support"})
    out = _ask(graph, "t1", "How much is scaling?")
    assert out["intent"] == "support"
    assert ans.calls[0]["query"] == "How much is scaling?"  # no rewrite call on first turn
    assert len(util.prompts) == 1
    assert out["reply"]["answer"] == "grounded answer to: How much is scaling?"
    assert [m["role"] for m in out["messages"]] == ["user", "assistant"]


def test_follow_up_is_rewritten_with_history_and_transcript_accumulates():
    graph, util, ans = _graph(
        {"intent": "support"},
        {"intent": "support"},
        {"standalone": "Is there a cheaper take-home teeth whitening option?"},
    )
    _ask(graph, "t2", "How much does teeth whitening cost?")
    out = _ask(graph, "t2", "Is there a cheaper take-home option?")
    assert ans.calls[1]["query"] == "Is there a cheaper take-home teeth whitening option?"
    assert ans.calls[1]["question"] == "Is there a cheaper take-home option?"
    assert "teeth whitening cost" in util.prompts[-1]  # rewriter saw the earlier turn
    assert len(out["messages"]) == 4
    assert [m.role for m in ans.calls[1]["history"]] == ["user", "assistant"]


def test_threads_are_isolated():
    graph, _, ans = _graph({"intent": "support"}, {"intent": "support"})
    _ask(graph, "a", "Hours on Saturday?")
    out = _ask(graph, "b", "Is parking free?")
    assert len(out["messages"]) == 2  # thread b doesn't see thread a
    assert ans.calls[1]["history"] == []


@pytest.mark.parametrize(
    ("intent", "behavior", "snippet"),
    [
        ("out_of_scope", "refuse", "can only help"),
        ("greeting", "answer", "Hello"),
        ("handoff", "handoff", "+91-20-5550-0140"),
    ],
)
def test_fixed_reply_routes(intent, behavior, snippet):
    graph, _, ans = _graph({"intent": intent})
    out = _ask(graph, f"t-{intent}", "some message")
    assert out["reply"]["behavior"] == behavior and snippet in out["reply"]["answer"]
    assert ans.calls == []  # no retrieval / generation for fixed replies


def test_intent_label_is_normalised():
    """Classifier output is validated; casing/whitespace in the label is normalised."""
    graph, _, ans = _graph({"intent": "Support "})  # normalised by the validator
    out = _ask(graph, "t3", "price of a crown?")
    assert out["intent"] == "support" and ans.calls


def _db_ok() -> bool:
    import psycopg

    from bot.config import get_settings

    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_ok(), reason="needs `docker compose up db`")
def test_postgres_checkpointer_persists_threads_across_graph_instances():
    import uuid

    from bot.graph.builder import open_checkpointer

    thread = f"test-{uuid.uuid4()}"
    with open_checkpointer() as cp:
        g1 = build_graph(
            Nodes(utility=ScriptedUtility({"intent": "support"}), answerer=FakeAnswerer()), cp
        )
        _ask(g1, thread, "Hours on Saturday?")
    with open_checkpointer() as cp:  # fresh connection + graph, like a server restart
        util = ScriptedUtility(
            {"intent": "support"}, {"standalone": "Is parking free on Saturday?"}
        )
        g2 = build_graph(Nodes(utility=util, answerer=FakeAnswerer()), cp)
        out = _ask(g2, thread, "Is parking free then?")
    assert [m["content"] for m in out["messages"]][:1] == ["Hours on Saturday?"]
    assert len(out["messages"]) == 4


# --------------------------------------------------------------------------- lead capture


@pytest.fixture
def saved(monkeypatch):
    """Record save_lead calls instead of writing to Postgres."""
    calls: list[dict] = []

    def fake_save(thread_id, **kw):
        calls.append({"thread_id": thread_id, **kw})
        return "lead-1"

    monkeypatch.setattr("bot.graph.nodes.save_lead", fake_save)
    return calls


def _ex(**kw):
    base = {
        "need": None,
        "timeline": None,
        "budget": None,
        "name": None,
        "phone": None,
        "email": None,
        "consent": "unclear",
        "question": None,
    }
    return {**base, **kw}


def test_lead_flow_asks_consent_then_saves_once(saved):
    graph, _, _ = _graph(
        {"intent": "lead"},
        _ex(need="teeth whitening"),
        {"intent": "support"},
        _ex(name="Priya", phone="9876543210", consent="yes"),
    )
    out1 = _ask(graph, "L1", "I want to get my teeth whitened")
    assert out1["lead_stage"] == "awaiting_consent"
    assert "never share or sell" in out1["reply"]["answer"] and saved == []
    out2 = _ask(graph, "L1", "Yes, I'm Priya, 9876543210")
    assert out2["lead_stage"] == "saved" and len(saved) == 1
    assert saved[0]["consented"] is True and saved[0]["thread_id"] == "L1"
    assert saved[0]["need"] == "teeth whitening" and "Priya" in out2["reply"]["answer"]
    assert saved[0]["contact"] == "9876543210"  # email is optional


def test_lead_with_phone_and_email_keeps_both(saved):
    graph, _, _ = _graph(
        {"intent": "lead"},
        _ex(need="braces"),
        {"intent": "support"},
        _ex(name="Ravi", phone="98765 43210", email="ravi@example.com", consent="yes"),
    )
    _ask(graph, "L5", "I need braces")
    out = _ask(graph, "L5", "Yes. Ravi, ravi@example.com, 98765 43210")
    assert out["lead_stage"] == "saved"
    assert saved[0]["contact"] == "98765 43210, ravi@example.com"


def test_lead_with_only_email_asks_for_phone(saved):
    graph, _, _ = _graph(
        {"intent": "lead"},
        _ex(need="crown"),
        {"intent": "support"},
        _ex(name="Asha", email="asha@example.com", consent="yes"),
    )
    _ask(graph, "L6", "I need a crown")
    out = _ask(graph, "L6", "Yes, Asha, asha@example.com")
    assert saved == [] and out["lead_stage"] == "awaiting_consent"
    assert "phone number" in out["reply"]["answer"]


def test_details_volunteered_before_consent_text_are_not_kept(saved):
    graph, _, _ = _graph(
        {"intent": "lead"},
        _ex(
            need="braces", name="Ravi", phone="9876543210", email="ravi@example.com", consent="yes"
        ),
    )
    out = _ask(graph, "L2", "Book me for braces, I'm Ravi ravi@example.com 9876543210, call me")
    assert saved == [] and out["lead_stage"] == "awaiting_consent"
    assert not {"name", "phone", "email"} & set(out["lead"])


def test_decline_keeps_no_personal_data(saved):
    graph, _, _ = _graph(
        {"intent": "lead"},
        _ex(need="crown"),
        {"intent": "support"},
        _ex(consent="no", name="Asha"),
    )
    _ask(graph, "L3", "I need a crown")
    out = _ask(graph, "L3", "No thanks, I don't want to share details")
    assert out["lead_stage"] == "declined" and saved == []
    assert "name" not in out["lead"] and "+91-20-5550-0140" in out["reply"]["answer"]


def test_question_during_lead_flow_is_answered_first(saved):
    graph, _, ans = _graph(
        {"intent": "lead"},
        _ex(need="whitening", question="How much is in-office whitening?"),
    )
    out = _ask(graph, "L4", "I'd like whitening, how much is in-office whitening?")
    assert ans.calls and ans.calls[0]["query"] == "How much is in-office whitening?"
    answer = out["reply"]["answer"]
    assert answer.startswith("grounded answer to:") and "contact you" in answer


def test_save_lead_refuses_without_consent():
    from bot.leads.store import ConsentRequired, save_lead

    with pytest.raises(ConsentRequired):
        save_lead("t", name="A", contact="1", consent_text="x", consented=False)


# --------------------------------------------------------------------------- human handoff


def _staff_graph(*utility_replies):
    """Graph whose first turn starts a staff conversation."""
    graph, _, ans = _graph({"intent": "handoff"}, *utility_replies)
    out = _ask(graph, "S1", "I want to speak to the manager")
    assert out["handoff_pending"] is True and out["reply"]["handoff_started"] is True
    assert out["staff_waiting_since"] and ans.calls == []
    return graph, ans


def test_staff_hold_the_chat_and_the_bot_stays_silent():
    graph, ans = _staff_graph({"intent": "support"}, {"intent": "handoff"})
    out = _ask(graph, "S1", "Yes, 5 pm works for me")
    assert out["reply"] == {"behavior": "staff", "answer": "", "citations": []}
    assert out["handoff_pending"] is True and ans.calls == []
    again = _ask(graph, "S1", "Can I talk to the manager instead?")  # already with staff
    assert again["reply"]["behavior"] == "staff" and "handoff_started" not in again["reply"]


def test_red_flag_while_staff_hold_the_chat_is_answered_at_once():
    graph, ans = _staff_graph(
        {"intent": "support", "emergency": True}, {"standalone": "spreading neck swelling"}
    )
    out = _ask(graph, "S1", "the swelling is spreading down my neck")
    assert len(ans.calls) == 1 and out["reply"]["behavior"] == "answer"
    assert out["handoff_pending"] is True  # staff still hold the conversation


def test_request_for_a_person_with_a_red_flag_gets_the_answer_then_the_handoff():
    graph, _, ans = _graph({"intent": "handoff", "emergency": "true"})
    out = _ask(graph, "S2", "get me a doctor, my cheek is swelling up fast")
    assert len(ans.calls) == 1
    assert out["reply"]["answer"].startswith("grounded answer")
    assert "+91-20-5550-0140" in out["reply"]["answer"] and out["reply"]["citations"]
    assert out["handoff_pending"] is True and out["reply"]["handoff_started"] is True


def test_handoff_with_nobody_online_does_not_start_a_staff_chat():
    graph, _, _ = _graph({"intent": "handoff"})
    cfg = {"configurable": {"thread_id": "S3"}}
    out = graph.invoke(new_turn("S3", "a person please", staff_online=False), cfg)
    assert "Nobody from our team" in out["reply"]["answer"]
    assert not out.get("handoff_pending") and out["reply"]["handoff_started"] is False


def test_presence_states():
    from bot.api.staff import Presence

    p = Presence()
    assert not p.staff_online(now=100)
    p.staff_ping(now=100)
    assert p.staff_online(now=170) and not p.staff_online(now=191)  # background-tab allowance
    p.patient_ping("t", now=100)
    assert p.patient_status("t", now=105) == ("online", 5)
    assert p.patient_status("t", now=121)[0] == "left"  # visible tab stopped polling
    p.patient_ping("t", hidden=True, now=130)
    assert p.patient_status("t", now=200)[0] == "away"  # hidden tabs poll slowly
    assert p.patient_status("t", now=221)[0] == "left"
    assert p.patient_status("unknown", now=50) == ("online", 0)  # unknown = seen now
    p.patient_ping("dup", now=300)  # visible tab ...
    p.patient_ping("dup", hidden=True, now=303)  # ... and its duplicate in the background
    assert p.patient_status("dup", now=304)[0] == "online"  # the most active tab wins
    assert p.patient_status("dup", now=330)[0] == "away"


def test_staff_presence_sessions_and_leaving():
    from bot.api.staff import Presence

    p = Presence()
    assert not p.staff_online(now=100) and not p.staff_gone(now=100)  # never seen: not "gone"
    p.staff_ping("a", now=100)
    p.staff_ping("b", now=100)
    p.staff_leave("a", now=101)
    assert p.staff_online(now=120)  # tab b is still open
    p.staff_leave("b", now=101)
    assert p.staff_online(now=110) and not p.staff_online(now=112)  # 10 s grace
    assert p.staff_gone(now=112)
    p.staff_ping("b", now=105)  # a reload within the grace period keeps staff online
    assert p.staff_online(now=112)


def test_staff_reply_end_and_timeout():
    from bot.api import staff

    graph, _ = _staff_graph({"intent": "support"})
    staff.reply(graph, "S1", "Hi, Vikram here.", persist=False)
    values = graph.get_state({"configurable": {"thread_id": "S1"}}).values
    assert values["messages"][-1] == {"role": "human_agent", "content": "Hi, Vikram here."}
    assert values["staff_waiting_since"] is None
    assert staff.expire(graph, "S1", persist=False) is False  # nobody is waiting

    _ask(graph, "S1", "Thanks, one more question")  # patient waits again
    since = graph.get_state({"configurable": {"thread_id": "S1"}}).values["staff_waiting_since"]
    assert staff.expire(graph, "S1", now=since + 60, persist=False) is False
    assert staff.expire(graph, "S1", now=since + 241, persist=False) is True
    values = graph.get_state({"configurable": {"thread_id": "S1"}}).values
    assert (
        values["handoff_pending"] is False and "call reception" in values["messages"][-1]["content"]
    )
    assert not staff.in_staff_mode(graph, "S1")


def test_staff_end_hands_the_chat_back_to_the_bot():
    from bot.api import staff

    graph, ans = _staff_graph({"intent": "support"}, {"standalone": "How much is scaling?"})
    text = staff.end(graph, "S1", persist=False)
    assert "back with the assistant" in text
    out = _ask(graph, "S1", "How much is scaling?")
    assert out["reply"]["behavior"] == "answer" and len(ans.calls) == 1


@pytest.mark.skipif(not _db_ok(), reason="needs `docker compose up db`")
def test_save_lead_is_idempotent_per_thread_and_deletable():
    import uuid

    from bot.leads.store import delete_lead, save_lead

    thread = f"test-{uuid.uuid4()}"
    kw = {
        "name": "Test Patient",
        "contact": "test@example.com",
        "consent_text": "consent v1",
        "consented": True,
    }
    first = save_lead(thread, need="whitening", **kw)
    second = save_lead(thread, need="whitening + cleaning", **kw)  # node re-run / update
    assert first == second
    assert delete_lead(first) is True and delete_lead(first) is False
