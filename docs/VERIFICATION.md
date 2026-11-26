# Verification

## Live local providers

Two disposable Codex threads and two disposable Claude Code threads each received a distinct randomly generated marker. Thread Atlas submitted a follow-up through its authenticated HTTP send endpoint asking for the marker without repeating it. The test submitted the same draft ID twice in each case, waited for delivery, and counted the messages read back from the original transcript. All four originals contained exactly one follow-up, one matching answer, and an answered receipt. The proof file records the source ID hash, marker, original answer, and observed counts. [Live receipts](live-receipts.json)

## Browser integration

The automated browser run uses a real, isolated Chromium profile and the shipped Manifest V3 extension. ChatGPT-shaped and Claude-shaped pages are served by test fixtures. For each provider, a message sent in the dashboard appeared once in the source page, the fixture produced a distinct answer, the extension returned its receipt, and the source card updated. The test also exercised actual ZIP upload, read-only handoff, inbox readback, drag and drop, hiding, restoration after reload, language switching, unauthorized access rejection, and responsive layout at four viewport widths.

These fixtures demonstrate the bridge and dashboard workflow. They do not establish compatibility with a current signed-in production ChatGPT or Claude web page.

## Store and routing checks

The storage suite covers export deduplication and selected branches, account isolation, preservation of grouping and visibility, partial JSONL writes, unread states, and tab-claim idempotency. The routing suite uses 20 synthetic topics with overlapping vocabulary, 50 English and 50 Korean requests, and ten ambiguous requests per language. On this dataset all 100 topic-bearing requests selected the correct thread and all 20 ambiguous requests prompted for a choice. This is a regression fixture, not a real-world routing accuracy estimate.

Run the commands in the [README](../README.md) to reproduce the checks.
