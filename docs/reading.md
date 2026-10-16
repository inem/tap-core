# Reading rules for status / doctor / where

These are the same cuts we used in client-usage. They are not a visual theme.
They decide **what is allowed to appear**, not how pretty it is.

## Three questions, three commands

| Command | Question |
|---|---|
| `status` | Is the pipe live right now? |
| `doctor` | What residue is blocking? |
| `where` | Where do I look? |

Do not answer another command's question inside this one.
`doctor` does not reprint `status`. `where` does not diagnose.

## Rules

1. **A column (or command) is one question.**
   Mixing "is it up?", "what's wrong?", and "where's the file?" in one list
   is an inventory, not a reading.

2. **Show a constraint only when it binds.**
   Cap, stale `ago`, pace arrows, a check line — silent until they change
   the next move. A green `probe passed` is cents after the decimal.

3. **Do not reuse a glyph for a different semantics.**
   Quota bar is not a prepaid wallet. `waiting · last batch succeeded` is
   not the same as `capturing`. If the fact is another kind, it gets another
   block (and a cut before it).

4. **Identity travels with the question.**
   If a satellite would be anonymous, repeat the name there. Backend version
   is identity, not a health check.

5. **Round to the grain of the decision.**
   Dollars, not cents. `present`, not `present · regular`. Human time.

6. **Same state, same weight.**
   If the left says this thing is exhausted, the satellite is gray too.
   If doctor is healthy, do not shout the same greens again.

7. **Encode a rate in the unit already on screen.**
   Not `23.78 pp/h` — N squares of the same bar. Jargon that needs a legend
   is a failed fold.

## First cuts

- `doctor`: diagnosis only. Checks appear when they are not the boring good.
- `status`: pipe vs work stay distinguishable (runtime/routing/capture vs
  pack rows). Pack `waiting` after success is a zero-window — later.
- `where`: path is the fact; `present · regular` is chrome — later.
