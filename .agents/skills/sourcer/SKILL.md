---
name: sourcer
description: "Sources and ranks candidates against a job description by parsing the JD, searching LinkedIn, GitHub, and X, enforcing hard user constraints, and publishing a Google Sheet shortlist or refined reslate tab. Use when asked to source candidates, build a recruiting list, rerun a role with tighter calibration, or rank prospects for an open role."
---

# Sourcer

Finds high-signal candidates for a single role and publishes a ranked Google Sheet shared with the requesting user.

## Use This Skill When

- The user asks to source candidates from a job description.
- The user wants a recruiting shortlist, talent map, or ranked prospect list.
- The user wants LinkedIn, GitHub, and X/Twitter signals combined into one sheet.

## Required Inputs

- A full JD or enough role context to reconstruct one.
- The requesting Slack user ID if the sheet must be shared automatically.
- Optional: target count, excluded companies, preferred school backgrounds, specific search seeds, or an existing spreadsheet link/ID when refining a prior slate.

If the user does not specify a target count, default to 25 ranked candidates.

## Output

Produce a Google Sheet with exactly these columns:

- `Name`
- `Title`
- `Company`
- `LinkedIn`
- `Email`
- `Location`
- `Score`
- `Notes`

Fresh slate:
- Create a new sheet and share it with the requesting user.

Refined slate:
- Preserve the existing spreadsheet.
- Add a new tab for the revised results.
- Write a short change log above the candidate table explaining what changed in the rerank or reslate. When publishing with `--spreadsheet-id`, include at least one `--change-log-entry`.

## Tooling Rules

1. Prefer browser automation for LinkedIn and GitHub when the deployment exposes `browser-use` or an equivalent browser tool.
2. Prefer `ptwittercli` for X/Twitter when it is installed; otherwise use the live `twitter` tool.
3. If tool contracts are unclear, run `call discover gsuite`, `call discover twitter`, `call discover harmonic`, `call discover slack`, and `call discover paradigmdb` once before proceeding.
4. Do not claim a source was crawled if you only inferred it from secondary search results.

## Workflow

1. Choose the sourcing mode before doing any search work.

Fresh slate:
- The user is asking for a brand-new sheet or has not referenced prior results.

Refine existing slate:
- The user is retrying the same role, tightening calibration, rejecting prior candidates, or asking to keep working in the same spreadsheet.
- Require the existing sheet link or spreadsheet ID. If it is not in the thread, ask for it before publishing.

Do not treat a retry as a fresh run.

2. Parse the JD and any follow-up feedback into a structured search spec.

Capture:
- role type
- seniority
- must-have skills
- nice-to-have skills
- industry context
- hard location rules
- compensation or timing clues if present

If the user is refining a prior slate, write a calibration checklist in your working notes before sourcing:
- Inclusion checklist: explicit positives and must-have attributes to amplify.
- Exclusion checklist: explicit negatives and traits to eliminate.

Treat these as hard filters whenever the user states them explicitly:
- company set
- geography
- hands-on depth
- school signal
- leadership scope

Write 2-4 short change-log bullets that explain how this pass differs from the prior pass. If any of those constraints are materially ambiguous, ask one short clarifying question before sourcing.

3. Identify the requester email when needed.

If the request came from Slack and you are creating a new sheet, run:

```bash
call slack get_user_email '{"user_id":"<requester_slack_user_id>"}'
```

If you are refining an existing sheet, preserve the current sharing setup unless the user explicitly asks you to share it with someone else.

4. Build the search plan.

Create search strings for each source:
- LinkedIn title + company background + location
- GitHub language/domain + role keywords + location
- X/Twitter bio keywords + employer history + location

Prefer multiple narrow searches over one broad search. Start with the hard filters first, then layer in high-signal background filters such as:
- elite CS / math / engineering programs
- early employee windows at hypergrowth companies
- direct domain adjacency

5. Gather candidates from multiple sources.

LinkedIn:
- Use `browser-use` if available to search profiles and capture current title, company, location, and LinkedIn URL.
- If browser automation is unavailable, use public web search and Harmonic as a fallback, but mark the source confidence lower in notes.

GitHub:
- Use browser automation if available to inspect profiles, pinned repos, contribution recency, and obvious location clues.
- Otherwise use `call websearch search` to find public GitHub profiles and repositories, then read the linked public pages directly.

X/Twitter:
- Use `ptwittercli` if installed.
- Otherwise use `call twitter search_tweets`, `call twitter get_user`, `call twitter get_timeline`, `call twitter get_following`, and `call twitter get_followers` as needed.

Structured supplement:
- Use `call harmonic search_people_recruiting` when you need a fast candidate pool for a role/location combination.
- Use `call harmonic enrich_person` for finalists when you need cleaner work-history or education data.

6. Normalize every candidate into one record.

For each candidate, collect:
- `name`
- `title`
- `company`
- `linkedin`
- `email`
- `location`
- `x_handle`
- `github_url`
- `notes`
- `scores.title_correspondence`
- `scores.educational_foundation`
- `scores.professional_trajectory`
- `scores.talent_density`
- `scores.timing_window`

Keep the notes factual. Include why the person is interesting, which hard filters they satisfy, and any uncertainty.

7. Enforce hard filters before scoring.

Location:
- If the JD or refinement request has a hard location requirement, exclude anyone clearly outside it.
- If the candidate location is ambiguous and you cannot resolve it quickly from profile evidence, exclude the candidate instead of guessing.

Feedback-driven hard filters:
- When the user narrows to explicit companies, only include candidates from those companies or direct equivalents the user approved.
- When the user asks for stronger hands-on depth, exclude candidates whose evidence is mostly product, strategy, or people management.
- When the user asks for stronger school signal, exclude candidates without the requested academic foundation or equivalent technical proof.
- When the user asks for leaders, exclude IC-only profiles unless the user explicitly broadened the scope.

Paradigm portfolio exclusion:
- Pull the current portfolio company list from `call paradigmdb db_organizations '{"limit":200}'`.
- Exclude anyone whose current employer is a Paradigm portfolio company.
- Do not exclude former employees of portfolio companies unless the user asked for that.

If the hard filters collapse the market, say that the market is thin and return the smaller high-conviction slate. Do not pad the sheet with weak matches.

8. Add the Paradigm follow signal.

This signal is a priority boost, not an inclusion requirement.

- If the user supplies Paradigm team X handles, use those.
- Otherwise, use any locally maintained handle list if one already exists.
- If no handle list is available, do not invent one. Mark the signal as unknown and do not penalize the candidate.

When you do have handles, compare the candidate's X handle against Paradigm-team following lists using `twitter.get_following`. Add the evidence to notes, for example: `Followed by 2 Paradigm team accounts on X.`

9. Score every candidate on the five weighted criteria.

Use a 0-5 subscore for each criterion.

- Title correspondence: 25%
- Educational foundation: 20%
- Professional trajectory: 20%
- Talent density of prior orgs: 20%
- Timing window: 15%

Scoring rubric:

- `title_correspondence`
  - 5: exact role and scope match
  - 3: adjacent role or one step above/below
  - 1: weak title match despite some relevant skills
- `educational_foundation`
  - 5: exceptional technical or analytical foundation, including elite universities or equivalent proof of depth
  - 3: strong but not standout foundation
  - 1: limited evidence
- `professional_trajectory`
  - 5: repeated promotions, strong scope expansion, founder or early builder patterns
  - 3: solid trajectory with moderate evidence of growth
  - 1: flat or unclear trajectory
- `talent_density`
  - 5: prior orgs are unusually high-signal and include early hypergrowth windows such as Stripe pre-2016 or Coinbase pre-2017
  - 3: good companies, but less concentrated talent density
  - 1: little signal from prior org set
- `timing_window`
  - 5: obvious transition window, such as post-acquisition, recent team change, or 2-4 years into current role
  - 3: plausible but not obvious timing
  - 1: likely difficult to move now

Prioritize elite university alumni, early employees at hypergrowth startups, and candidates followed by Paradigm team members on X when the evidence supports it.

10. Publish the shortlist.

Fresh slate:

```bash
uv run .agents/skills/sourcer/scripts/sourcer.py publish \
  --input /tmp/sourcer-candidates.json \
  --title "<Role> Sourcer Shortlist" \
  --share-with "<requester_email>"
```

Refine existing slate:

```bash
uv run .agents/skills/sourcer/scripts/sourcer.py publish \
  --input /tmp/sourcer-candidates.json \
  --title "<Role> Sourcer Shortlist" \
  --spreadsheet-id "<existing_sheet_id_or_url>" \
  --tab-name "<unique refined tab name>" \
  --change-log-entry "<What tightened in this pass>" \
  --change-log-entry "<What you excluded this time>" \
  --change-log-entry "<Why the ranking or count changed>"
```

The script computes the weighted score, sorts the candidates, and either creates a new Google Sheet or appends a new tab to the existing spreadsheet with the change log above the candidate table.
If a refined publish is interrupted after the tab is created, rerun the same command with the same `--tab-name`; the script treats the existing tab as a replay and rewrites the change log and candidate table.

Use `--top-n <count>` if you want to cap the exported set.

11. Report back with the artifact.

Return:
- the Google Sheet link
- how many candidates made the final sheet
- the top 3-5 names with one-line reasons
- whether the market was thin after the hard filters
- any hard blockers such as missing location data or unavailable browser automation

## Candidate JSON Shape

The publishing script accepts either a bare array or an object with `candidates`.

```json
{
  "candidates": [
    {
      "name": "Jane Doe",
      "title": "Staff Backend Engineer",
      "company": "Example",
      "linkedin": "https://www.linkedin.com/in/jane-doe",
      "email": "jane@example.com",
      "location": "New York, NY",
      "notes": "Ex-Stripe 2015 hire. Followed by 2 Paradigm team accounts on X.",
      "scores": {
        "title_correspondence": 4.5,
        "educational_foundation": 4,
        "professional_trajectory": 4.5,
        "talent_density": 5,
        "timing_window": 3.5
      }
    }
  ]
}
```

## Guardrails

- Never include someone currently at a Paradigm portfolio company.
- Never relax a hard JD location constraint.
- Never relax an explicit company, geography, hands-on depth, school-signal, or leadership-scope constraint from a retry.
- Do not guess an email address.
- Do not use private or non-consensual data sources.
- Do not treat missing Paradigm-follow data as a negative signal.
- Keep notes concise and evidence-based.
- Do not pad a refined slate with weak candidates just to hit the previous count.
- Do not treat retry feedback as informal commentary; convert it into the inclusion and exclusion checklist before re-sourcing.
- If browser automation is unavailable, say so explicitly and continue with the best public-web and tool-backed fallback.
