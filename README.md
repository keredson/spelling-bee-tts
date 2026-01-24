![Spelling Bee icon](spelling-bee-tts.png)

Spelling Bee TTS
================

Spelling Bee TTS is a spelling testing app I wrote for my kids.  It uses natural text-to-speech for the prompt and a local large language model
to generate definitions and example sentences.  It has a built in corpus of ~31,000 words.

The app tracks the user's progress and automatically adjusts the word difficulty level based on how well they're doing.

![Screenshot](screenshot.png)

Install
-------

System dependencies (Ubuntu):

```
sudo apt install python3-gi gir1.2-gtk-4.0 mpv
```

System dependencies (macOS with Homebrew):

```
brew install python gtk4 pygobject3 gobject-introspection mpv
```

Install with `pip`:

```
pip install spelling-bee-tts
```

Run
---
On Linux `pip` creates a application shortcut.  Hit your launcher and search for 'spell'.  Or to run from the command line:

```
python3 -m spellingbee
```

Notes
-----
- `edge-tts` requires network access.
- You can override the voice with `EDGE_TTS_VOICE` (default: `en-US-AriaNeural`).
- Offline sentence generation uses `llama-cpp-python` with a local GGUF model.
  - By default, the app prompts to download Qwen3-4B-Instruct-2507 Q8_0 into the Hugging Face cache on first use.
  - Override with `LLM_REPO_ID` or `LLM_MODEL_PATH`.
  - Optional tuning: `LLM_N_CTX`, `LLM_THREADS`, `LLM_N_BATCH`, `LLM_TEMPERATURE`, `LLM_TOP_P`.
