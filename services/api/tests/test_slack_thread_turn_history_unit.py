from api.workflows.slack_thread_turn import _lookup_last_unresolved_ask_from_history


def test_recovery_hydration_prefers_mentioned_request_over_later_chatter():
    prior_ask, provenance = _lookup_last_unresolved_ask_from_history(
        [
            {
                "message_id": "slack:ask",
                "role": "user",
                "parts": [{"type": "text", "text": "do call discover and go from there"}],
                "user_id": "U1",
                "metadata": {"slack": {"is_mention": True}},
            },
            {
                "message_id": "slack:assistant",
                "role": "assistant",
                "parts": [{"type": "text", "text": "Ran discovery."}],
                "user_id": "UBOT",
                "metadata": {"slack": {"is_mention": False}},
            },
            {
                "message_id": "slack:chatter",
                "role": "user",
                "parts": [{"type": "text", "text": "cool i think chat sdk will work tomorrow"}],
                "user_id": "U1",
                "metadata": {"slack": {"is_mention": False}},
            },
        ],
        user_id="U1",
        current_message_id="slack:again",
    )

    assert prior_ask == "do call discover and go from there"
    assert provenance["hydrated_from_message_id"] == "slack:ask"
    assert provenance["hydrated_from_mention"] is True

