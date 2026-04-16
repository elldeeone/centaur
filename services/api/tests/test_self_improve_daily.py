"""Unit tests for the pure helpers in workflows.self_improve_daily.

These target the pieces of the nightly self-improvement workflow that do
not require a WorkflowContext: scorecard rendering, Slack link
formatting, user-name extraction, child-result annotation, and the
slack_narrative privacy strip that runs before the implementing child
workflow ever sees the fix packet.
"""

from __future__ import annotations

from workflows.self_improve_daily import (
    SLACK_ONLY_FIX_FIELDS,
    _annotate_child_results_with_narratives,
    _build_scorecard_markdown,
    _message_user_display,
    _render_source_thread_links,
    _slack_pr_link,
    _slack_thread_archive_url,
    _strip_slack_only_fields,
)


def test_slack_pr_link_uses_angle_bracket_format() -> None:
    # Slack renders `<url|text>` as a link; GitHub-style `[text](url)` is
    # surfaced as literal characters, which is the bug we saw in the
    # first rendered nightly scorecard post.
    link = _slack_pr_link(322, "https://github.com/paradigmxyz/centaur/pull/322")
    assert link == "<https://github.com/paradigmxyz/centaur/pull/322|#322>"


def test_slack_pr_link_handles_missing_pieces() -> None:
    assert _slack_pr_link("", "") == ""
    assert _slack_pr_link(322, "") == "#322"
    assert _slack_pr_link("", "https://example.test/pr") == "<https://example.test/pr>"


def test_message_user_display_prefers_user_name_then_name_then_username() -> None:
    assert (
        _message_user_display(
            {"metadata": {"user_name": "Josie", "name": "ignored", "username": "j"}}
        )
        == "Josie"
    )
    assert (
        _message_user_display(
            {"metadata": {"name": "Josie Kim", "username": "j"}}
        )
        == "Josie Kim"
    )
    assert _message_user_display({"metadata": {"username": "josie"}}) == "josie"
    assert _message_user_display({"metadata": {}}) == ""
    assert _message_user_display({}) == ""


def test_message_user_display_falls_back_to_user_id_cache() -> None:
    # The slackbot only persists `user_id` in metadata — this is the real
    # production shape. The cache resolves those IDs to Slack usernames.
    message = {"metadata": {"user_id": "U076CL29AP5"}}
    cache = {"U076CL29AP5": "arjun", "U01XYZ": "josie"}
    assert _message_user_display(message, cache) == "arjun"


def test_message_user_display_prefers_explicit_name_over_cache() -> None:
    message = {"metadata": {"user_id": "U01XYZ", "user_name": "Josie Kim"}}
    cache = {"U01XYZ": "josie"}
    assert _message_user_display(message, cache) == "Josie Kim"


def test_message_user_display_cache_miss_returns_empty() -> None:
    message = {"metadata": {"user_id": "U_UNKNOWN"}}
    cache = {"U01XYZ": "josie"}
    assert _message_user_display(message, cache) == ""
    # With no cache at all, no crash and no fake name.
    assert _message_user_display(message) == ""


def test_slack_thread_archive_url_strips_dot_from_ts() -> None:
    url = _slack_thread_archive_url("C0A82R7S80N", "1776374169.372999")
    assert url == "https://slack.com/archives/C0A82R7S80N/p1776374169372999"


def test_slack_thread_archive_url_handles_missing_pieces() -> None:
    assert _slack_thread_archive_url("", "1776374169.372999") == ""
    assert _slack_thread_archive_url("C0A82R7S80N", "") == ""
    assert _slack_thread_archive_url("", "") == ""


def test_render_source_thread_links_renders_slack_format_link_text() -> None:
    single = _render_source_thread_links(
        [{"channel": "C0A82R7S80N", "thread_ts": "1776374169.372999"}]
    )
    assert single == "<https://slack.com/archives/C0A82R7S80N/p1776374169372999|thread>"


def test_render_source_thread_links_joins_multiple_threads() -> None:
    multi = _render_source_thread_links(
        [
            {"channel": "C0A82R7S80N", "thread_ts": "1776374169.372999"},
            {"channel": "C0ASR4NFLPR", "thread_ts": "1776222625.548429"},
        ]
    )
    assert (
        multi
        == "<https://slack.com/archives/C0A82R7S80N/p1776374169372999|thread>, "
        "<https://slack.com/archives/C0ASR4NFLPR/p1776222625548429|thread>"
    )


def test_render_source_thread_links_skips_entries_missing_channel_or_ts() -> None:
    # Defense in depth: a malformed entry should not leak `|thread>` with an
    # empty URL into the Slack post.
    partial = _render_source_thread_links(
        [
            {"channel": "C0A82R7S80N"},
            {"thread_ts": "1776374169.372999"},
            {"channel": "C0A82R7S80N", "thread_ts": "1776374169.372999"},
        ]
    )
    assert partial == "<https://slack.com/archives/C0A82R7S80N/p1776374169372999|thread>"


def test_render_source_thread_links_empty_returns_empty_string() -> None:
    assert _render_source_thread_links([]) == ""
    assert _render_source_thread_links(None) == ""


def test_strip_slack_only_fields_removes_narrative_but_keeps_rest() -> None:
    packet = {
        "title": "Tighten verification reminder",
        "fix_type": "prompt_tweak",
        "target_surface": "tools/personas/eng/PROMPT.md",
        "what_to_change": "Add lint check reminder.",
        "slack_narrative": "Josie hit this on Tuesday.",
    }

    stripped = _strip_slack_only_fields(packet)

    assert "slack_narrative" not in stripped
    for field in SLACK_ONLY_FIX_FIELDS:
        assert field not in stripped
    assert stripped["title"] == "Tighten verification reminder"
    # Input must not be mutated — callers still need the narrative for Slack.
    assert packet["slack_narrative"] == "Josie hit this on Tuesday."


def test_annotate_child_results_with_narratives_pairs_by_position() -> None:
    selected_fixes = [
        {
            "title": "Tighten verification reminder",
            "fix_type": "prompt_tweak",
            "dominant_failure_mode": "verification_miss",
            "slack_narrative": "Josie hit the lint gap Tuesday; Matt Thursday.",
            "source_threads": [
                {
                    "thread_key": "C0A82R7S80N:1776374169.372999",
                    "channel": "C0A82R7S80N",
                    "thread_ts": "1776374169.372999",
                }
            ],
        },
        {
            "title": "Add triage-first guidance",
            "fix_type": "workflow_fix",
            "dominant_failure_mode": "intent_miss",
            "slack_narrative": "Asher asked why morning-brief never posted.",
        },
    ]
    child_results = [
        {"pr_number": 42, "pr_url": "https://example.test/pr/42", "title": "Add lint check"},
        {"error": "child workflow timed out", "child_run_id": "wfr_abc"},
    ]

    annotated = _annotate_child_results_with_narratives(
        child_results=child_results,
        selected_fixes=selected_fixes,
    )

    assert annotated[0]["slack_narrative"].startswith("Josie hit the lint")
    assert annotated[0]["fix_type"] == "prompt_tweak"
    assert annotated[0]["dominant_failure_mode"] == "verification_miss"
    # Source threads must travel with the narrative so the scorecard can
    # render clickable `thread` links under each opened PR.
    assert annotated[0]["source_threads"][0]["channel"] == "C0A82R7S80N"
    # Title that already exists on the child result must win over the
    # upstream fix title (the child's PR title is what shipped).
    assert annotated[0]["title"] == "Add lint check"
    assert annotated[1]["slack_narrative"].startswith("Asher asked")
    # Missing PR data still gets paired with its narrative so the
    # failure line in the scorecard can explain what we were trying.
    assert annotated[1]["error"] == "child workflow timed out"


def test_annotate_child_results_tolerates_length_mismatch() -> None:
    # Reality: one of the kids failed to start and never produced an
    # output_json. The annotator must not crash and must leave the
    # fixes-we-actually-have alone.
    annotated = _annotate_child_results_with_narratives(
        child_results=[
            {"pr_number": 1, "pr_url": "https://x.test/1"},
            {"pr_number": 2, "pr_url": "https://x.test/2"},
            {"error": "bad"},
        ],
        selected_fixes=[
            {"title": "Fix A", "slack_narrative": "A narrative."},
        ],
    )

    assert annotated[0]["slack_narrative"] == "A narrative."
    assert "slack_narrative" not in annotated[1]
    assert "slack_narrative" not in annotated[2]


def _scorecard_review_fixture() -> dict:
    return {
        "tasks_reviewed": 8,
        "below_bar_count": 3,
        "below_bar_rate": 0.375,
        "task_reviews": [
            {"composite_score": 82},
            {"composite_score": 60},
            {"composite_score": 55},
        ],
        "top_failure_modes": [
            {"failure_mode": "verification_miss", "count": 3},
            {"failure_mode": "intent_miss", "count": 2},
        ],
        "selected_fixes": [
            {
                "title": "Tighten verification reminder",
                "fix_type": "prompt_tweak",
                "slack_narrative": (
                    "Josie's pulumi change shipped without lint on Tuesday and Matt "
                    "hit the same gap Thursday, so code-change tasks keep bypassing "
                    "ruff."
                ),
                "source_threads": [
                    {
                        "thread_key": "C0A82R7S80N:1776374169.372999",
                        "channel": "C0A82R7S80N",
                        "thread_ts": "1776374169.372999",
                    }
                ],
            },
            {
                "title": "Add triage-first workflow guidance",
                "fix_type": "workflow_fix",
                "slack_narrative": (
                    "Asher asked why the morning-brief workflow never posted and the "
                    "agent proposed a redesign instead of checking logs."
                ),
                "source_threads": [
                    {
                        "thread_key": "C0ASR4NFLPR:1776222625.548429",
                        "channel": "C0ASR4NFLPR",
                        "thread_ts": "1776222625.548429",
                    }
                ],
            },
        ],
    }


def _scorecard_synthesis_fixture() -> dict:
    return {
        "opportunities_found": 2,
        "opportunities": [
            {
                "opportunity_type": "new_persona",
                "title": "Editorial persona for decision memos",
            },
            {
                "opportunity_type": "new_workflow_idea",
                "title": "Guided bootstrap for policy-news monitors",
            },
        ],
        "selected_builds": [
            {
                "opportunity_type": "new_persona",
                "title": "Editorial persona for decision memos",
                "slack_narrative": (
                    "Matt and Dan both asked for crisper decision memos three times "
                    "last week; no existing persona covers that stance."
                ),
                "source_threads": [
                    {
                        "thread_key": "C0ZZZ:1776100000.000000",
                        "channel": "C0ZZZ",
                        "thread_ts": "1776100000.000000",
                    },
                    {
                        "thread_key": "C0YYY:1776200000.000000",
                        "channel": "C0YYY",
                        "thread_ts": "1776200000.000000",
                    },
                ],
            },
        ],
    }


def test_build_scorecard_markdown_has_clean_indentation() -> None:
    # This is the regression bug from the first rendered nightly post:
    # textwrap.dedent with multi-line f-string substitutions lost its
    # common prefix on continuation lines, leaving an 8-space indent on
    # the top-level lines. Every line the renderer produces must start
    # at column 0 (top-level) or column 2 (sub-bullet).
    child_results = [
        {
            "pr_number": 322,
            "pr_url": "https://github.com/paradigmxyz/centaur/pull/322",
            "title": "Tighten verification",
            "slack_narrative": (
                "Josie's pulumi change shipped without lint on Tuesday and Matt "
                "hit the same gap Thursday, so code-change tasks keep bypassing "
                "ruff."
            ),
            "fix_type": "prompt_tweak",
        },
    ]

    md = _build_scorecard_markdown(
        review=_scorecard_review_fixture(),
        synthesis=_scorecard_synthesis_fixture(),
        child_results=child_results,
        notifier_stats={"merged_prs": 1, "deployed_prs": 1, "source_threads_notified": 2},
    )

    for line in md.splitlines():
        if not line.strip():
            continue
        leading_spaces = len(line) - len(line.lstrip(" "))
        assert leading_spaces in {0, 2, 4}, (
            f"unexpected leading whitespace ({leading_spaces} spaces) on line: {line!r}"
        )


def test_build_scorecard_markdown_uses_slack_link_format_not_markdown_link() -> None:
    md = _build_scorecard_markdown(
        review={"tasks_reviewed": 0, "selected_fixes": []},
        synthesis={"opportunities": [], "selected_builds": []},
        child_results=[
            {
                "pr_number": 322,
                "pr_url": "https://github.com/paradigmxyz/centaur/pull/322",
                "title": "Tighten verification",
            }
        ],
        notifier_stats={"merged_prs": 0, "deployed_prs": 0, "source_threads_notified": 0},
    )

    assert "<https://github.com/paradigmxyz/centaur/pull/322|#322>" in md
    # GitHub-style markdown would be the bug. Make sure it is truly gone.
    assert "[#322]" not in md
    assert "](https://github.com" not in md


def test_build_scorecard_markdown_renders_per_fix_narratives() -> None:
    md = _build_scorecard_markdown(
        review=_scorecard_review_fixture(),
        synthesis=_scorecard_synthesis_fixture(),
        child_results=[
            {
                "pr_number": 42,
                "pr_url": "https://example.test/pr/42",
                "title": "Add lint check",
                "slack_narrative": "Josie's Tuesday pulumi change failed CI because ruff didn't run.",
            }
        ],
        notifier_stats={"merged_prs": 0, "deployed_prs": 0, "source_threads_notified": 0},
    )

    # Narratives should land under the Gap Analysis fixes.
    assert "Josie's pulumi change shipped without lint" in md
    assert "Asher asked why the morning-brief" in md
    # ...and under the Learning Synthesis builds.
    assert "Matt and Dan both asked for crisper decision memos" in md
    # ...and next to the opened PR.
    assert "Josie's Tuesday pulumi change failed CI because ruff" in md
    # The _Why:_ prefix signals the sub-bullet type and renders as italic
    # in Slack mrkdwn. Make sure it is wired up.
    assert "_Why:_" in md


def test_build_scorecard_markdown_handles_empty_state() -> None:
    md = _build_scorecard_markdown(
        review={"tasks_reviewed": 0, "selected_fixes": []},
        synthesis={"opportunities": [], "selected_builds": []},
        child_results=[],
        notifier_stats={"merged_prs": 0, "deployed_prs": 0, "source_threads_notified": 0},
    )

    assert "*Self Improve Nightly*" in md
    assert "- none selected" in md
    assert "- none found" in md
    assert "- none opened" in md
    assert "- none" in md
    # No tracebacks, no crashes — the message remains postable.
    assert md.startswith("*Self Improve Nightly*")


def test_build_scorecard_markdown_renders_thread_links_in_all_sections() -> None:
    # Each section (Gap Analysis, Learning Synthesis, Execution) should emit
    # a `_Thread:_` sub-bullet with a clickable Slack link whenever the
    # fix / build / PR carries source_threads. The link text is always
    # the literal word "thread" so a reader knows where to click.
    child_results = [
        {
            "pr_number": 42,
            "pr_url": "https://github.com/paradigmxyz/centaur/pull/42",
            "title": "Tighten verification",
            "slack_narrative": "Josie's Tuesday pulumi change failed CI because ruff didn't run.",
            "source_threads": [
                {
                    "thread_key": "C0A82R7S80N:1776374169.372999",
                    "channel": "C0A82R7S80N",
                    "thread_ts": "1776374169.372999",
                }
            ],
        }
    ]

    md = _build_scorecard_markdown(
        review=_scorecard_review_fixture(),
        synthesis=_scorecard_synthesis_fixture(),
        child_results=child_results,
        notifier_stats={"merged_prs": 0, "deployed_prs": 0, "source_threads_notified": 0},
    )

    gap_url = "<https://slack.com/archives/C0A82R7S80N/p1776374169372999|thread>"
    intent_miss_url = "<https://slack.com/archives/C0ASR4NFLPR/p1776222625548429|thread>"

    # Gap Analysis section — one `Thread:` per fix.
    assert f"    - _Thread:_ {gap_url}" in md
    assert f"    - _Thread:_ {intent_miss_url}" in md
    # Learning Synthesis build uses `Threads:` (plural) because it has two.
    assert "    - _Threads:_ " in md
    assert "<https://slack.com/archives/C0ZZZ/p1776100000000000|thread>" in md
    assert "<https://slack.com/archives/C0YYY/p1776200000000000|thread>" in md
    # Execution section gets the link next to the PR too.
    exec_section = md.split("*Execution*", 1)[1]
    assert f"_Thread:_ {gap_url}" in exec_section


def test_build_scorecard_markdown_never_emits_github_thread_link_syntax() -> None:
    md = _build_scorecard_markdown(
        review=_scorecard_review_fixture(),
        synthesis=_scorecard_synthesis_fixture(),
        child_results=[],
        notifier_stats={"merged_prs": 0, "deployed_prs": 0, "source_threads_notified": 0},
    )

    # Guard against regressing into `[thread](https://slack.com/...)` form,
    # which Slack renders as literal text.
    assert "[thread](https://slack.com" not in md
    assert "](https://slack.com" not in md
