![Spelling Bee icon](spelling-bee-tts.png)

Install
-------

System dependencies (Ubuntu):

```
sudo apt install python3-gi gir1.2-gtk-4.0 mpv
```

Python dependencies:

```
python3 -m pip install --user -r requirements.txt
```

Run:

```
python3 -m spellingbee
```

Notes:
- `edge-tts` requires network access.
- You can override the voice with `EDGE_TTS_VOICE` (default: `en-US-AriaNeural`).
- Offline sentence generation uses `llama-cpp-python` with a local GGUF model.
  - By default, the app prompts to download Qwen3-4B-Instruct-2507 Q8_0 into the Hugging Face cache on first use.
  - Override with `LLM_REPO_ID` or `LLM_MODEL_PATH`.
  - Optional tuning: `LLM_N_CTX`, `LLM_THREADS`, `LLM_N_BATCH`, `LLM_TEMPERATURE`, `LLM_TOP_P`.
