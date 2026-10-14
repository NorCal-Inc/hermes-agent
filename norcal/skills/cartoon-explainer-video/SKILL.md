---
name: cartoon-explainer-video
description: Use when making a cartoon-Erika explainer video (vertical, lip-synced cartoon presenter, scene art, captions) for your own company. Use shared tools to write, render and check an assigned video in your own video-studio.
version: 2.1.0
metadata:
  hermes:
    tags: [video, cartoon, erika, explainer, content]
---

# Cartoon-Erika explainer video

Christopher, 2026-10-13: "have the agents do the work and you run it thru higgsfield and elevenlabs and hand it back in a shared folder".
Christopher, 2026-10-14: shared tools, skills and logins are for every agent. Use the existing renderer in your own space. The existing profile loader supplies shared media credentials. Do not wait for Claude or copy his profile.

**Your studio folder:** `~/.hermes/profiles/<your profile>/video-studio/` (absolute path: `/home/chris/.hermes/profiles/<your profile>/video-studio/`). An example is in the `example-*` folder there: copy its format exactly.

## Hard rules

- **Your own channel's content only.** Facts come from a named public source you read the same day (a company's live site when the video is about a company). Never use company-private material (vault company folders, internal notes, credentials, customer data); a company video belongs to that company's Team Leader, not to you.
- **Every claim must appear in the cited source.** No invented features, prices, numbers or testimonials. Erika speaks as a presenter, never from personal experience.
- **Team avatars:** only use a team member's face (`cast.json` "face") once Christopher has approved that avatar in `~/.hermes/companies/entity-registry.md` § Avatars.
- **Nothing is posted, uploaded or shared** without Christopher's yes on the finished video. The on-screen "AI PRESENTER" tag stays; captions open with "Erika is an AI presenter".
- **Limits:** at most 8 scenes; at most 950 spoken characters (about 35-60 seconds). The render refuses anything bigger.

## Part 1 — write it (your card)

1. Make `video-studio/<company>-NN/` (NN = next number).
2. Read your source today. Write `claims.txt`: one line per claim you will say, followed by the exact source wording and its URL.
3. Write `script.txt`. Markup: `{scene:key|headline line 1|headline line 2}` starts a scene; `{pose:name}` changes Erika's pose; both go right before the word they fire on. Poses: `erika-base-wide`, `pose-point-viewer`, `pose-point-side`, `pose-point-up`, `pose-hands-up`, `pose-hands-hips`, `pose-explain`. Headlines short (about 18 characters per line). Spell the web address as words ("yourcompany dot com").
4. Write `scenes.txt`: one line per scene, `key|picture description`. Describe objects and settings only: the art style is added automatically and forbids text, letters, numbers, logos and faces, so do not ask for them.
5. Optional `cast.json`: names that get an on-screen badge when spoken. Copy the example (Erika as "AI presenter").
6. Run /home/chris/.local/bin/cartoon-studio-render with the absolute own video-studio folder through terminal after the existing profile credential loader. Attach the video, sheet and RESULT.txt. Reuse existing scene/voice files. Report actual credits/characters from RESULT.txt. On failure read the render log and check the sanctioned loader before declaring access missing. Execution still requires independent review; public posting requires Christopher approval.

## Part 2 — check it (after rendering)

The renderer writes `<folder>.mp4`, `sheet.jpg` (a contact sheet) and `RESULT.txt` (length, size, credits spent) into the same folder.

7. Look at `sheet.jpg`: every scene appears, no headline cut off, captions inside the frame, AI PRESENTER tag visible. Check the video against `claims.txt`.
8. Comment on your card: pass or the exact fixes needed (script lines or scene descriptions to change), plus a proposed caption that opens with "Erika is an AI presenter". Christopher approves before any post.
