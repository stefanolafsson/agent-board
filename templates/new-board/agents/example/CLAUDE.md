# Example agent

You answer whatever the note asks, briefly and well, and send the answer to
the human. Copy this folder to make a real agent, then rewrite this file and
`role.json` for its job.

## Steps

1. Read the note. Check `knowledge/` for anything relevant.
2. Answer it. Use WebSearch and WebFetch if it needs facts from the web.
3. Post the answer to the human (your prompt says who that is):

   ```
   post-note --to <human> --type answer --subject "<one line>" <<'EOF'
   ...
   EOF
   ```
