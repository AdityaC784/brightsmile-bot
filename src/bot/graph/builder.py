"""Build the chat graph and its Postgres checkpointer.

    with open_checkpointer() as cp:
        graph = build_graph(checkpointer=cp)
        out = graph.invoke(new_turn(thread_id, "How much is a molar root canal?"),
                           {"configurable": {"thread_id": thread_id}})
        print(out["reply"]["answer"])

Flow:
    START -> classify -> support: rewrite -> support -> END (or -> handoff, see below)
                      -> lead (D3) / handoff (D4) / greet / refuse -> END
                      -> staff (while staff hold the conversation: relay, no bot reply) -> END
    A request for a person that also shows a red-flag symptom gets the bot's answer first,
    then the handoff. Staff replies, ending and the timeout are applied by the API with
    `update_state` (see bot.api.staff), not by graph nodes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from bot.config import get_settings
from bot.graph.nodes import Nodes
from bot.graph.state import ChatState, Turn


def build_graph(nodes: Nodes | None = None, checkpointer=None):
    n = nodes or Nodes()
    g = StateGraph(ChatState)
    g.add_node("classify", n.classify)
    g.add_node("rewrite", n.rewrite)
    g.add_node("support", n.support)
    g.add_node("lead", n.lead)
    g.add_node("handoff", n.handoff)
    g.add_node("staff", n.staff)
    g.add_node("greet", n.greet)
    g.add_node("refuse", n.refuse)

    g.add_edge(START, "classify")
    g.add_conditional_edges(
        "classify", n.route, ["rewrite", "lead", "handoff", "greet", "refuse", "staff"]
    )
    g.add_edge("rewrite", "support")
    g.add_conditional_edges("support", n.after_support, ["handoff", END])
    for leaf in ("lead", "handoff", "greet", "refuse", "staff"):
        g.add_edge(leaf, END)
    return g.compile(checkpointer=checkpointer)


def new_turn(thread_id: str, text: str, *, staff_online: bool = True) -> dict:
    """Input for one user turn. Per-turn fields are reset; the transcript accumulates."""
    return {
        "thread_id": thread_id,
        "staff_online": staff_online,
        "question": text,
        "messages": [Turn(role="user", content=text)],
        "standalone": "",
        "intent": "support",
        "emergency": False,
        "reply": {},
    }


@contextmanager
def open_checkpointer(database_url: str | None = None) -> Iterator[PostgresSaver]:
    """Postgres checkpointer (conversation state per thread_id). Creates its tables once."""
    with PostgresSaver.from_conn_string(database_url or get_settings().database_url) as cp:
        cp.setup()
        yield cp
