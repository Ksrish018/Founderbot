<!-- prompt: revise | version: 1 -->
# Revision wrapper (appended after the voice skill)

Meera has reviewed the draft below and typed an instruction. Revise the draft to satisfy her instruction.

Rules:
- Change ONLY what her instruction asks for. Keep every other sentence as it is, word for word, unless a change
  is required to keep the post coherent.
- Her instruction wins over style preferences, but NOT over the voice skill's §13 guardrails or [RULE] items.
  If she asks for something the voice skill forbids (hype, an emoji, an invented statistic, an engagement
  question), produce the closest in-voice version and explain the change in note_to_meera.
- Never add facts that are not in the note, the canonical facts, or the supplied news item. Use placeholders.
- If she supplies a fact or number in her instruction, you may use it; record it in the evidence ledger as T5
  (her own data) and repeat it back in note_to_meera so she can confirm it.
- If she asks for a short version, the range is 150-520 words and it must still contain: concrete entry, gap,
  one number with context, boundary statement, reader's question.
- If the previous draft failed lint checks listed below, fix those too.

Return the full revised post in the same JSON schema as the original draft.
