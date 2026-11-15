# Third-party notices

ReelSense's own code is under the [MIT licence](LICENSE). The Docker image it builds installs the following, each under its own licence:

| Component | Licence | Source |
|---|---|---|
| yt-dlp (PyPI package) | Unlicense | https://github.com/yt-dlp/yt-dlp |
| yt-dlp's optional extras: mutagen (GPL-2.0+), curl_cffi (MIT), pycryptodomex (BSD), brotli (MIT), websockets (BSD) and others | various | https://github.com/yt-dlp/yt-dlp#dependencies |
| FFmpeg (Debian package) | LGPL-2.1+; GPL-2.0+ as Debian builds it | https://ffmpeg.org/legal.html |
| Deno | MIT | https://github.com/denoland/deno |
| FastMCP | Apache-2.0 | https://github.com/PrefectHQ/fastmcp |
| MCP Python SDK | MIT | https://github.com/modelcontextprotocol/python-sdk |
| Uvicorn | BSD-3-Clause | https://github.com/encode/uvicorn |
| Requests | Apache-2.0 | https://github.com/psf/requests |
| Python and Debian base image (`python:3.12-slim`) | PSF-2.0 and Debian package licences | https://hub.docker.com/_/python |

`requirements.txt` locks ReelSense's application dependencies. yt-dlp and its optional extras are installed separately and may vary between image builds. Inspect a built image with `pip freeze` and an SBOM for its exact contents.

This repository publishes source code only. Anyone who distributes a built image must comply with each component's licence, including preserving required notices and providing corresponding source code, licence texts or build information where required. `docker scout sbom <image>` lists what an image contains.

ReelSense calls these services' APIs, under their own terms, not a software licence: Deepgram, AudD, Voyage AI, Spotify and Apple's iTunes Search API.
