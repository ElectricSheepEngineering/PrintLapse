# PrintLapse

PrintLapse grabs stills from an RTSP camera on a timer and turns them into a timelapse video. It is meant for a Prusa Buddy3D camera on a home network, and it works with any other RTSP URL.

One ffmpeg process stays connected and saves a JPEG at the interval you set. When the capture stops, ffmpeg builds an H.264 `.mp4`. The JPEGs stay in the session folder, so the video can be rebuilt at another frame rate without recording again.

## No authentication. Local use only.

This app has no login, no password, and no access token. Anyone who can open the page can start and stop a capture, read the saved camera URL (including a password typed into it), download frames and videos, and delete files on the output volume.

Run it only on your own machine, or on a private host that is not reachable from the internet. Bind the desktop app to `127.0.0.1`. If you use Docker, publish the port only on that host's local network. Do not put it on a public hostname, a reverse proxy open to the internet, or a server shared with people who should not control the camera and the saved files.

## Desktop

Python 3.9 or newer is enough. There are no Python packages to install. ffmpeg must be on your `PATH`, and it needs libx264 to build the video.

```bash
# macOS
brew install ffmpeg

# Windows
winget install Gyan.FFmpeg

# Debian, Ubuntu, Raspberry Pi OS
sudo apt install ffmpeg
```

From this directory:

```bash
python3 -m printlapse
```

That opens `http://127.0.0.1:8765/`. Leave the terminal open while a capture is running. Closing the browser does not stop the capture. Closing the terminal does.

To put the command on your `PATH`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
printlapse
```

On Windows, activate the venv with `.venv\Scripts\activate`.

`gui --host 127.0.0.1 --port 8765` is the default. `--no-browser` skips opening a window. Do not pass `--host 0.0.0.0` on a machine other people can reach.

## Docker and Portainer

The image is Python 3.13 plus ffmpeg with libx264. Settings and sessions both live on `/data`, so one volume keeps them when the container is replaced. The same warning above applies: the container has no authentication.

From this directory, with Docker running:

```bash
docker compose up -d --build
```

Then open `http://127.0.0.1:8765/`. On another machine on your LAN, use the Docker host's address. Change the left side of `"8765:8765"` in `compose.yaml` if that port is taken. Do not forward that port from your router.

### Portainer from Git

1. Put this project in a Git repository the Portainer host can clone.
2. In Portainer, open **Stacks** → **Add stack** and name it `printlapse`.
3. Choose **Repository**. Set the repository URL and the compose path to `compose.yaml`.
4. Deploy the stack. Portainer builds the image from the Dockerfile.

### Portainer from an image you built

On the Docker host Portainer manages:

```bash
docker build -t printlapse:latest .
```

In Portainer, **Add stack** → **Web editor**, paste `portainer-stack.yml`, and turn off **Pull latest image**. Deploy the stack.

The camera has to be reachable from the container. A bridge network can usually open `rtsp://192.168.x.x/live`. If a test frame never arrives and the host is Linux, set `network_mode: host` on the service and remove the `ports:` section. Host networking is not available on Docker Desktop for Mac or Windows.

Leave the output folder blank in the form. That stores sessions in `/data/timelapses` on the volume. A path outside `/data` disappears when the container is replaced.

## Buddy3D camera

The camera and the computer have to be on the same network. The stream is the camera's own feed, not the printer's.

1. Open the Prusa app, select the printer, and open the Camera tab.
2. Turn on **RTSP stream on local network**.
3. Find the camera's IP address in the app, or in your router's device list. This is not the printer IP.
4. Use `rtsp://<camera-ip>/live`. Port 554 is the default and can be left off. `rtsp://192.168.1.42:554/live` is the same address.

Quit other live views before a long capture. The Buddy camera often accepts only one RTSP client, so VLC or the live view in the Prusa app can block this one.

A frame every 10 seconds, played back at 30 fps, is 300× realtime. One hour of printing becomes 12 seconds of video.

## Any other RTSP camera

Put the camera's URL in the same field. Usernames and passwords go in the URL:

```text
rtsp://user:password@192.168.1.50:554/stream1
```

Encode `@`, `:`, and other reserved characters in the password as `%40`, `%3A`, and so on. If TCP does not connect, switch transport to UDP.

The full URL, including a password, is stored in the settings file so the form can fill it in next time. `meta.json` in a session folder does not contain that password. Treat the settings file as a secret. On the desktop it is under Application Support on macOS, `%APPDATA%` on Windows, and `~/.config` on Linux. In Docker it is `/data/config/settings.json` on the volume. The page footer shows the path.

## What gets saved

Sessions go in `~/PrintLapse/` unless you pick another folder. In Docker the default is `/data/timelapses`. Each session looks like this:

```text
print-20261104-153012/
  frames/frame_000001.jpg
  latest.jpg
  timelapse.mp4
  meta.json
```

`meta.json` stores the interval, frame rate, and redacted camera URL. An original-size JPEG every 10 seconds for 8 hours is a few thousand frames. Narrow the frame width if you want to use less disk.

The page lists the 100 newest sessions under **Saved files**:

- **Video** downloads `timelapse.mp4`. **Play** opens it in the browser.
- **Frames zip** downloads the JPEGs.
- **Frames + video** downloads one zip with the JPEGs, the mp4, `latest.jpg`, and `meta.json`.
- **Files** shows sizes and a page of frames. You can delete the frames, the video, or the whole session. Deleting frames leaves the video. Deleting the video leaves the frames, so it can be rebuilt.

The app will not delete a session that is still capturing or encoding. **Show folder** opens the session in the desktop file manager. That button is hidden in Docker. There, the download buttons are how you copy files off the volume.

## Command line

```bash
python3 -m printlapse test --url rtsp://192.168.1.42/live
python3 -m printlapse capture --url rtsp://192.168.1.42/live --every 10 --fps 30
python3 -m printlapse encode "~/PrintLapse/print-20261104-153012" --fps 24
```

`capture` runs until you press Ctrl+C, then builds the video. `--minutes 120` stops on its own. `--no-encode` leaves only the frames. `--delete-frames` removes the JPEGs after a successful encode.

## How a capture works

ffmpeg connects over RTSP and keeps one frame per interval of wall-clock time. That stays even if the camera's own timestamps jump around. If the stream drops, the app reconnects and continues the frame numbers so the sequence has no gaps. After several failed reconnects it stops and, when it already has frames, still builds the video.

The video is H.264, `yuv420p`, CRF 18, with the moov atom at the front so it can start playing before it finishes downloading.

## Troubleshooting

- **ffmpeg missing.** Install it with the command for your system above, then start the app again. A build without libx264 can save frames but cannot make the video. The Docker image already includes a build with libx264.
- **No frame arrives.** Check that the IP is the camera, that RTSP is enabled, and that nothing else is viewing the stream. Try UDP if TCP fails.
- **The picture is from the wrong device.** The printer and the Buddy camera have different addresses.
- **The capture stops when the laptop sleeps.** Keep the computer awake for the length of the print, or run the app on a machine that stays on.
- **The container cannot see the camera.** Check that the camera IP is reachable from the Docker host. On Linux, host networking avoids the bridge NAT. The Buddy camera still allows only one RTSP client.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT. See [LICENSE](LICENSE). Replace `<copyright holder>` in that file with the name you want on the copyright line.
