# Third-party notices

## RNNoise (`librnnoise.dylib` / `rnnoise.dll`)

This app bundles a prebuilt RNNoise shared library used for background noise
suppression on recorded audio (see `noise_reduction.py`). The binary is taken
directly from the [pyrnnoise](https://github.com/pengzhendong/pyrnnoise)
project's published wheels (not the pyrnnoise Python package itself, which
pulls in dependencies this app doesn't need — see `noise_reduction.py`'s own
docstring) and is licensed under the Apache License, Version 2.0. The
underlying RNNoise algorithm/model is the work of Xiph.Org / Jean-Marc Valin
(https://github.com/xiph/rnnoise).

Full license text: `THIRD_PARTY_LICENSES/pyrnnoise-Apache-2.0.txt`.
