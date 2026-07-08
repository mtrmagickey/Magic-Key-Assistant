# Getting Started

Magic Key Assistant works best when you use it to keep real work from getting lost.

This guide is intentionally hands-on. Do the steps as you read them.

If you have 15 minutes, you can go from a fresh install to a working continuity workspace in one sitting.

---

## Start Here

After [installation](INSTALLATION.md), start the app:

- **If you downloaded the release exe:** double-click `MagicKey-Beta-Release-1.0.exe`.
- **If you're running from source:** run `python launcher.py`.

Either way, the app opens the web console for you. If it doesn't, browse to `http://localhost:8000`.

Your goal for the first session is not to configure everything. Your goal is to prove four things:

1. You can open the app.
2. You can ask one real question.
3. You can capture one action and one decision.
4. You can see what needs review next.

---

## 15-Minute First Run

### Step 1: Finish setup fast

In the setup wizard:

1. Pick the job you want help with first.
2. Stay on the local path if it is available.
3. Leave the sample workspace turned on if you want to learn by example.
4. Skip cloud keys unless you already know you need them.

What success looks like:

- Setup finishes.
- You land in a workspace that is not empty.
- You understand whether you are starting local-only or cloud-assisted.

### Step 2: Look at the dashboard for one minute

Do not click everything yet.

Just answer these questions:

1. What looks overdue?
2. What has no owner?
3. What still needs a decision?

If you turned on the sample workspace, the dashboard should already answer those questions.

### Step 3: Ask one real question

Open Conversations and ask something concrete.

Good first questions:

- `What in this workspace looks most at risk right now?`
- `Which actions still do not have an owner?`
- `What decisions need review this week?`

Avoid vague first questions like `What can you do?`

What success looks like:

- You get a specific answer.
- The answer points to actual work, not generic advice.

### Step 4: Capture one action

Open Actions and add one task that is real, current, and small.

Good examples:

- `Confirm who is opening the building on Saturday`
- `Update next week's swim class notice`
- `Check whether the front desk handoff note is complete`

For the first action, always do these two things:

1. Add an owner if you know it.
2. Add a due date if the timing matters.

What success looks like:

- The action is visible.
- It is not anonymous.
- It has a next step, not just a topic.

### Step 5: Capture one decision

Open Teach or use chat to record one operating rule or decision.

Good examples:

- `Urgent facility issues must be reviewed before close.`
- `Friday is our follow-through review point.`
- `Customer-facing schedule changes must be posted by Thursday afternoon.`

What success looks like:

- The decision has both the rule and the reason.
- Someone returning later could understand why it exists.

### Step 6: Add one real document

Open Knowledge and add one file you actually use.

Good first documents:

- a handoff note
- a standard operating procedure
- a shift checklist
- a schedule or announcement draft

Do not start with a huge mixed folder. Start with one file you understand well.

What success looks like:

- The file is indexed.
- You can ask a question against it.

### Step 7: Check the review loop

Before you stop, confirm that the system can now show reviewable work.

Look for:

- overdue items
- unowned work
- unresolved decisions
- gaps in knowledge

If the workspace feels too empty, add one more action and re-ask your earlier question.

---

## Your First Three Sessions

## Session 1: Prove the basics

Do this:

1. Finish setup.
2. Ask one real question.
3. Capture one action.
4. Capture one decision.
5. Add one document.

By the end of Session 1, you should trust that the workspace can hold real operating context.

## Session 2: Improve one answer

Do this:

1. Ask 3 real questions tied to work in flight.
2. Open Gaps.
3. Resolve one missing-context area by adding a note or document.
4. Ask the same question again.

By the end of Session 2, you should see that the system improves when you feed it real context.

## Session 3: Build a repeatable rhythm

Do this:

1. Review what is overdue.
2. Assign any unowned work.
3. Re-check unresolved decisions.
4. Close or re-date anything stale.

By the end of Session 3, the product should feel like part of your operating rhythm, not just a demo.

---

## Best First Inputs

If you are not sure what to add first, use one item from each box below.

### Add one note

- today's handoff note
- a short incident summary
- a shift checklist

### Add one action

- a task with a real owner
- a due date that matters this week
- a follow-up that tends to get dropped

### Add one decision

- a rule your team keeps repeating verbally
- a review cadence you want people to follow
- a threshold for escalation

### Ask one question

- `What is most likely to slip this week?`
- `What still has no owner?`
- `What do we keep needing to revisit?`

---

## What To Ignore On Day One

Do not try to tune everything immediately.

You can safely ignore these on the first pass:

- model routing details
- automation tuning
- Discord setup
- advanced retrieval debugging
- deeper settings cleanup

Come back to those after the workspace already contains real actions, decisions, and documents.

---

## Common Early Mistakes

| Mistake | Better move |
|---|---|
| Asking broad generic questions | Ask about one live piece of work |
| Adding documents with no current value | Add one file you need this week |
| Capturing vague actions | Write the next concrete step |
| Leaving everything unowned | Name an owner or explicitly mark it unowned |
| Trying to configure every page | Finish the first-run loop first |

---

## If The Sample Workspace Feels Wrong

That is normal.

The sample workspace is there to show structure, not to match your exact world.

If it helps, keep it for one session.

If it distracts you, switch quickly to your own material:

1. Add one real note.
2. Add one real action.
3. Add one real decision.
4. Re-ask your earlier question.

The product becomes clearer as soon as the examples are yours.

---

## Troubleshooting

| Issue | What to do |
|---|---|
| Admin console will not start | Verify Python 3.12+ and run `pip install -r LeisureLLM/requirements.txt` |
| Answers are weak | Add one document and one decision, then ask again |
| The workspace feels empty | Add one action and one note before judging it |
| Local inference is not ready | Finish setup anyway and add cloud access later only if needed |
| GUI is slow or odd-looking | Restart the launcher once after setup completes |

Logs:

```powershell
Get-Content leisurellm.log -Tail 100
```

---

## What "Working" Looks Like

By the end of your first few sessions, the workspace should give you all of these:

- at least one useful answer grounded in your own context
- at least one action with an owner and due date
- at least one written decision with rationale
- at least one visible review item

That is the point where the product is operational, not just installed.