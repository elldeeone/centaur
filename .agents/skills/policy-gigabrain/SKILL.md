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
3. Only then supplement with external sources (OCC/SEC/CFTC websites, Federal Register, etc.) if needed
4. Always cite the internal analysis as the primary source

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
| **LegiStorm** | `call legistorm` | Staffer database, career tracking, office moves |
| **Plural Policy** | — | Bill momentum scores, AI summaries (future) |

### Internal Sources

| Source | Tool | Use For |
|--------|------|---------|
| **Shift / paradigmdb** | `call paradigmdb` | Portfolio companies, prior interactions, notes |
| **Slack** | `call slack search_messages` | Policy team discussions, intel |
| **GSuite** | `call gsuite` | Meeting notes, Hill interaction logs, Drive docs |
| **Archived docs / notes** | `call gsuite`, `call slack search_messages`, `call websearch search` | Archived policy documents, notes, and discussion |

---

## Workflows

### 1. Meeting Briefer Generation

Generate a policy briefing memo before Hill meetings.

**Trigger Phrases** (from Madison, Dominique, Alex G., Justin, Stefan, Katie, or Caitlin):
- "Write a policy briefing memo for [Name, Title]"
- "Briefing memo for [Name]"
- "Policy briefer for [Name]"
- "Meeting brief for [Name]"

**Input:** Member/staffer name, meeting context, date, and any requested topical sections (for example: crypto knowledge, prediction markets, AI, defense tech, robotics)

**Steps:**
1. Read the Policy Explainer Index first, then check Shift / slack / notes for prior Paradigm context and any portfolio-company relevance that sharpens the read.
   ```bash
   call slack search_messages '{"query":"from:#policy [member_name]"}'
   call paradigmdb notes_search '{"query":"[member_name]"}'
   call paradigmdb notes_search '{"query":"[relevant_topic_or_company]"}'
   ```
2. Verify any company example in Shift before mentioning it. Only use Paradigm portfolio companies.
3. Look up member profile via web search (committee assignments, voting history, public statements, current lane)
4. Find relevant pending legislation:
   ```bash
   call congress bills '{"congress":119}'
   call congress bill '{"congress":119,"type":"s","number":123}'
   ```
5. Check FEC for campaign contribution context when it materially helps:
   ```bash
   call openfec candidates '{"name":"[member_name]"}'
   call openfec contributions '{"contributor_name":"[member_name]"}'
   ```
6. Cross-reference with paradigmdb for portfolio company relevance:
   ```bash
   call paradigmdb db_query '{"query":"SELECT * FROM \"Organization\" WHERE name ILIKE '\''%relevant_company%'\'';"}'
   ```
7. Generate operator brief in the format below

**Format Rules:**
- Write these as **operator briefs**, not formal research memos.
- Use short paragraphs and crisp bullets. Prefer direct sentences over issue-summary prose.
- Do **not** use the old memo scaffolding (`Executive Summary`, `Background`, `Primary Ask / Secondary Objectives / Success Criteria`, `Specific Topics To Address`) unless a policy-team member explicitly asks for it.
- Start with the office read, then move into the lanes that matter for the meeting.
- Include only the topical sections the policy-team prompt asks for or that clearly earn their place. Common sections include `Crypto Knowledge`, `Stance on Prediction Markets`, `Stance on AI`, `Stance on Defense Tech`, and `Stance on Robotics`.
- Use less interpretive language. Favor "this is the lane" and "this is what they are likely to care about" over broader ideological framing.
- If you mention a portfolio company, it must be a **Paradigm portfolio company verified in Shift**. Use **one company by default**. Mention it only if it materially sharpens the memo.
- Put the company mention in the **second bullet** of the relevant topical section after first establishing the member read.
- Company mentions should focus on the business and the concrete policy lane it makes real. Use public toplines and high-level internal context when helpful, but do not add a separate company sidebar.

**Operator-Brief Template:**
```
Landscape Summary
[2 short paragraphs on why this office matters, what lane is productive, and what frame to avoid.]

Biography
[1-2 short paragraphs on role, committee seats, prior career, and anything that changes how they process the issue.]

Goals
• [Goal 1]
• [Goal 2]
• [Goal 3]

Crypto Knowledge
• [Short, concrete bullet]
• [Optional second bullet with one verified Paradigm company if it materially sharpens the lane]
• [Short, concrete bullet]

Stance on Prediction Markets
• [Short, concrete bullet]
• [Optional company/business example in bullet two if relevant]
• [Short, concrete bullet]

Stance on AI
• [Short, concrete bullet]
• [Optional company/business example in bullet two if relevant]
• [Short, concrete bullet]

Stance on Defense Tech
• [Short, concrete bullet]
• [Optional company/business example in bullet two if relevant]
• [Short, concrete bullet]

Stance on Robotics
• [Short, concrete bullet]
• [Optional company/business example in bullet two if relevant]
• [Short, concrete bullet]
```

**Section Selection Rules:**
- `Landscape Summary`, `Biography`, and `Goals` are the default anchor sections.
- Add topical sections only when the prompt, agenda, or office lane makes them useful.
- Do not force symmetry. A strong memo may have `Crypto Knowledge` and `Stance on Defense Tech` but no AI section, or AI and robotics but no prediction-markets section.
- If a section has nothing useful to say, omit it.

**Company-Reference Example:**
```text
Stance on Defense Tech
• Kennedy should be receptive to defense tech when it is framed around readiness, industrial capacity, and strategic competition, not startup culture.
• True Anomaly is a useful example. It just raised $650 million, is building spacecraft and autonomy software for contested-space missions, and is already tied to Space Force work.
• The strongest pitch is that the U.S. should make room for trusted new defense suppliers that can build strategically important systems at scale without lowering oversight.
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

# LegiStorm lookup
call legistorm staff '{"name":"[staffer_name]"}'
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
# Search bills via Congress.gov API
call congress bills '{"congress":119,"limit":50}'
call congress bill '{"congress":119,"type":"hr","number":4763,"detail":"summaries"}'

# Search hearings
call congress hearings '{"congress":119,"chamber":"senate"}'
call legistorm townhalls '{}'

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

# State-level via web search
web_search "state cryptocurrency legislation 2026"
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
2. Web search for Senator Lummis profile, recent statements, committee assignments
3. Search internal sources:
   ```bash
   call slack search_messages '{"query":"Lummis"}'
   call gsuite gmail_search '{"query":"Lummis"}'
   call paradigmdb notes_search '{"query":"Lummis"}'
   ```
4. Find current stablecoin legislation status
5. Check portfolio companies in stablecoin space
6. Generate briefer using template

---

## Future Integrations

| Integration | Purpose | Priority |
|-------------|---------|----------|
| Plural Policy API | Bill tracking, momentum scores | High |
| Shift direct integration | Portfolio cross-reference | Medium |
| Regulations.gov API | Docket comments, rulemaking tracking | Medium |
