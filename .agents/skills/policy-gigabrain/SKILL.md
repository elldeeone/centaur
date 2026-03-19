---
name: policy-gigabrain
description: "Policy team intelligence system for Hill briefings, staffer tracking, bill analysis, vote prediction, and legislative trend detection. Use when asked about congressional meetings, staffers, legislation, regulatory actions, rulemakings, whip counts, policy team workflows, or any question about OCC, SEC, CFTC, FDIC, FinCEN, Treasury guidance, stablecoins, GENIUS Act, market structure bills, or crypto regulation. Triggers on: policy question, regulatory impact, bill analysis, legislation impact on portfolio."
---

# Policy Gigabrain

Centralized intelligence system for government affairs. Generates meeting briefers, tracks staffers and legislators, monitors legislation, predicts votes, and surfaces portfolio-relevant regulatory actions.

## ⚠️ MANDATORY FIRST STEP: Policy Explainer Index

**Before answering ANY policy or regulatory question — before running web searches or reading external sources — you MUST first check the Paradigm Policy team's internal analysis.**

```bash
call gsuite docs_read '{"doc_id":"1yiKL4NgJfT0cAXehqHvaYC1mMezKdDhxwnr8Gm8aa6c"}'
```

This is the **Policy Explainer Index** ([link](https://docs.google.com/document/d/1yiKL4NgJfT0cAXehqHvaYC1mMezKdDhxwnr8Gm8aa6c/edit?tab=t.0)), maintained by the Policy team. It contains Paradigm's internal analysis and takes on major regulatory developments (GENIUS Act, FDIC rulemakings, SEC actions, etc.).

**Why this matters:** The Policy team's analysis includes Paradigm-specific context, portfolio impact assessments, and strategic framing that external sources cannot provide. External web searches should only supplement — never replace — the team's own work.

**Workflow:**
1. Read the Policy Explainer Index to check if the topic has been analyzed
2. If an explainer exists, read the linked analysis doc for full detail
3. Pull recent posts from **#gigabrain-feed** for additional policy intel:
   ```bash
   call slack get_channel_history '{"channel":"C0AM0TR8N91","limit":50}'
   ```
4. Only then supplement with external sources (OCC/SEC/CFTC websites, Federal Register, etc.) if needed
5. Always cite the internal analysis as the primary source

## Core Capabilities

| Function | Description |
|----------|-------------|
| **Meeting Briefers** | Auto-generate one-pagers for Hill meetings |
| **Staffer Tracking** | Track congressional/regulatory staff careers and relationships |
| **Bill Tracking** | Monitor legislation with momentum scores and status |
| **Vote Prediction** | Maintain whip sheets with stance, confidence, rationale |
| **Trend Detection** | Surface emerging patterns across jurisdictions |
| **Portfolio Impact** | Flag legislation affecting portfolio companies |

---

## Data Sources

### External APIs

| Source | Tool | Purpose |
|--------|------|---------|
| **Congress.gov** | `call congress` | Bills, members, committees, hearings, votes, amendments |
| **Federal Register** | `call fedreg` | Regulatory dockets, comment periods, rulemakings |
| **OpenFEC** | `call openfec` | Campaign contributions, candidates, committees, filings |
| **LegiStorm** | `call legistorm` | Congressional staff (`get_staff`), members (`get_members`), hearings (`get_hearings`), offices (`get_offices`), caucuses (`get_caucuses`), town halls (`get_townhalls`), privately funded travel (`get_trips`). All list endpoints require `updated_from`/`updated_to` date params (YYYY-MM-DD). |
| **Plural (Open States)** | `call plural` | State-level legislation (`search_bills`, `get_bill`), legislators (`search_people`), committees (`list_committees`), and legislative events (`list_events`). Use `jurisdiction` param (e.g. `"New York"`, `"California"`). Covers all 50 states + territories. |

### Internal Sources

| Source | Tool | Use For |
|--------|------|---------|
| **Shift / paradigmdb** | `call paradigmdb` | Portfolio companies, prior interactions, notes |
| **Slack** | `call slack search_messages` | Policy team discussions, intel |
| **#gigabrain-feed** | `call slack get_channel_history '{"channel":"C0AM0TR8N91"}'` | Curated policy intel feed — regulatory updates, legislative signals, and policy analysis posted by the policy team. Check this channel early in any policy workflow. |
| **GSuite** | `call gsuite` | Meeting notes, Hill interaction logs, Drive docs |
| **Archived docs / notes** | `call gsuite`, `call slack search_messages`, `call websearch search` | Archived policy documents, notes, and discussion |

---

## Workflows

### 1. Meeting Briefer Generation

Generate a policy briefing memo before Hill meetings.

**Trigger Phrases** (from Madison, Alex G., Justin, Stefan, Katie, or Caitlin):
- "Write a policy briefing memo for [Name, Title]"
- "Briefing memo for [Name]"
- "Policy briefer for [Name]"
- "Meeting brief for [Name]"

**Input:** Member/staffer name, meeting context, date

**Steps:**
1. Look up member profile via web search and LegiStorm:
   ```bash
   # LegiStorm member profile, committee assignments, staff
   call legistorm get_members '{"updated_from":"2025-01-01","updated_to":"2026-12-31","state_id":"[XX]"}'
   ```
2. Search internal sources for prior Paradigm interactions:
   ```bash
   call slack search_messages '{"query":"from:#policy [member_name]"}'
   call slack search_messages '{"query":"in:#gigabrain-feed [member_name]"}'
   call gsuite gmail_search '{"query":"[member_name]"}'
   call paradigmdb notes_search '{"query":"[member_name]"}'
   ```
3. Find relevant pending legislation:
   ```bash
   call congress bills '{"congress":119}'
   call congress bill '{"congress":119,"type":"s","number":123}'
   ```
4. Check FEC for campaign contribution context:
   ```bash
   call openfec candidates '{"name":"[member_name]"}'
   call openfec contributions '{"contributor_name":"[member_name]"}'
   ```
5. Cross-reference with paradigmdb for portfolio company relevance:
   ```bash
   call paradigmdb db_query '{"query":"SELECT * FROM \"Organization\" WHERE name ILIKE '\''%relevant_company%'\'';"}'
   ```
6. Generate structured briefer (see template below)

**Briefer Template:**
```
# Policy Briefing Memo: [Name, Title]
Date: [DATE] | Location: [LOCATION]

## Executive Summary
[2-3 sentence overview: who we're meeting, why it matters, and our primary objective.]

## Background
Write 1-2 paragraphs in full sentences covering: their current role and title, committee assignments, party and state, what they are currently prioritizing, key legislation they sponsor or co-sponsor, and any prior Paradigm interactions or touchpoints.

## Biography
Write 1-2 paragraphs in full sentences covering: key career milestones (Hill tenure, private sector, executive branch experience), education and alma mater, notable past and present committee or subcommittee positions, leadership roles, and relevant personal context such as state/district dynamics, known interests, or relationship dynamics.

## Crypto Knowledge
Write 1-2 paragraphs in full sentences covering: their overall familiarity level with crypto and digital assets, notable public statements or positions on crypto, DeFi, or stablecoins, relevant votes on crypto or fintech legislation, and the sophistication level of key staffers covering crypto and tech policy.

## Stance on Prediction Markets
Write 5-7 sentences covering: (1) the member's general opinion of prediction markets (public statements, letters signed, bills sponsored or co-sponsored); (2) relevant current events such as prediction market bills introduced, CFTC rulemakings, or court cases (e.g., Kalshi litigation); (3) whether those current events are likely to shift the member's position; and (4) any constituent, state, or district considerations that could influence their stance — including state gambling revenue, tribal gaming interests, state gambling laws, DGE/gaming commission enforcement actions, and the competitive dynamics between CFTC-regulated event contracts and state-licensed sportsbooks. Frame the analysis around Paradigm's position that prediction markets and all event contracts (including sports betting) should be regulated by the CFTC under exclusive federal jurisdiction.

## Goals
- **Primary Ask:** [What we want from this meeting]
- **Secondary Objectives:** [Relationship-building goals, intel to gather, positions to reinforce]
- **Success Criteria:** [How we'll know the meeting went well]

## Specific Topics To Address
1. [Topic 1 — context, our position, and suggested framing]
2. [Topic 2 — context, our position, and suggested framing]
3. [Topic 3 — context, our position, and suggested framing]
[Add as many as needed based on the meeting agenda and current legislative landscape]
```

### 2. Staffer Tracking

Track congressional and regulatory staffers relevant to crypto policy.

**Key Roles to Track:**
- Legislative Directors (LD)
- Committee Counsel (especially Banking, Finance, Agriculture)
- Personal Office Chiefs of Staff
- Leadership Staff
- Executive branch personnel rotating to legislative

**Data Points:**
- Current role and office
- Committee assignments
- Policy areas covered
- Career trajectory
- Paradigm touchpoints
- Alma mater (for relationship mapping)

**Flags to Surface:**
- Junior staff moving to senior roles (build relationships early)
- Committee transfers (new jurisdictional exposure)
- Executive → legislative rotations
- Departures from key offices

**Search Commands:**
```bash
# Find prior interactions with staffer
call slack search_messages '{"query":"[staffer_name]"}'
call gsuite gmail_search '{"query":"[staffer_name]"}'

# Check if mentioned in notes
call paradigmdb notes_search '{"query":"[staffer_name]"}'

# LegiStorm lookup — get_staff requires date range; use a wide window to find current staff
call legistorm get_staff '{"updated_from":"2025-01-01","updated_to":"2026-12-31","member_id":[member_id]}'
# Or search all recent staff updates
call legistorm get_staff '{"updated_from":"2026-01-01","updated_to":"2026-12-31","limit":20}'
```

### 3. Bill Tracking & Analysis

Monitor crypto-relevant legislation across federal and state jurisdictions.

**Federal Focus:**
- Senate Banking Committee
- House Financial Services Committee
- Agriculture Committees (CFTC jurisdiction)
- Judiciary Committees (DOJ, IP)

**State Priorities:** NY, TX, CA, WY, IL (active crypto agendas)

**Track Per Bill:**
- Bill number and title
- Sponsors and cosponsors
- Committee referral and status
- Hearing schedule
- Markup dates
- Floor action timeline
- Amendment activity
- Paradigm position (support/oppose/monitor)

**Search Commands:**
```bash
# Search federal bills via Congress.gov API
call congress bills '{"congress":119,"limit":50}'
call congress bill '{"congress":119,"type":"hr","number":4763,"detail":"summaries"}'

# Search federal hearings
call congress hearings '{"congress":119,"chamber":"senate"}'
call legistorm get_hearings '{"updated_from":"2026-01-01","updated_to":"2026-12-31","chamber":"S"}'
call legistorm get_townhalls '{"updated_from":"2026-01-01","updated_to":"2026-12-31"}'

# Search STATE-LEVEL bills via Plural (Open States)
call plural search_bills '{"jurisdiction":"New York","q":"cryptocurrency","sort":"updated_desc"}'
call plural search_bills '{"jurisdiction":"California","q":"digital assets"}'
# Get specific state bill details
call plural get_bill '{"jurisdiction":"New York","session":"2025-2026","bill_id":"S1234"}'

# Search for internal discussions
call slack search_messages '{"query":"[bill number]"}'
```

### 4. Vote Prediction / Whip Sheet

Maintain running vote counts for priority legislation.

**Whip Sheet Fields:**
| Field | Values |
|-------|--------|
| Stance | Support / Lean Support / Uncommitted / Lean Oppose / Oppose |
| Strength | Firm / Soft |
| Rationale | Why we believe this |
| Key Influencer | Who can move them |
| The Ask | What we need from them |
| Next Action | Follow-up task |
| Owner | Paradigm team member |
| Last Verified | Date of last confirmation |
| Evidence | Meeting notes, public statements |

**Prediction Inputs:**
- Historical voting patterns (VoteSmart, GovTrack)
- Cosponsor networks
- Public statements
- Committee behavior
- Party leadership signals
- Direct intelligence from meetings

**Update Process:**
1. After each Hill interaction, log stance update
2. Flag inconsistencies between public statements and private positions
3. Surface members whose stance has shifted

### 5. Coalition & Opposition Mapping

Track who is lobbying on which bills.

**Entities to Track:**
- Industry groups (Chamber, trade associations)
- Advocacy organizations
- Companies (competitors, allies)
- Think tanks
- Other crypto firms

**Per Entity:**
- Position on key bills
- Lobbying intensity (high/medium/low)
- Key contacts
- Coalition membership

### 6. Trend Detection

Surface emerging patterns before they become consensus.

**Signals to Monitor:**
- Bill introduction clusters (3+ states with similar language)
- Hearing topic frequency
- Floor statement themes
- Regulatory action patterns
- Model legislation from ALEC, ULC

**State AG Actions:**
- Enforcement actions in priority states
- Settlement patterns
- New investigation announcements

**Search Commands:**
```bash
# Search Federal Register for crypto-related regulatory actions
call fedreg search '{"query":"cryptocurrency","agency":"securities-and-exchange-commission"}'
call fedreg search '{"query":"digital assets","agency":"commodity-futures-trading-commission"}'
call fedreg search '{"query":"stablecoin","type":"PRORULE"}'

# State-level legislation via Plural (Open States) — check priority states
call plural search_bills '{"jurisdiction":"New York","q":"cryptocurrency","action_since":"2026-01-01"}'
call plural search_bills '{"jurisdiction":"Texas","q":"digital assets","action_since":"2026-01-01"}'
call plural search_bills '{"jurisdiction":"California","q":"blockchain","action_since":"2026-01-01"}'
call plural search_bills '{"jurisdiction":"Wyoming","q":"digital assets","action_since":"2026-01-01"}'
call plural search_bills '{"jurisdiction":"Illinois","q":"cryptocurrency","action_since":"2026-01-01"}'

# State-level events (hearings, floor sessions)
call plural list_events '{"jurisdiction":"New York","after":"2026-01-01","require_bills":true}'

# LegiStorm for congressional hearing trends
call legistorm get_hearings '{"updated_from":"2026-01-01","updated_to":"2026-12-31","limit":20}'

# Supplement with web search
call websearch search '{"query":"state cryptocurrency legislation 2026"}'
```

### 7. Regulatory Docket Monitoring

Track SEC, CFTC, FinCEN, OCC, Treasury rulemakings.

**Per Docket:**
- Agency and docket number
- Proposed rule summary
- Comment deadline
- Paradigm response status (draft/submitted/none)
- Portfolio company impact

**Search Commands:**
```bash
# Open comment periods
call fedreg comments-open '{"agency":"securities-and-exchange-commission"}'
call fedreg comments-open '{"agency":"commodity-futures-trading-commission"}'

# Search regulatory dockets
call fedreg search '{"query":"cryptocurrency","type":"RULE"}'
call fedreg search '{"query":"digital assets","type":"PRORULE","agency":"treasury-department"}'

# Get specific document details
call fedreg document '{"document_number":"2026-01234"}'
```

### 8. Portfolio Impact Flagging

Automatically surface legislation affecting portfolio companies.

**Process:**
1. When analyzing new legislation, check if any provisions affect:
   - Stablecoin issuers
   - DeFi protocols
   - Custody providers
   - Exchange operators
   - Any specific portfolio company sector
2. Query paradigmdb for relevant holdings:
   ```bash
   call paradigmdb notes_search '{"query":"[sector keyword]"}'
   call paradigmdb db_query '{"query":"SELECT o.name, o.description FROM \"Organization\" o WHERE o.description ILIKE '\''%stablecoin%'\'';"}'
   ```
3. Flag with explanation of potential impact

---

## Jurisdiction Mapping

### Who Has the Pen?

| Issue | Primary Jurisdiction | Key Committees |
|-------|---------------------|----------------|
| Market structure | SEC | Senate Banking, House Financial Services |
| Derivatives | CFTC | Senate/House Agriculture |
| Stablecoins | Mixed (SEC/OCC/Fed) | Banking + Financial Services |
| AML/KYC | FinCEN | Banking, Homeland Security |
| Sanctions | OFAC/Treasury | Banking, Foreign Affairs |
| Tax | Treasury/IRS | Finance, Ways & Means |

### Committee Staff Routing

For a given issue, route to the right staffer:
1. **Committee Counsel** for legislative drafting
2. **Personal Office LD** for member positioning
3. **Leadership Staff** for floor scheduling and whip operations

---

## Internal Accountability

### Log All Hill Interactions

After every Hill meeting or call:
1. Log in Shift as meeting note
2. Record commitments made (by us and by them)
3. Note follow-ups owed
4. Update relevant whip sheet entries
5. Tag portfolio-relevant intel

### Periodic Reports

**Weekly:** Policy team activity summary
**Monthly:** Legislative landscape shifts, whip sheet changes
**Quarterly:** Strategic assessment for leadership

---

## Natural Language Queries

The system should answer questions like:

- "What is Senator X's likely position on the GENIUS Act and why?"
- "Which bills introduced this session could affect our stablecoin portfolio companies?"
- "Who are the key staffers on Senate Banking covering crypto?"
- "What have we heard about stablecoin reserve requirements in the last 90 days?"
- "Generate a briefer for my meeting with Rep Y tomorrow"
- "What is the current whip count on [bill]?"
- "Which states have introduced DeFi-related bills this year?"

---

## Example Session

**User:** Policy briefer for Senator Lummis — meeting tomorrow about stablecoin legislation.

**Agent:**
1. Read Policy Explainer Index first for internal analysis
2. Pull recent #gigabrain-feed posts for stablecoin intel:
   ```bash
   call slack get_channel_history '{"channel":"C0AM0TR8N91","limit":50}'
   ```
3. Look up Senator Lummis via LegiStorm and web search:
   ```bash
   call legistorm get_members '{"updated_from":"2025-01-01","updated_to":"2026-12-31","state_id":"WY"}'
   call legistorm get_staff '{"updated_from":"2025-01-01","updated_to":"2026-12-31","member_id":[lummis_member_id]}'
   ```
4. Search internal sources:
   ```bash
   call slack search_messages '{"query":"Lummis"}'
   call slack search_messages '{"query":"in:#gigabrain-feed Lummis"}'
   call gsuite gmail_search '{"query":"Lummis"}'
   call paradigmdb notes_search '{"query":"Lummis"}'
   ```
5. Find current stablecoin legislation — federal and state:
   ```bash
   call congress bills '{"congress":119}'
   call plural search_bills '{"q":"stablecoin","action_since":"2026-01-01"}'
   ```
6. Check portfolio companies in stablecoin space
7. Generate briefer using template

---

### 9. Regulatory Filing Analysis

> **⚠️ ISOLATION NOTICE**: This workflow has its own style, format, and process rules. Do NOT import conventions from other Policy Gigabrain workflows (briefers, whip sheets, bill tracking). A policy brief is different from a regulatory analysis. Learn from each other, but keep them separate.

Process new regulatory documents (rules, guidance, no-action letters, NPRMs/ANPRMs, etc.) into a concise index entry and Slack message for the I&R team. The agent works in a strict five-step feedback loop with the user. **Do not skip steps or rush to a final deliverable before receiving user feedback.**

**Trigger Phrases** (from Stefan, Alex G., Madison, Justin, Katie, or Caitlin):
- "Process this filing" / "New regulatory action"
- "Ingest this rule/guidance/order"
- A link to a PDF, uploaded document, or regulatory filing URL

#### Step 1: Receive the Document

The user provides a link, uploaded PDF, or document file. Before doing anything else:

1. **Confirm access.** If you cannot open and parse the full document (scanned PDF without OCR, password-protected, login-walled URL), stop immediately and tell the user. Do not attempt workarounds. Do not proceed.
2. **Identify and note:** issuing agency or agencies; document type (final rule, interpretive guidance, ANPRM, NPRM, no-action letter, etc.); date of issuance; docket/release number; effective date and comment deadline, if any.
3. **Read the entire document, including all footnotes.** Important definitional language, limiting conditions, and novel holdings are frequently buried in footnotes. Never treat a footnote as a throwaway.
4. **Check the Policy Explainer Index** for existing entries this document supersedes, modifies, or cross-references:
   ```bash
   call gsuite docs_read '{"doc_id":"1yiKL4NgJfT0cAXehqHvaYC1mMezKdDhxwnr8Gm8aa6c"}'
   ```
5. **Check portfolio relevance** — query for companies that may be affected:
   ```bash
   call paradigmdb db_query '{"query":"SELECT name, description FROM \"Organization\" WHERE relevance = 1 ORDER BY name;"}'
   ```

#### Step 2: Generate an Initial Summary (.docx)

Before the user reads the document, produce a **2–3 page Word document (.docx)** summarizing the key issues. This summary helps the user read smarter — it is not a substitute for their reading. Generally follow the flow of the source document so the summary helps the reader read along; reorganize by topic only if there is a clear structural reason.

**The summary must cover:**
- What the document holds and what, if anything, changed from prior law or guidance
- Any assets, transaction types, companies, parties, or individuals explicitly named (these are the most actionable items for the investment team)
- Portfolio relevance: which portfolio companies or holdings appear most directly affected
- Effective date and any comment deadline, called out prominently
- Areas of legal uncertainty or apparent internal inconsistency in the source document (flag clearly, without alarmism)

**Summary style:**
- Plain English for a sophisticated non-lawyer reader
- No Latin, no jargon without explanation
- No condescending explanations of what things are — assume a sophisticated reader
- TNR 12pt, justified, single-spaced, 6pt/12pt paragraph spacing, 1-inch margins, black text only

**After delivering the .docx, say exactly:**
> "I have summarized the document above. Please read the source with this summary as context, then tell me: (1) what you think is most important or most relevant to the portfolio, and (2) anything you want added, removed, or reframed in the index entry. I will draft the entry once I have your feedback."

#### Step 3: Wait

**Wait for the user's feedback.** Do not draft the index entry yet. Do not ask follow-up questions unless the user's feedback is genuinely ambiguous.

The user has read the document and generally gets deference on what matters. That said, make suggestions and push back if you disagree — you may catch things the user missed, or the user may be wrong. Flag disagreements clearly, explain your reasoning, and let the user decide.

#### Step 4: Receive Feedback

- Take careful note of what the user identifies as most important, most relevant to the portfolio, or most worth highlighting. The user's feedback shapes emphasis and framing.
- If the user flags something you missed, acknowledge and incorporate it without debate (don't debate importance, do debate accuracy).
- If the user's feedback includes language you believe is legally imprecise or substantively incorrect, flag it clearly and respectfully, explain why, and let the user decide. Do not silently incorporate language you believe is wrong. Do not override the user's decision once made.
- Calibrate emphasis based on feedback. If the user says a point is secondary, treat it as secondary even if you find it analytically interesting.
- When the user returns a commented/edited document, treat it as a directive: apply the comments and re-upload immediately. No confirmation loop needed.

#### Step 5: Write the Index Entry and Slack Message

Produce two deliverables:
1. **Index entry** as a .docx file (format below)
2. **Slack message** in the chat (format below)

---

#### Index Entry Format

The index entry has five structural components. The first three (Title, BLUF, Our View) are non-negotiable. The remaining components should follow this pattern but may be adapted where the document's structure gives good reason.

**1. Title** (Heading 1)
- Format: `Analysis: [Agency] [Short Title] ([Date])`
- Example: `Analysis: SEC/CFTC Crypto Asset Securities Law Guidance (3/17/26)`
- Reflects the full scope of the document. Should almost always fit on a single line.

**2. BLUF** (Bold paragraph, labeled)
- Begins with "BLUF:" followed by 2–4 sentences. Maximum 4 sentences, no exceptions.
- What happened, why it matters, what changed.
- The document name or a clear reference should be a hyperlink to the source.
- Other contextual hyperlinks are permitted where they genuinely add value (e.g., a link to a superseded rule or comparable guidance from another agency).
- Write as if the reader has 20 seconds.

**3. Our View** (Bold paragraph, immediately after BLUF)
- A standalone bold paragraph with Paradigm's strategic read on the regulatory action.
- Covers impact on Paradigm or portfolio companies, with any input from the user.
- This is a second bold paragraph directly after the BLUF — no separate heading. Matches the ANPRM example format.

**4. Summary** (Bold label "Summary", plain body)
- Explains the document's scope and structure: what topics it covers, enumerated.
- Orients the reader to the Key Aspects that follow.

**5. Connector Paragraph** (Plain text, no heading)
- Procedural context: where the document comes from, what process led to it, effective date, whether it invites comments, what it supersedes.

**6. Key Aspects** (Heading 3, standalone line, bulleted list)
- "Key Aspects" as a standalone Heading 3 line (not inline label).
- Adapt the heading when warranted (e.g., "Key Questions" for an ANPRM).
- Each bullet has:
  - An **underlined** sub-header (NOT bold+underline) followed by plain prose
  - 2–3 sentences per bullet; substantive, not one-liners
  - Nesting permitted in moderation where it genuinely aids clarity
  - No hanging indents — first line and subsequent lines flush
- Page number references throughout, citing source document pages.
- Lead with substantive provisions in order of importance; close with procedural/effective-date matters.
- Selectively quote the filing for emphasis, but never copy in large chunks. Summarize and identify key points.
- When the source has a long list of unchanged items, condense to a brief summary (2–3 sentences) rather than copying verbatim.

**Total entry length:** 2–3 pages maximum. Every sentence must earn its place.

**File naming:** `[Agency] [Short Title] [MM.DD.YYYY].docx` — no underscores, date at end in MM.DD.YYYY format.

**Formatting:**
- TNR 12pt, justified, single-spaced, 1-inch margins, black text only
- Bold/underline stops BEFORE the colon (e.g., "Summary:" where only "Summary" is bold)
- Smart (curly) quotes throughout
- No hanging indents on bullets

---

#### Slack Message Format

3–5 sentences. No bullet points (exception: multiple companion documents in one message may use bullets). The audience is non-lawyers on the I&R team.

Answer three questions: What happened? What changed? Why does it matter for the portfolio or the industry?

Include the effective date or comment deadline if material to investment decisions.

**Always close with:** "Our full index entry is [here]." (user will add the actual link) or "We'll have a full index entry later today." if the entry is not yet finalized.

Do not editorialize beyond what the source document supports. Do not use legal citations.

---

#### Style Rules (Scoped to This Workflow)

- **Plain English.** Write for a sophisticated investor, not a court. No Latin. No jargon without brief explanation. Active voice where possible.
- **No condescending explanations.** Assume the reader is sophisticated. Do not explain what well-known institutions, legal concepts, or market structures are.
- **Em dashes: absolute minimum.** Before using one, ask whether a comma, period, or parentheses would work instead. They almost always will.
- **Pithiness.** Cut any sentence that repeats a point already made or provides background the reader already knows.
- **Legal precision.** Use the precise legal characterization the source document uses. Never conflate terms: "final rule" ≠ "interpretive guidance"; "no-action letter" ≠ "safe harbor"; "proposed rule" ≠ "final rule."
- **Named assets and parties.** If the document names specific assets, companies, or individuals, call them out. Verify portfolio company status against the Organization table before claiming relevance.
- **Footnotes get proportionate treatment.** Read them all. Cite footnote numbers. Important definitions and exceptions often live in footnotes, but do not overstate their importance.
- **Effective dates and comment deadlines.** Always include. Put in connector paragraph. If a meaningful comment deadline exists, consider giving it a dedicated Key Aspects bullet.
- **Supersession.** If the document supersedes prior guidance, identify what is superseded with precision. Do not overstate scope.
- **Page references.** Include page number citations from the source document throughout.
- **Hyperlinks.** Link to the source filing, any superseded documents, and any directly referenced companion documents (e.g., no-action letters, related orders).

---

#### Pre-Delivery Checklist

Before delivering the index entry, check for and address each of the following:

- [ ] Internal inconsistency in your draft (e.g., characterizing the same holding differently in BLUF vs Key Aspects)
- [ ] Legal imprecision in your own language (e.g., calling interpretive guidance a "rule")
- [ ] Anything the user said that you incorporated but believe may be legally incorrect — flag it, explain why, let the user decide
- [ ] Named assets, parties, or transaction types the investment team should know about that were not highlighted in user feedback
- [ ] Anything that supersedes or modifies prior indexed guidance
- [ ] Internal inconsistencies or drafting errors in the source document itself (note as observations, not conclusions)
- [ ] Portfolio company references verified against Organization table (relevance=1, not ex-portfolio)

---

#### What to Never Do

- Do not draft the index entry before receiving user feedback at Step 4
- Do not write a BLUF longer than 4 sentences
- Do not use bold within bullet bodies; underlined sub-header is the primary formatting element
- Do not use em dashes liberally — default to commas, periods, or parentheses
- Do not conflate legal terms of art — use the term the source document uses
- Do not make the Slack message sound like a legal brief
- Do not skip footnotes
- Do not proceed if you cannot parse the full document
- Do not include ex-portfolio companies as current portfolio references
- Do not copy large chunks of source text verbatim — summarize and identify key points

---

#### Example Index Entry Structure (SEC/CFTC Crypto Taxonomy)

```
Analysis: SEC/CFTC Crypto Asset Securities Law Guidance (3/17/26)

BLUF: [2-4 bold sentences — what happened, why it matters, what changed. Document name hyperlinked to source.]

[Bold paragraph — Our View on impact to Paradigm/portfolio. No heading.]

Summary: [Plain text — scope and structure of the document, enumerated topics.]

[Connector paragraph — procedural context, effective date, comment period, supersession.]

Key Aspects

• Not Securities: [underlined sub-header, plain prose, 2-3 sentences, page refs]
• Securities: [underlined sub-header, plain prose]
• Sometimes Securities: [underlined sub-header, plain prose]
  ...
```

#### Example Slack Messages

**Single document (Phantom No-Action Letter):**
> CFTC staff issued a no action letter to Phantom this morning, which says that CFTC will not take enforcement action against Phantom for providing access to Kalshi's prediction markets through their UI without registering as a derivatives broker. The letter highlights the fact that Phantom would not "hold, control, or take into custody" assets, and is merely "passively providing software" as the basis for the no-action decision. While not binding on the CFTC, the letter is consistent with other developer protections for neutral, non-custodial software providers, including those in the BRCA, and the fact that Agency staff have now taken this position in writing is a significant (and welcome) development. We'll have a full index entry later today.

**Single document with link (SEC/CFTC Taxonomy):**
> SEC and CFTC jointly issued final guidance for applying federal securities laws to crypto today. The release explicitly names more than fifteen tokens (including BTC, ETH, SOL, and XRP) that are *not* securities (and the list is not exhaustive), and confirms that mining, staking, wrapping, and retroactive airdrops do not constitute securities transactions. The guidance will be effective as soon as it is published (which will be soon), and supersedes prior, inconsistent staff guidance. Our full index entry is [here].

**Multiple companion documents (CFTC Prediction Markets):**
> As Alex mentioned above, CFTC issued two documents this morning related to prediction markets:
> • **First**, CFTC staff issued a guidance document with a series of "reminders" about existing swaps regulations…
> • **Second**, CFTC issued an Advanced Notice of Proposed Rulemaking. Our full index entry is [here]…

---

## Future Integrations

| Integration | Purpose | Priority |
|-------------|---------|----------|
| Shift direct integration | Portfolio cross-reference | Medium |
| Regulations.gov API | Docket comments, rulemaking tracking | Medium |
