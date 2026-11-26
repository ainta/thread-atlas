# Verification

## Live local providers

Two disposable Codex threads and two disposable Claude Code threads each received a distinct randomly generated marker. Thread Atlas submitted a follow-up through its authenticated HTTP send endpoint asking for the marker without repeating it. The test submitted the same draft ID twice in each case, waited for delivery, and counted the messages read back from the original transcript. All four originals contained exactly one follow-up, one matching answer, and an answered receipt. The proof file records the source ID hash, marker, original answer, and observed counts. [Live receipts](live-receipts.json)

## Browser integration

The automated browser run uses a real, isolated Chromium profile and the shipped Manifest V3 extension. ChatGPT-shaped and Claude-shaped pages are served by test fixtures. For each provider, a message sent in the dashboard appeared once in the source page, the fixture produced a distinct answer, the extension returned its receipt, and the source card updated. The test also exercised actual ZIP upload, read-only handoff, inbox readback, drag and drop, hiding, restoration after reload, language switching, unauthorized access rejection, responsive layout at four viewport widths, collapsing and expanding a 103-session map, and a lookup answered from an older message whose **View source** opens the history at that message.

Read-only inspection of signed-in ChatGPT and Claude pages also identified visible message and composer elements. ChatGPT used newer response markup, which the extension and its test fixture now cover. Original-thread sends on the two production websites require a separately paired live test.

## Native package

The Apple Silicon menu-bar app launched the service on macOS. An independent cold start from its packaged launcher returned HTTP 200 for the dashboard and opened an isolated empty workspace. The bundle passed strict code-signature verification after executing, and its release ZIP passed archive integrity verification. The signature is ad hoc.

## Store and routing checks

Sixteen storage and delivery regression tests cover paginated alerts, restart recovery, repeated utterances across different export and DOM identifiers, and export deduplication and selected branches, account isolation, preservation of grouping and visibility, partial JSONL writes, unread states, and tab-claim idempotency. They also cover tool-call narration, unfamiliar transcript records, formatted exports matched to rendered pages, regenerated answers, older exports imported after a page read, busy and unacknowledged web sends, stale requests after a restart, and dispatch during a locked database. The routing suite uses 20 synthetic topics with overlapping vocabulary, 50 English and 50 Korean requests, and ten ambiguous requests per language. On this dataset all 100 topic-bearing requests selected the correct thread and all 20 ambiguous requests prompted for a choice. This is a regression fixture, not a real-world routing accuracy estimate. Six search tests run the collector process and query its search endpoint. They cover a match found only in an older message, an in-place edit, hidden sessions, the one-time migration of an unversioned database, inflected Korean forms, and a median search time below 100 ms over 100,000 messages.

Run the commands in the [README](../README.md) to reproduce the checks.
