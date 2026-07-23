# Model Pricing Audit

Registry revision: `2026-07-14.1`  
Last verified: `2026-07-14`

This is a review queue, not an automatic price update. Verify every change against the linked official provider page before editing the registry.

## Official Source Changes

- [xai](https://docs.x.ai/developers/models) changed (`ad2e1647c8d7329110f5b339c9212749ad26b546b3d5b3fb91c31843e5e8f2b6` -> `d03da18da61d291d8c50daecbaccf84bb39f62de4dc3f2f2012572d46846bfcb`)

## Price Conflicts

None.

## Tracked Models Missing Upstream

None.

## Unverifiable Tracked Prices

- `gemini-2-5-flash-lite` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-2.5-flash-lite` omits `cache_creation_input_token_cost`
- `gemini-2-5-flash` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-2.5-flash` omits `cache_creation_input_token_cost`
- `gemini-2-5-pro` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-2.5-pro` omits `cache_creation_input_token_cost`
- `gemini-3-1-flash-lite` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3.1-flash-lite` omits `cache_creation_input_token_cost`
- `gemini-3-1-pro` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3.1-pro-preview` omits `cache_creation_input_token_cost`
- `gemini-3-5-flash` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3.5-flash` omits `cache_creation_input_token_cost`
- `gemini-3-flash` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3-flash-preview` omits `cache_creation_input_token_cost`
- `gemini-3-pro` `cache_write_5m` is unverifiable because LiteLLM `gemini/gemini-3-pro-preview` omits `cache_creation_input_token_cost`
- `gpt-4-1-mini` `cache_write_5m` is unverifiable because LiteLLM `gpt-4.1-mini` omits `cache_creation_input_token_cost`
- `gpt-4-1-nano` `cache_write_5m` is unverifiable because LiteLLM `gpt-4.1-nano` omits `cache_creation_input_token_cost`
- `gpt-4.1` `cache_write_5m` is unverifiable because LiteLLM `gpt-4.1` omits `cache_creation_input_token_cost`
- `gpt-4o-mini` `cache_write_5m` is unverifiable because LiteLLM `gpt-4o-mini` omits `cache_creation_input_token_cost`
- `gpt-4o` `cache_write_5m` is unverifiable because LiteLLM `gpt-4o` omits `cache_creation_input_token_cost`
- `gpt-5-3-codex` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.3-codex` omits `cache_creation_input_token_cost`
- `gpt-5-4-mini` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.4-mini` omits `cache_creation_input_token_cost`
- `gpt-5-4-nano` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.4-nano` omits `cache_creation_input_token_cost`
- `gpt-5-4` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.4` omits `cache_creation_input_token_cost`
- `gpt-5-5` `cache_write_5m` is unverifiable because LiteLLM `gpt-5.5` omits `cache_creation_input_token_cost`
- `gpt-5-mini` `cache_write_5m` is unverifiable because LiteLLM `gpt-5-mini` omits `cache_creation_input_token_cost`
- `gpt-5-nano` `cache_write_5m` is unverifiable because LiteLLM `gpt-5-nano` omits `cache_creation_input_token_cost`
- `gpt-5` `cache_write_5m` is unverifiable because LiteLLM `gpt-5` omits `cache_creation_input_token_cost`
- `grok-4-20` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-4.20-0309-reasoning` omits `cache_creation_input_token_cost`
- `grok-4-3` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-4.3` omits `cache_creation_input_token_cost`
- `grok-4-5` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-4.5` omits `cache_creation_input_token_cost`
- `grok-code-fast` `cache_write_5m` is unverifiable because LiteLLM `xai/grok-code-fast` omits `cache_creation_input_token_cost`

## New Model Candidates

- `claude-3-7-sonnet-20250219` appears in LiteLLM provider `anthropic` but is not in the registry (anthropic)
- `claude-4-sonnet-20250514` appears in LiteLLM provider `anthropic` but is not in the registry (anthropic)
- `claude-sonnet-4-20250514` appears in LiteLLM provider `anthropic` but is not in the registry (anthropic)
- `gemini-2.5-computer-use-preview-10-2025` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-exp-1114` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-exp-1206` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-flash-latest` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-flash-lite-latest` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-gemma-2-27b-it` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-gemma-2-9b-it` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gemini-pro-latest` appears in LiteLLM provider `gemini` but is not in the registry (google)
- `gpt-5-chat-latest` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `gpt-5-chat` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `gpt-5-codex` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `gpt-5.1-codex-max` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `gpt-5.2-chat-latest` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `gpt-5.3-chat-latest` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `grok-2-1212` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-2-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-2` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-3-fast-beta` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-3-fast-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-3-mini-beta` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-3-mini-fast-beta` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-3-mini-fast-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-3-mini-fast` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-3-mini-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-3-mini` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4-1-fast-non-reasoning-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4-1-fast-non-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4-1-fast-reasoning-latest` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4-1-fast-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4-1-fast` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4-fast-non-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4-fast-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-beta-0309-non-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-beta-0309-reasoning` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-4.20-multi-agent-beta-0309` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-code-fast-1-0825` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `grok-code-fast-1` appears in LiteLLM provider `xai` but is not in the registry (xai)
- `o1-pro-2025-03-19` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `o1-pro` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `o3-pro-2025-06-10` appears in LiteLLM provider `openai` but is not in the registry (openai)
- `o3-pro` appears in LiteLLM provider `openai` but is not in the registry (openai)
