# Third-party notice: Smart Turn v3 (Pipecat AI)

This directory (`smart_turn/`) and `assets/models/smart-turn-v3.2-cpu.onnx`
contain code and a model file derived from the
[pipecat-ai/pipecat](https://github.com/pipecat-ai/pipecat) project, pinned
at commit `d7f1abc0c38d91b9c8345d404ce39386db2939fe`.

- `smart_turn/_whisper_features.py`: vendored verbatim from
  `src/pipecat/audio/turn/smart_turn/_whisper_features.py`.
- `assets/models/smart-turn-v3.2-cpu.onnx`: copied verbatim from
  `src/pipecat/audio/turn/smart_turn/data/smart-turn-v3.2-cpu.onnx`.
- `smart_turn_model.py` (repo root, not in this directory): a from-scratch
  wrapper that reimplements the pre/post-processing steps of
  `src/pipecat/audio/turn/smart_turn/local_smart_turn_v3.py`
  (`LocalSmartTurnAnalyzerV3._predict_endpoint`) without depending on
  Pipecat's pipeline framework (`Frame`/`FrameProcessor`/`BaseTurnAnalyzer`),
  since this project does not use Pipecat.

Both the code and the model weights are licensed under the BSD 2-Clause
License, copyright (c) 2024-2026, Daily. Full text:

```
BSD 2-Clause License

Copyright (c) 2024-2026, Daily

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

`_whisper_features.py` additionally notes that its math mirrors
`transformers.WhisperFeatureExtractor` (Apache-2.0, Hugging Face); see that
file's own header for details. No `transformers` code is copied here — only
the vendored numpy reimplementation from the pipecat-ai project above.
