Yes, this can be fixed properly. I captured Gemini Chat’s own two-turn behavior and confirmed native continuation works.

## What Gemini Chat actually does

For the first turn, Gemini receives an empty conversation descriptor in payload slot `2`.

After Gemini responds, the response contains four continuation values:

- `cid`: conversation ID
- `rid`: response ID
- `rcid`: response candidate ID
- `context_token`: opaque continuation token

The next UI request sends:

```python
[
    cid,
    rid,
    rcid,
    None,
    None,
    None,
    None,
    None,
    None,
    context_token,
]
```

in payload slot `2`.

The values can currently be extracted from Gemini’s response frames as follows:

- `cid`: response frame field `[1][0]`
- `rid`: response frame field `[1][1]`
- `rcid`: selected candidate field `[4][n][0]`
- `context_token`: response frame field `[2]["26"]`

I tested this directly:

1. First API request: “Remember the secret word marigold.”
2. Extracted all four continuation values.
3. Second API request contained only: “What secret word did I ask you to remember?”
4. The second request used the continuation descriptor rather than resending history.
5. Gemini answered `marigold`.

So native multi-turn conversations are feasible.

---

# Recommended architecture

The bridge should not use an LLM or fuzzy semantic classifier to decide whether two requests belong to the same conversation. That would occasionally connect unrelated users or chats.

Instead, it should use deterministic conversation reconciliation:

1. Explicit conversation references when available.
2. Standard Responses API `previous_response_id`.
3. Exact message-history matching within a safe client namespace.
4. Otherwise, start a new Gemini conversation.

This provides intelligent behavior without guessing.

---

## 1. Introduce a Gemini continuation object

Add a structure such as:

```python
@dataclass(frozen=True)
class GeminiContinuation:
    cid: str
    rid: str
    rcid: str
    context_token: str

    def payload_slot(self) -> list:
        return [
            self.cid,
            self.rid,
            self.rcid,
            None,
            None,
            None,
            None,
            None,
            None,
            self.context_token,
        ]
```

Change `_build_payload()` to accept:

```python
continuation: GeminiContinuation | None = None
```

For a new conversation:

```python
inner[2] = ["", "", "", None, None, None, None, None, None, ""]
```

For a continuation:

```python
inner[2] = continuation.payload_slot()
```

---

## 2. Parse conversation metadata from every response

The existing response parser extracts text and some image metadata but discards native conversation state.

Introduce a general response type:

```python
@dataclass
class GeminiTurnResult:
    text: str
    images: list[GeneratedImage]
    continuation: GeminiContinuation | None
    raw: str
```

Both text and image generation should use the same frame parser.

The parser should collect the final complete set of:

```text
cid
rid
rcid
context_token
```

A turn should only be committed to conversation storage if all required values were received and the upstream response completed successfully.

---

# Conversation resolution

## 3. Responses API behavior

The Responses API already has a suitable standard mechanism:

```json
{
  "model": "gemini-3.6-flash",
  "previous_response_id": "resp_abc123",
  "input": "Continue from before"
}
```

The bridge should map its generated `resp_...` response IDs to Gemini continuation states.

### First request

```json
{
  "input": "Remember that my favorite color is violet"
}
```

The bridge:

1. Starts a new Gemini conversation.
2. Stores the returned continuation state.
3. Associates it with the returned `resp_...` ID.

### Next request

```json
{
  "previous_response_id": "resp_abc123",
  "input": "What is my favorite color?"
}
```

The bridge:

1. Loads the exact continuation associated with `resp_abc123`.
2. Sends only the new input to Gemini.
3. Stores a new continuation under the newly generated response ID.

This naturally supports branching. Two requests can both use the same `previous_response_id`, producing two branches from the same Gemini turn.

---

## 4. Chat Completions behavior

Chat Completions does not have a standard `previous_response_id`. Clients normally resend the complete message list:

```json
{
  "messages": [
    {"role": "user", "content": "Remember violet"},
    {"role": "assistant", "content": "Stored"},
    {"role": "user", "content": "What was the word?"}
  ]
}
```

The bridge should recognize the longest previously completed transcript prefix.

### Example

After the first response, the bridge records a fingerprint for:

```text
user: Remember violet
assistant: Stored
```

When the next request arrives, it fingerprints everything except the new user message:

```text
user: Remember violet
assistant: Stored
```

If this exactly matches a stored turn, the bridge:

1. Loads the continuation from that turn.
2. Sends only `What was the word?`.
3. Does not flatten the previous messages into the prompt again.

This makes Open WebUI work naturally because it already sends message history.

---

# Safe conversation identification

## 5. Explicit conversation ID

Support a bridge extension through either:

```http
X-Gemini-Conversation-ID: conv_<random-token>
```

or:

```json
{
  "metadata": {
    "conversation_id": "conv_<random-token>"
  }
}
```

The bridge should return the ID in both the response and response headers:

```http
X-Gemini-Conversation-ID: conv_<random-token>
```

```json
{
  "conversation_id": "conv_<random-token>"
}
```

Conversation IDs should be random capability tokens containing at least 128 bits of entropy, preferably 256 bits.

### Explicit new-chat control

Also support:

```http
X-Gemini-New-Chat: true
```

or:

```json
{
  "metadata": {
    "new_conversation": true
  }
}
```

This gives integrations an unambiguous reset mechanism.

---

## 6. Namespace isolation

Transcript matching must never happen globally.

Suppose two users both send:

```text
Hello
```

and receive the same answer. A global transcript lookup could attach one user to the other user’s Gemini conversation.

Conversation lookups must therefore be namespaced by one of:

1. An unguessable explicit conversation ID.
2. A trusted user/tenant identity forwarded by Bifrost.
3. The OpenAI `user` parameter, where deployments trust it.
4. An Open WebUI chat identifier from request metadata.

Do not use source IP addresses. Reverse proxies, NAT, mobile clients, and shared networks make IP-based session identity unreliable.

If no safe namespace or explicit conversation reference exists, the bridge should remain stateless and start a new Gemini chat.

---

# Exact decision algorithm

For each request:

## Step 1: Explicit reset

If the client requests a new conversation:

```text
start new Gemini chat
```

## Step 2: Responses API parent

If `previous_response_id` is present:

- If found, continue exactly from that stored turn.
- If unknown or expired, return a state-expired error unless full history is available.
- Do not silently continue from some unrelated conversation.

## Step 3: Explicit conversation ID

If a conversation ID is present:

- If the request includes only a new message, continue from that conversation’s current head.
- If it includes complete history, reconcile it against the stored turn tree.
- If it matches an older turn, create a branch from that turn.
- If the history conflicts with all known turns, begin a new Gemini root for that local conversation or return a conflict in strict mode.

## Step 4: Full-history reconciliation

If there is a safe client namespace:

1. Normalize the message history.
2. Exclude the final new user/tool input.
3. Calculate the transcript-prefix hash.
4. Look up the exact matching turn.
5. Continue from that turn if found.

## Step 5: Stateless fallback

If nothing reliable matches:

- Start a new Gemini conversation.
- Preserve the current behavior by including the complete supplied history in the initial prompt.

This means clients continue to work even if they do not know about conversation state.

---

# When to force a new Gemini chat

The bridge should start a new upstream chat when:

- The client explicitly requests it.
- No continuation state exists.
- The continuation state expired.
- The Gemini account or primary session changed.
- `auth_user` changed.
- The system/developer instruction changed.
- The tool definition set changed materially.
- The request history does not match any stored ancestor.
- The persistent-versus-temporary chat mode changed.
- Gemini rejects the continuation and full history is available for safe reconstruction.

## Things that should not automatically force a new chat

These should normally be allowed inside an existing conversation:

- Switching from normal reasoning to Extended thinking.
- Switching between Flash-Lite, Flash, and Pro.
- Attaching a new image.
- A tool result following an assistant tool call.
- Regenerating or branching from a stored earlier turn.

Gemini Chat itself permits per-turn mode selection, but model and reasoning switching should be live-tested during implementation.

---

# Turn tree rather than one mutable session

Do not store only one current conversation head.

Use a turn tree:

```text
turn 1
├── turn 2
│   └── turn 3
└── alternate turn 2
```

Each completed turn should contain its own continuation snapshot.

A possible schema:

```sql
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    namespace_hash TEXT NOT NULL,
    current_head_id TEXT,
    account_fingerprint TEXT NOT NULL,
    system_fingerprint TEXT,
    tools_fingerprint TEXT,
    temporary_chat INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE conversation_turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    parent_turn_id TEXT,
    response_id TEXT UNIQUE,
    transcript_hash TEXT,
    request_hash TEXT,
    cid TEXT NOT NULL,
    rid TEXT NOT NULL,
    rcid TEXT NOT NULL,
    context_token TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
```

This supports:

- Edited messages
- Regeneration
- Responses API branching
- Open WebUI branch navigation
- Retry and idempotency handling
- Continuing from an older response

---

# Transcript normalization

Matching must be exact, not semantic.

Normalize only transport-level differences:

- Normalize `\r\n` to `\n`.
- Serialize message roles consistently.
- Canonically serialize tool arguments as JSON.
- Preserve content-part order.
- Hash image bytes or canonical image references.
- Include system/developer instructions.
- Include assistant tool calls and tool results.
- Include the tool schema fingerprint.

Avoid:

- Fuzzy text matching
- Embedding similarity
- LLM classification
- Whitespace-insensitive matching beyond line-ending normalization
- Matching across users merely because messages look similar

The bridge can store rolling SHA-256 transcript hashes rather than complete message plaintext.

---

# Incremental prompt construction

When a continuation matches, do not send the entire OpenAI history again.

Calculate the longest known prefix and render only the delta.

For example:

```text
Known upstream state:
  user question
  assistant tool call

New request contains:
  user question
  assistant tool call
  tool result
  user follow-up
```

Only send:

```text
[Tool result]: ...
[user follow-up]
```

Gemini already has the earlier turns in its native state.

If there is no matching state, send the complete flattened transcript as a new chat, preserving today’s compatibility behavior.

---

# Streaming behavior

Streaming needs careful state handling.

The bridge should:

1. Mark the turn as `pending`.
2. Stream text to the client while continuing to parse response frames.
3. Capture continuation fields from terminal frames.
4. Commit the turn only after the upstream response completes.
5. Atomically update the conversation head.
6. Mark incomplete or disconnected requests as `aborted`.

Do not advance a conversation using a partial response without a complete continuation token.

If the client disconnects, the implementation must decide whether to finish consuming the upstream response. The safest initial behavior is:

- Do not advance the conversation head if the upstream response was not completely consumed.
- Allow the client to retry from the previous completed turn.

---

# Concurrent requests

Use a lock or optimistic version check per conversation.

For the Responses API, `previous_response_id` identifies the exact parent, so concurrent requests naturally become separate branches.

For explicit conversation IDs without a parent ID:

- Serialize requests against the current head, or
- Require an expected parent-turn ID for concurrency-safe operation.

Never let a request resolve against one head and then silently append to a newer head that appeared while the request was executing.

---

# Persistence and expiration

Use a separate SQLite database, not Bifrost’s database:

```text
/data/conversations.db
```

Recommended properties:

- WAL mode
- File mode `0600`
- Parent directory mode `0700`
- Configurable TTL, initially seven days
- Cleanup job for expired turns and conversations
- Maximum conversation and turn counts
- LRU/oldest-first eviction
- No continuation values in logs

Suggested configuration:

```json
{
  "conversation_state_enabled": true,
  "conversation_store_path": "/data/conversations.db",
  "conversation_ttl_sec": 604800,
  "conversation_max_conversations": 10000,
  "conversation_max_turns_per_conversation": 200,
  "conversation_resolution_mode": "automatic",
  "conversation_fallback": "new_with_full_history"
}
```

Continuation values are not account cookies, but they should still be treated as sensitive context capabilities.

---

# Recovery behavior

A stored continuation may eventually be rejected by Gemini.

If the request contains complete history:

1. Mark the continuation invalid.
2. Start a new upstream conversation.
3. Send the full supplied history using the current stateless formatter.
4. Store the new continuation.
5. Return the response normally.

If the request contains only incremental input, such as a Responses request with an expired `previous_response_id`, the bridge cannot safely reconstruct context unless it stored the transcript.

In that case return something explicit, such as:

```json
{
  "error": {
    "code": "conversation_state_expired",
    "message": "The Gemini conversation state expired. Resend the complete history or start a new conversation."
  }
}
```

It must not silently answer without the missing context.

---

# Implementation phases

## Phase 1: Protocol primitives

Add:

- `GeminiContinuation`
- Continuation serialization into payload slot `2`
- Response extraction for `cid`, `rid`, `rcid`, and field `26`
- Structured non-streaming result
- Streaming continuation parser

Tests:

- Extract all four fields from captured frames.
- Build exact continuation slot.
- Two-turn native continuation.
- Missing or malformed metadata does not get stored.

## Phase 2: In-memory Responses support

Add:

- `previous_response_id`
- Response ID to continuation mapping
- Branching from any previous response
- Expiration and invalid-state errors

This is the cleanest endpoint to implement first because it has explicit parent semantics.

## Phase 3: Explicit Chat Completions sessions

Add:

- `metadata.conversation_id`
- `X-Gemini-Conversation-ID`
- New-chat control
- Conversation ID returned in response/header
- Per-conversation locks

## Phase 4: Open WebUI transcript reconciliation

Add:

- Canonical message hashing
- Longest-prefix matching
- Delta prompt generation
- Branch recognition
- Stateless full-history fallback

Before enabling this, capture an actual Open WebUI request and confirm whether it forwards `metadata.chat_id`, `user`, or another stable identifier through Bifrost.

## Phase 5: Durable SQLite storage

Add:

- Conversation and turn tables
- WAL and transactional commits
- TTL cleanup
- Account fingerprint invalidation
- Storage quotas
- Restart continuation tests

## Phase 6: Advanced cases

Validate:

- Tool-call loops
- Image follow-up questions
- Switching models inside a chat
- Switching reasoning effort
- Temporary chats
- Regeneration from an earlier turn
- Concurrent branches
- Client disconnects
- Gemini continuation expiry

---

# Required live acceptance tests

The feature should not be considered complete until all of these pass:

1. **Native memory**
   - First request stores a random word.
   - Second request sends only the new question.
   - Gemini remembers the word.

2. **Restart persistence**
   - Complete first turn.
   - Restart the bridge.
   - Continue the conversation successfully.

3. **Open WebUI**
   - Start a chat.
   - Send multiple turns.
   - Reload the page.
   - Continue without flattened-history duplication.

4. **Branching**
   - Continue twice from the same earlier turn.
   - Ensure each branch remains independent.

5. **Edited message**
   - Edit an earlier user message.
   - Continue from the matching ancestor or safely start a new Gemini chat.

6. **Namespace isolation**
   - Two clients submit identical transcripts.
   - Ensure their Gemini conversations never overlap.

7. **Model switching**
   - Start with Flash.
   - Continue with Pro.
   - Continue again with Flash-Lite.

8. **Reasoning switching**
   - Start with `low`.
   - Continue with `high`.
   - Verify Extended thinking changes without losing context.

9. **Vision continuation**
   - Upload an image.
   - Ask a second question without reuploading it.
   - Verify Gemini remembers the image.

10. **Expired state**
    - Invalidate a continuation.
    - Verify full-history fallback or an explicit state-expired error.

---

## Recommended initial behavior

I recommend these defaults:

- Native continuation enabled for `previous_response_id`.
- Native continuation enabled for explicit conversation IDs.
- Transcript reconciliation enabled only when a safe client namespace is available.
- Exact matching only.
- Full-history stateless fallback when no state matches.
- No semantic or fuzzy “same chat” guessing.
- SQLite persistence enabled with a seven-day TTL.
- Model and reasoning changes stay in the same chat.
- System instructions, tool schemas, account identity, and temporary-chat mode changes start a new upstream chat.

That gives us actual Gemini multi-turn state while retaining backward compatibility for clients that continue sending ordinary stateless requests.
