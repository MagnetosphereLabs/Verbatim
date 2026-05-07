Install with 1 command:
```
curl -fsSL https://raw.githubusercontent.com/MagnetosphereLabs/Verbatim/main/install.sh | bash
```

Uninstall with 1 command:
```
curl -fsSL https://raw.githubusercontent.com/MagnetosphereLabs/Verbatim/main/uninstall.sh | bash -s -- --purge-system
```

<img src="/demo/demo1.gif" width="1024" />

# Verbatim

Local AI voice dictation for Linux.

Click a text input, press **Super+V**, speak naturally, pause for a moment, and Verbatim types what you said into the focused app. It works with browsers and other desktop apps, it even supports VR desktop workflows through WayVR.

It is built for those who want voice dictation on Linux to be simple, fast, and private. No cloud transcription, no analytics, no privacy concerns, no accounts, and no subscription. Just a small overlay, a shortcut, and local Whisper running on your hardware.

## What makes this different

Linux already has pieces that can do parts of voice dictation. Whisper can transcribe audio. Wayland can handle modern desktop sessions. GPUs can run AI models quickly. Virtual input can paste text into focused apps.

Verbatim does that orchestration for you.

Assuming your graphics drivers are configured properly, it installs the right backend for your hardware, configures the shortcut, sets up the user service, prepares the model, creates a small dictation popup that records only when you ask it to, transcribes locally, then inserts the text into the input field you already had selected.

If you use WayVR, it also adds a microphone icon right next to the keyboard icon on your wristwatch. And it also adds a new microphone icon on the WayVR keyboard for easy voice dictation, even while inside of VR.

<img src="/demo/demo22.gif" width="1024" />

## Features

- Local Whisper based voice dictation
- No cloud API and no server account
- One command installer
- Simple model choice during install
- NVIDIA GPU acceleration through faster-whisper and CTranslate2
- AMD and other Vulkan GPU support through whisper.cpp
- CPU fallback only when GPU acceleration is not available
- Simple GUI
- Wayland friendly text insertion
- Works in Firefox, Chromium, terminals, Electron apps, COSMIC apps, and most normal text fields
- Super+V desktop shortcut on most Debian based desktops (like Ubuntu and Pop OS)
- User systemd service so it is ready after login
- Optional WayVR integration with mic buttons in VR
- Clean uninstall path that removes Verbatim's files, service entries, shortcuts, and WayVR changes
