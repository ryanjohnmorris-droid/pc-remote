# PC Remote

Control your Windows PC from your phone: trackpad, keyboard, media and volume, open apps, see your screen, send files, share the clipboard and more. It works on iPhone and Android, including Samsung Galaxy phones, foldables and tablets with Samsung Internet or Chrome. You don't need an App Store app.

<p align="center">
  <img src="mockups/showcase.png" width="760" alt="PC Remote on a phone: Trackpad, Keyboard, Media, Apps, Screen, More, Files, My buttons and Game pad screens">
</p>

## Features

**Main tabs**
- **Trackpad:** a smooth, precise pointer. Slow finger movements give fine control and quick flicks cross the screen. Laptop-style gestures: tap to click, tap twice and hold to drag, two-finger tap for right-click, three-finger tap for middle-click, and two fingers to scroll (natural scrolling optional).
- **Keyboard:** type straight into the PC (works with iPhone, Gboard and Samsung keyboards), plus Ctrl/Alt/Shift shortcuts, arrow keys, and More keys (F1–F12, Home, End, Page Up/Down, Delete).
- **Media:** **Now Playing** (song, artist and album art from Spotify, YouTube, etc.), a **real volume slider**, play/pause and track buttons, and handy shortcuts.
- **Apps:** every app in your Start menu, with search and favorites. Tap one to open it and see it on your phone.
- **Screen:** a live view of the active window or the whole screen. Tap the picture to click there, pinch to zoom in on small text, and use 📸 to save a full-size screenshot to your phone.
- **📱 Phone size:** resizes the app's window on the PC into a tall, phone-shaped window, so it fills your phone and is easy to read. Websites switch to their mobile layout. Tap ↩ to put the window back. Apps you open from the phone do this automatically (you can turn that off in Settings). A small live view also appears on the Trackpad, Keyboard and Media tabs, with the mouse pointer drawn on it.

**More tab**
- **Power:** lock, sleep, restart and shut down (restart and shut down wait 30 s so you can cancel).
- **Files:** send photos and files from your phone to the PC, and download files from the PC to your phone.
- **Clipboard & links:** copy or paste text on the PC from your phone, get the PC's clipboard, and open a link in the PC's browser.
- **Open windows:** see what's open, switch to it, or close it.
- **My buttons:** your own one-tap buttons for keyboard shortcuts (e.g. Discord mute), websites, apps or text snippets. They're saved on the PC, so every phone gets them.
- **Sleep timer:** sleep or shut down the PC in 15 min to 2 hours.
- **PC stats:** CPU, memory, disk, battery and uptime, live.
- **Presentation:** big Next/Back buttons for PowerPoint and Google Slides.
- **Game pad:** hold-down WASD/arrow keys and action buttons for simple keyboard games.
- **My PCs:** switch between several PCs.
- **Settings:** tap to click, natural scrolling, scroll speed, vibration, and the live screen picture on or off.

The status at the top shows the connection delay in milliseconds (or "slow mode" if the live connection couldn't start).

**Under the hood:** one Python file that only uses the standard library, so there's nothing else to install. A live WebSocket connection keeps the mouse responsive.

## Setup

1. Install Python 3 on your Windows PC from [python.org/downloads](https://www.python.org/downloads/).
2. Download [`server.py`](server.py) (click it, then the download button) and put it somewhere it can stay.
3. Double-click `server.py`. If Windows Firewall asks, click **Allow**.
4. **Scan the QR code** in the window with your phone's camera. Your phone must be on the **same Wi-Fi**. You can also type the link shown under the QR code.
5. Add it to your Home Screen:
   - **iPhone:** Share → **Add to Home Screen**
   - **Android (Chrome):** ⋮ menu → **Add to Home screen**
   - **Samsung Internet:** ≡ menu → **Add page to** → **Home screen**

**Start with Windows (optional):** right-click `server.py` → **Create shortcut**. Press `Win + R`, type `shell:startup` and move the shortcut into that folder.

Files you send from your phone go to **Downloads ▸ PC Remote**. Put files in that folder to download them on your phone.

## Troubleshooting

| Problem | Fix |
|---|---|
| **Wrong key** | Scan the QR code again, or type the link exactly as shown. Make sure only one copy of `server.py` is running. |
| **PC not reachable / site can't be reached** | Make sure the phone is on the same Wi-Fi (not mobile data or a guest network). Allow Python through Windows Firewall, or run this in an admin terminal: `netsh advfirewall firewall add rule name="PC Remote" dir=in action=allow protocol=TCP localport=8765`. Some routers block devices from talking to each other ("AP/client isolation"); turn that off in the router settings. |
| **Link opens inside Discord / another app and fails** | Open Chrome, Safari or Samsung Internet yourself and type the link in, starting with `http://`. |
| **Samsung: page won't load** | Turn off **Secure Wi-Fi** or any VPN on the phone (Settings → Connections). |
| **Smart App Control blocks a file** | Double-click `server.py` directly. It runs with Python, which Windows trusts. |
| **Mouse too fast / slow** | Use the Speed slider on the Trackpad tab. |
| **Screen taps land in the wrong place** | The PC window says so at startup if display scaling couldn't be set up. Restart `server.py`. |
| **Screen is black** | Some full-screen games can't be captured this way. |
| **Now Playing is empty** | It shows apps that report to Windows' media controls (Spotify, browsers, Media Player). Start playing something first. |

## Security

- The link and QR code contain a random key, and the PC ignores requests without it. The key is saved in `config.json` next to `server.py`. Delete that file to make a new one.
- Traffic is **not encrypted**, so use it on your home Wi-Fi only. **Do not** forward the port on your router to the internet. To use it away from home, install [Tailscale](https://tailscale.com) on the PC and the phone, and use the PC's Tailscale address in the link.
- Uploads can only go into the `Downloads\PC Remote` folder, and downloads can only come from it.

## Turning the PC on from your phone

PC Remote can't turn on a PC that's off, because nothing is running on it yet. Use **Wake-on-LAN** instead, with a free app like Wolow (iPhone) or Wake On Lan (Android). Your PC needs an Ethernet cable, "Wake on Magic Packet" enabled for the network adapter in Device Manager, Fast Startup turned off, and Wake-on-LAN / "Power On By PCI-E" enabled in the BIOS.

## For developers

`server.py` in the main folder is built from the readable source in [`source/`](source/):

```bash
python source/build_single.py
```

On non-Windows systems the server runs in **dry-run** mode. It logs commands instead of sending input and fakes the PC side, which is handy for working on the phone UI.

## License

[MIT](LICENSE)
