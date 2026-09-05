# Multi-Turn Conversation Implementation Status

The native multi-turn implementation is available on `main`, but the complete
architecture described in `MULTI_TURN_CONVERSATION_PLAN.md` is not yet finished.

## Implemented

- Native Gemini continuation state using `cid`, `rid`, `rcid`, and the context token
- Continuation payload generation in Gemini payload slot `2`
- Continuation extraction from Gemini response frames
- Durable SQLite conversation turn storage
- `previous_response_id` support for `/v1/responses`
- Explicit conversation IDs through `metadata.conversation_id` and `X-Gemini-Conversation-ID`
- Explicit new-chat controls through `metadata.new_conversation` and `X-Gemini-New-Chat: true`
- Exact Chat Completions history-prefix reconciliation
- Client namespace isolation using explicit user or chat identifiers
- Conversation branching from earlier Responses turns
- Configurable state TTL and conversation count cleanup
- Conversation ID fields and headers in successful OpenAI-compatible responses
- State-expiration errors for unknown or expired `previous_response_id` values
- Full-history fallback for some Chat Completions continuation failures
- Persistent storage support through a mounted `/data` directory

## Remaining limitations

### Streaming

Stateful requests currently buffer the complete Gemini response with
`generate_turn()` and then emit the result downstream. Native continuation is
not yet captured incrementally from `generate_stream()`. Pending, completed,
and aborted turn states are not yet tracked separately, and client disconnect
handling does not yet provide the complete planned lifecycle.

### Google-compatible endpoints

The OpenAI Chat Completions and Responses endpoints support conversation state.
The Google-compatible `/v1beta` generation endpoints still operate statelessly
and do not yet accept or return conversation IDs.

### Generated-image conversations

Generated-image continuation metadata is parsed, but image-generation handlers
do not yet persist or resume that state. Follow-up questions in an ordinary
text conversation can use native state when the request is otherwise matched,
but the image-generation endpoints themselves do not yet expose conversation
continuation.

### Context fingerprints

The database schema has fields for system and tool fingerprints, but those
fields are not populated or checked. A material change to tool definitions does
not yet force a new Gemini conversation. System messages participate in full
transcript matching, but are not stored as a separate context fingerprint.

The account fingerprint currently uses the configured conversation account ID
and `auth_user`. It does not detect every possible Google account or cookie
session change automatically.

### Concurrency and retention

Conversation persistence uses process-local locks around writes and prevents a
stale request from silently moving a branch onto a newer head. Full
resolution-to-generation serialization is not yet implemented for every
explicit conversation-ID request. The configured per-conversation turn limit
is not yet enforced.

### Recovery and idempotency

Continuation recovery is not yet complete for every endpoint. Responses requests
with only an expired `previous_response_id` return an explicit state error, but
there is no general full-history recovery path for every request shape. There
is also no `Idempotency-Key` support, so client retries can create duplicate
turns.

### Conversation management

There are no conversation list, retrieve, delete, manual-expiration, or branch
inspection endpoints. Incremental-only requests do not retain a complete
plaintext transcript for arbitrary reconstruction after continuation expiry.

### Client integration and validation

The bridge accepts explicit user/chat namespace values, but the deployed
Open WebUI and Bifrost path has not yet been verified to forward a stable chat
identifier automatically. Model switching, reasoning switching, image
follow-ups, concurrent branches, continuation expiry, and restart persistence
still require live acceptance testing against the deployed stack.

The feature is implemented and tested locally, but the current multi-turn
changes have not been deployed to production.
