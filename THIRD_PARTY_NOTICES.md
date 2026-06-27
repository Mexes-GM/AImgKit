# Third-Party Notices

AImgKit is distributed under the [MIT License](LICENSE). It bundles and/or
depends on third-party components that are distributed under their own terms,
listed below.

## FFmpeg (bundled binary)

The Windows release of AImgKit (`AImgKit.exe`) bundles an **unmodified** build
of `ffmpeg.exe`, used as a separate executable (invoked as a subprocess) solely
to remux the original audio track back into watermarked videos. AImgKit does not
link against the FFmpeg libraries.

- **Component:** FFmpeg (`ffmpeg.exe`)
- **License:** GNU Lesser General Public License, version 2.1 or later (LGPL-2.1+)
- **Build:** static LGPL build from the BtbN FFmpeg-Builds project
  (`ffmpeg-master-latest-win64-lgpl.zip`)
- **Build project / binaries:** https://github.com/BtbN/FFmpeg-Builds
- **FFmpeg project & source code:** https://ffmpeg.org/ — https://git.ffmpeg.org/ffmpeg.git
- **License text:** https://www.ffmpeg.org/legal.html

The bundled binary is the upstream build, redistributed without modification.
Its corresponding source code is available from the FFmpeg project and the BtbN
build project linked above. If you obtained `AImgKit.exe` and require the exact
source of the bundled FFmpeg build, it can be retrieved from the BtbN release
that produced the binary.

> When running AImgKit **from source**, no FFmpeg binary is bundled; AImgKit uses
> an `ffmpeg` found on your `PATH` (or next to the script), if present.

## Python dependencies

Installed via `requirements.txt`; each retains its own license:

| Package | License |
|---|---|
| Pillow | MIT-CMU / HPND |
| opencv-python | Apache-2.0 (OpenCV); MIT (Python wrapper) |
| NumPy | BSD-3-Clause |
| tkinterdnd2 | MIT |
| customtkinter | MIT |

Build tooling: **PyInstaller** is licensed under the GPL with a bootloader
exception that explicitly permits distributing the generated executable under
any license, so it does not affect AImgKit's MIT licensing.
