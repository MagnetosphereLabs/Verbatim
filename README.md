Install with 1 command:
```
curl -fsSL https://raw.githubusercontent.com/MagnetosphereLabs/Verbatim/main/install.sh | bash
```

Uninstall with 1 command:
```
curl -fsSL https://raw.githubusercontent.com/MagnetosphereLabs/Verbatim/main/uninstall.sh | bash -s -- --purge-system
```

# Verbatim

Local AI voice dictation for Linux.

Click a text input, press **Super+V**, speak naturally, pause for a moment, and Verbatim types what you said into the focused app. It works with browsers and other desktop apps.

<img src="/demo/demo11.gif" width="1024" />

<img src="/demo/demo111.gif" width="1024" />

<img src="/demo/demo1111.gif" width="1024" />

## Works with WayVR and VR apps

<img src="/demo/demo222.gif" width="1024" />

<img src="/demo/demo333.gif" width="1024" />

## Other Features

- NVIDIA GPU acceleration through faster-whisper and CTranslate2
- AMD and other Vulkan GPU support through whisper.cpp
- CPU fallback only when GPU acceleration is not available
