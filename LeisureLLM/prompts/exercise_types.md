# Persona Meeting Exercises

## Philosophy

Meetings should be **exercises**, not discussions. An exercise has:
1. **Assigned roles** — personas have specific jobs, not just personalities
2. **Structural friction** — the format itself forces disagreement/exploration  
3. **Required artifact** — the output is a *thing*, not "takeaways"
4. **Clear end condition** — we know when we're done

## Trigger Hierarchy

Exercises don't run on timers. They run when there's a **reason**:

### 1. Partner Request (highest priority)
A human submitted an agenda item via `/agenda add`. Always honor this first.

### 2. Knowledge Tension (primary source)
The Librarian/Steward scours the actual knowledge base and finds something worth discussing:
- An unresolved question from a past conversation
- Two documents that contradict each other
- A commitment or deadline that may have passed
- A technical claim without evidence
- A recurring concern that hasn't been addressed

This is the organic heart of the system. The LLM reads the docs and notices things.

### 3. Database Events (fallback)
If no knowledge tension is found, check for mechanical triggers:
- Project recently completed → Case Study
- Task overdue 7+ days → Pre-Mortem
- Lead going cold → Client Roleplay
- Too many open tasks → Prioritization

### 4. Nothing (correct outcome)
If none of the above apply, **no exercise runs**. This is good.
Silence means the system has nothing worth saying.

## Quality Bar for Knowledge Tensions

Not every finding warrants an exercise. The system rejects:
- Generic observations ("we should document more")
- Process meta-talk ("we need better tracking")
- Things clearly already resolved
- Vague concerns without specific grounding

A worthy tension is **specific** and **actionable**:
- "The October client call mentioned a follow-up demo. Did that happen?"
- "The spec says we support 4K, but the test doc only shows 1080p results."
- "Ben asked about Arduino integration 3 times. No one answered."

## Exercise Types

### 1. Devil's Advocate (`devils_advocate`)
**Purpose:** Stress-test an idea by forcing genuine opposition.

**Structure:**
- One persona is assigned ADVOCATE (must defend the idea)
- One persona is assigned CRITIC (must find flaws)
- Others are JURY (ask clarifying questions, then vote)

**Artifact:** Decision — GO / NO-GO / NEEDS MORE INFO (with specific info needed)

**Triggers:** New proposal, new lead, scope change request

---

### 2. Pre-Mortem (`pre_mortem`)
**Purpose:** Imagine the project failed, work backwards to find risks.

**Structure:**
- FACILITATOR announces: "It's 6 months from now. [Project] failed catastrophically. Why?"
- Each persona must name ONE specific failure mode from their domain
- Group votes on most likely failures
- Assign mitigations

**Artifact:** Risk register with owner + mitigation for top 3 risks

**Triggers:** Project kickoff, major milestone, contract signing

---

### 3. Case Study Dissection (`case_study`)
**Purpose:** Learn from a real past project — what worked, what didn't.

**Structure:**
- HISTORIAN (Librarian) presents the facts from docs
- Each persona asks ONE probing question from their perspective
- Group identifies: 1 thing to repeat, 1 thing to never do again

**Artifact:** Lessons learned brief (2-3 bullets, actionable)

**Triggers:** Project completion, anniversary of past project, similar new opportunity

---

### 4. Constraint Storm (`constraint_storm`)
**Purpose:** Force creative solutions by imposing artificial constraints.

**Structure:**
- FACILITATOR announces a constraint: "We have $5k budget" or "We have 2 weeks" or "No custom code"
- Each persona proposes ONE solution that respects the constraint
- Group picks the most promising approach

**Artifact:** Approach recommendation with constraints explicitly stated

**Triggers:** Budget discussions, timeline pressure, resource constraints

---

### 5. Client Roleplay (`client_roleplay`)
**Purpose:** Prepare for real client interactions by simulating them.

**Structure:**
- One persona plays THE CLIENT (skeptical, budget-conscious, has hidden concerns)
- Others must pitch, answer questions, handle objections
- CLIENT reveals their "hidden concern" at the end — did the team address it?

**Artifact:** Pitch talking points or objection-handling notes

**Triggers:** Upcoming client call, proposal submission, RFP response

---

### 6. Proof Point Sprint (`proof_point`)
**Purpose:** Build credibility materials by extracting compelling evidence.

**Structure:**
- FACILITATOR names a claim we want to make ("We deliver reliable systems")
- Each persona must find ONE piece of evidence from docs/projects
- Group assembles into a proof point (stat, quote, case reference)

**Artifact:** One proof point ready for proposals/marketing

**Triggers:** Marketing push, RFP prep, website update

---

### 7. Technical Spike (`technical_spike`)
**Purpose:** Investigate a specific technical question with research.

**Structure:**
- FACILITATOR poses a concrete question ("Can we do X with Y?")
- Scout does live web research
- Dreamer proposes approaches
- Steward identifies risks/unknowns
- Group converges on: YES (here's how) / NO (here's why) / MAYBE (here's what we need to test)

**Artifact:** Technical recommendation with confidence level

**Triggers:** Technical question from partner, new capability request, tool evaluation

---

### 8. Prioritization Poker (`prioritization`)
**Purpose:** Force trade-off decisions when everything feels urgent.

**Structure:**
- FACILITATOR presents 3-5 items competing for attention
- Each persona argues for their top pick (30 seconds each)
- Forced rank: "If you could only do ONE, which?"
- Document the decision and the reasoning

**Artifact:** Prioritized list with rationale

**Triggers:** Too many open tasks, sprint planning, resource conflict

---

## Anti-Patterns (NEVER DO)

- **"General discussion"** — no structure = no friction = filler
- **"Status updates"** — that's what the database is for
- **"Brainstorming without constraints"** — generates noise, not signal
- **"Let's align on..."** — align on WHAT? Be specific or don't meet.
- **Hourly/daily schedules** — time-based triggers produce filler meetings

## What "Good" Looks Like

A good exercise:
1. Was triggered by something **specific** (a doc, an event, a partner request)
2. Had **assigned roles** that created friction (not just "discuss")
3. Produced an **artifact** (decision, recommendation, proof point)
4. Took **10-15 exchanges** max (quality over quantity)
5. Ended with a **clear next step** or conclusion

A good week might have 2-3 exercises. A busy week might have 5.
A week with nothing to discuss should have **zero exercises**.
