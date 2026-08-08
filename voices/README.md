# Custom voices

Drop an audio clip in this folder and it becomes a selectable voice for any
cloning engine (currently Chatterbox-Turbo).

* **Longer than 5 seconds** — the model asserts on anything shorter.
* Accepted: `.wav`, `.mp3`, `.flac`, `.ogg`, `.m4a`
* The filename is the voice name: `Narrator Bob.wav` shows up as "Narrator Bob (custom)".
* Clean, single-speaker speech with no music or background noise clones best.

Editing a clip changes its modification time, which invalidates any chapter
audio cached from it — the next play re-renders with the new reference.

Kokoro ignores this folder: its voices are baked into the model and listed in
`config.yaml`.
