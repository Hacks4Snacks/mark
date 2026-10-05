# Model Pricing Audit

Registry revision: `2026-09-24.1`  
Last verified: `2026-09-24`

This is a review queue, not an automatic price update. Verify every change against the linked official provider page before editing the registry.

## Official Source Changes

- [anthropic](https://platform.claude.com/docs/en/about-claude/pricing) changed (`eb4e72fa122fe5af25baf62e6e1c2c1f8383183ceadf97e44164205d3dc61e3a` -> `f1011ae54cc6258c9c102e09569d2a9dda15d745e12763bfc7903d1c40eb6a92`)
- [google](https://ai.google.dev/gemini-api/docs/pricing) changed (`17eaed895004fb366b69953d17c2df4ce884f1abcd7bd30e76f436ececb8770f` -> `b5f1a0b539ce608ad5f76f5293a3be448ff0c7f651ddd5a0726e622acfdb49d8`)
- [openai](https://developers.openai.com/api/docs/pricing) changed (`d11b31e190f7a155bac9cc7d48d0ff5611ef6655bcd2aaf7411e41c72021b56f` -> `ce6f7fc0f80dc0ec7db7a8a0ed3c8e12d63f37646146253db99f6574a28b252a`)

## Price Conflicts

None.

## Tracked Models Missing Upstream

- `composer-2-5-fast` was not found in LiteLLM provider `cursor`
- `composer-2-5` was not found in LiteLLM provider `cursor`

## Unverifiable Tracked Prices

- `chat-latest` `cache_write_5m` is unverifiable because LiteLLM `chat-latest` omits `cache_creation_input_token_cost`
- `gemini-2-5-flash-lite` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-2.5-flash-lite` omits `cache_creation_input_token_cost`
- `gemini-2-5-flash` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-2.5-flash` omits `cache_creation_input_token_cost`
- `gemini-2-5-pro` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-2.5-pro` omits `cache_creation_input_token_cost`
- `gemini-3-1-flash-lite` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3.1-flash-lite` omits `cache_creation_input_token_cost`
- `gemini-3-1-pro` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3.1-pro-preview` omits `cache_creation_input_token_cost`
- `gemini-3-5-flash-lite` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3.5-flash-lite` omits `cache_creation_input_token_cost`
- `gemini-3-5-flash` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3.5-flash` omits `cache_creation_input_token_cost`
- `gemini-3-6-flash` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3.6-flash` omits `cache_creation_input_token_cost`
- `gemini-3-7-flash` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3.7-flash` omits `cache_creation_input_token_cost`
- `gemini-3-8-flash` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3.8-flash` omits `cache_creation_input_token_cost`
- `gemini-3-flash` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3-flash-preview` omits `cache_creation_input_token_cost`
- `gpt-4-1-mini` `cache_write_5m` is unverifiable because LiteLLM `gpt-4.1-mini` omits `cache_creation_input_token_cost`
- `gpt-4.1` `cache_write_5m` is unverifiable because LiteLLM `gpt-4.1` omits `cache_creation_input_token_cost`
- `gpt-4o-mini` `cache_write_5m` is unverifiable because LiteLLM `gpt-4o-mini` omits `cache_creation_input_token_cost`
- `gpt-4o` `cache_write_5m` is unverifiable because LiteLLM `gpt-4o` omits `cache_creation_input_token_cost`
- `gpt-5-1` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.1` omits `cache_creation_input_token_cost`
- `gpt-5-2-pro` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.2-pro` omits `cache_creation_input_token_cost`
- `gpt-5-2-pro` `cached_input` is unverifiable because LiteLLM `gpt-5.2-pro` omits `cache_read_input_token_cost`
- `gpt-5-2` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.2` omits `cache_creation_input_token_cost`
- `gpt-5-3-codex` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.3-codex` omits `cache_creation_input_token_cost`
- `gpt-5-4-mini` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.4-mini` omits `cache_creation_input_token_cost`
- `gpt-5-4-nano` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.4-nano` omits `cache_creation_input_token_cost`
- `gpt-5-4` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.4` omits `cache_creation_input_token_cost`
- `gpt-5-5-cyber` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.5-cyber` omits `cache_creation_input_token_cost`
- `gpt-5-5` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.5` omits `cache_creation_input_token_cost`
- `grok-4-20-multi-agent-0309` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-4.20-multi-agent` omits `cache_creation_input_token_cost`
- `grok-4-20` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-4.20` omits `cache_creation_input_token_cost`
- `grok-4-3` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-4.3` omits `cache_creation_input_token_cost`
- `grok-4-5` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-4.5` omits `cache_creation_input_token_cost`
- `grok-4-6` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-4.6` omits `cache_creation_input_token_cost`
- `grok-4-7` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-4.7` omits `cache_creation_input_token_cost`
- `grok-build-0-1` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-build-0.1` omits `cache_creation_input_token_cost`

## New Model Candidates

- `claude-mythos-preview` appears in LiteLLM provider `anthropic` but is not in the registry (anthropic)
- `claude-sonnet-5-5` appears in LiteLLM provider `anthropic` but is not in the registry (anthropic)
- `gemini-2.5-computer-use-preview-10-2025` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-exp-1114` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-exp-1206` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-flash-latest` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-flash-lite-latest` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-gemma-2-27b-it` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-gemma-2-9b-it` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-pro-latest` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gpt-5-chat` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `gpt-6.1-sol` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `grok-4.20-beta-latest-non-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-beta-latest-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-beta-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-beta-non-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-beta-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-experimental-beta-0304-non-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-experimental-beta-0304-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-experimental-beta-0304` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-experimental-beta-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-experimental-beta-non-reasoning-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-experimental-beta-reasoning-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-multi-agent-experimental-beta-0304` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-multi-agent-experimental-beta-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-non-reasoning-gv2` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-reasoning-gv2` appears in LiteLLM provider `xai` but is not in the registry (xai)
