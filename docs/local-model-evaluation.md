# Model evaluation for FilaBrain

Start with Groq Qwen 3.8 27B through OmniRoute for hosted chat and interactive research. Use `qwen3:4b-instruct` for basic local chat and reviewed research. The Guide includes installation and endpoint setup. Keep `llama3.1:8b` experimental for research until it passes the same connection and extraction checks.

## OmniRoute and local Qwen

Both endpoints received the same synthetic FilaBrain 0.1.19 prompts. The local Ollama server used an NVIDIA V100 with 32 GB of VRAM. The preview’s validators accepted all three responses on each route. Both recovered 9 of 11 expected specs with zero wrong values and showed both conflicting nozzle ranges.

| One test run | Groq via OmniRoute | Qwen 27B via Ollama |
| --- | ---: | ---: |
| Connection test | 0.5 s | 39.5 s |
| Spec extraction | 1.7 s | 33.9 s |
| Grounded chat | 0.8 s | 17.4 s |

Select the tested route `groq/qwen/qwen3.8-27b`. These timings reflect the tested setup and will vary with local hardware, model loading, and hosted usage. [OmniRoute](https://github.com/cheaper-inference/OmniRoute) · [Groq model](https://console.groq.com/docs/model/qwen/qwen3.8-27b)

## What we tested locally

We ran the installed FilaBrain 0.1.19 preview against stock FilaMan 2.2.2 using a disposable library and the same prompts on both Ollama models. Both models used Q4_K_M. The chat sample covered seven grounded cases and one navigation case. Research used the production extraction request with fixed excerpts to hold source availability constant. Each configuration ran once. These results establish behavior for the tested tags and preview.

| Initial 0.1.19 preview | Qwen 3 4B Instruct | Llama 3.1 8B |
| --- | --- | --- |
| Model discovery and setup test | Found, setup passed | Found, setup returned 502 on the extraction sample |
| Grounded answers accepted by the validator | 2 of 7. Evidence cards filled in the other five | 3 of 7. Evidence cards filled in the other four |
| Open a prefilled filament form | Passed | Passed |
| Parseable extraction output | 2 of 3 cases | 0 of 3 cases |
| Report both nozzle ranges when sources disagree | Missed | Missed |

Both models answered the ASA-versus-PETG question. For a saved printer profile, the cards showed the exact selected nozzle, temperature, and bed values. Both model replies repeated numbers and fell back to those cards. When comparing regular TPU with Bambu TPU for AMS, retrieval surfaced the exact grade and feeder specifications but omitted the explicit generic TPU restriction. This is a retrieval gap.

The extraction cases used an exact 1 kg spooled product next to a 500 g refill, conflicting manufacturer and reseller nozzle ranges, and a product page with no stated tare. Qwen recovered the exact package and published tare from the first case. Its output for the conflict was invalid JSON.

## Team follow-up

1. Keep the application responsible for variant matching, spool eligibility and weight calculations, settings lookup, source provenance, and approval. Return exact values and links from evidence. Check that an unknown weight stays unknown and that net or gross weight cannot become tare.
2. Retrieve both sides of a compatibility comparison before prompting the model. Add a regression for generic TPU and Bambu TPU for AMS in the same AMS 2 Pro question. Allow validated printer and product names such as A1 and AMS 2 Pro in chat prose while keeping numeric claims behind evidence validation.
3. Try short, scoped extraction requests with a response schema and compact quotes where the provider supports it. A schema experiment gave Llama three parseable outputs. It still omitted the second nozzle range. Qwen recovered the exact product’s nozzle and bed ranges but truncated JSON on the conflicting-source case. Track parseability and evidence recall as separate checks. [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
4. Keep the current fallback visible. The short chat prompt trial moved Qwen from two to four accepted replies and Llama from three to two. Use task-level checks and repeated runs across exact model tags before standardizing a prompt or expanding the recommended list.

## Preview review follow-up

The reviewed preview allows more time for extraction and accepts a bare array of suggestions through the existing quote checks. It collects explicitly labeled temperature ranges from matching supplier excerpts, so a source can remain visible when the model omits it. Grounded chat now retrieves both AMS 2 Pro TPU rules and permits exact device names found in the cited evidence. The table above records the initial benchmark.
