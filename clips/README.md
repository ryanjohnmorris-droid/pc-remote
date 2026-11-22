# Clutch: clip your mobile games

Clutch records your phone games in the background. When you make a great play, you save it, and Clutch keeps the **last 15 seconds to 2 minutes** as a clip. You don't need to record whole sessions and cut them up later.

There are two native apps: **Android** (`android/`) and **iPhone** (`ios/`).

## What it does

| | Android | iPhone |
|---|---|---|
| Rolling replay buffer (15 s – 2 min) | ✅ kept in memory | ✅ kept as short files on the phone |
| Save mid-game | Floating **CLIP** button, Quick Settings tile, notification button | **Back Tap**, Action Button, Siri, or the Shortcuts app ("Save Clip") |
| Game sound | ✅ Android 10+ (some games block it) | ✅ |
| Microphone, mixed in | ✅ | ✅ (turn the mic on in the Start sheet) |
| Clip library with thumbnails | ✅ | ✅ |
| Trim | ✅ saves a trimmed copy | ✅ saves a trimmed copy |
| Share | Share sheet (Discord, WhatsApp, TikTok…) | Share sheet |
| Save to Gallery / Photos | ✅ Movies › Clutch | ✅ |
| Rename, delete | ✅ | ✅ |
| Quality | 720p / 1080p, 30 / 60 fps | 720p / 1080p |

## Android

**Get the app:** open this repository's **Actions** tab on GitHub → **Build Clutch apps** → the latest run → download **Clutch-android** under *Artifacts*. Unzip it to get `app-release.apk`. Open it on your phone and allow "Install unknown apps" when Android asks.

Or build it yourself with Android Studio: open the `clips/android` folder and press ▶ Run.

**Use it:**
1. Open Clutch and tap **Start replay**. Allow notifications, the microphone (needed for game sound too), and "Display over other apps" for the floating button. Then tap **Start now** on the screen-recording prompt.
2. Open your game and play.
3. When something great happens, tap the floating **CLIP** button (drag it out of the way first). You can also swipe down and tap the **Save clip** tile, or tap **Save clip** in the notification. The phone buzzes when the clip is saved.
4. Open Clutch to watch, trim, share or save your clips.

Notes:
- If you turn the phone between portrait and landscape, the replay starts over in the new orientation.
- The floating button shows up in your clips. Turn it off in settings and use the tile instead if you'd rather it didn't.
- Some games mark their audio as "don't capture". Those clips have no game sound, but the mic still works.
- 2-minute clips at 1080p 60 fps use about 200 MB of memory while replay is on.

## iPhone

Apple only lets you install your own apps through Xcode on a Mac (a free Apple ID works) or with a sideloading tool.

**Build with Xcode (recommended):**
1. On a Mac, install Xcode and [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`).
2. In `clips/ios/project.yml`, change `BASE_BUNDLE_ID: com.example.clutch` to something unique, like `com.yourname.clutch`.
3. In the `clips/ios` folder, run `xcodegen`, then open `Clutch.xcodeproj`.
4. Select each of the **Clutch** and **ClutchBroadcast** targets. Under *Signing & Capabilities*, pick your Team. Both targets use the App Group `group.<your bundle id>`. Xcode creates it the first time.
5. Plug in your iPhone, choose it at the top and press ▶ Run. On the phone, trust the developer in Settings › General › VPN & Device Management.

With a free Apple ID, the app works for 7 days before you need to run it from Xcode again.

**No Mac?** Each GitHub Actions run also produces **Clutch-iphone-unsigned** (an unsigned `.ipa`). Sideloading tools such as Sideloadly (Windows/Mac) can sign it with your Apple ID. Some tools rename the App Group when they sign. If Clutch shows a warning that the App Group isn't set up, use the Xcode route instead.

**Use it:**
1. Open Clutch and tap **Start replay**. In Apple's sheet, choose **Clutch**, turn the microphone on or off, and tap **Start Broadcast**. You can also start it from Control Center: long-press **Screen Recording** and pick Clutch.
2. Set up a way to save mid-game. Go to **Settings › Accessibility › Touch › Back Tap › Double Tap** and choose **Save Clip**. Now tapping the back of your iPhone twice saves a clip. You can also put Save Clip on the Action Button, or say "Hey Siri, save a clip in Clutch".
3. Play. Double-tap the back of the phone after a great play.
4. Open Clutch. New clips take a few seconds to finish, then you can watch, trim, share, save to Photos, rename or delete them.

The red status bar or pill at the top of the screen means replay is on. Tap it, or tap **Stop replay** in Clutch, to stop.

## How it works

- **Android:** a foreground service mirrors the screen into the phone's hardware H.264 encoder using `MediaProjection`. Game sound (`AudioPlaybackCapture`) and the mic are mixed into one AAC track. The encoded video and audio go into a fixed-size ring buffer outside the Java heap. Saving copies the newest keyframe-aligned stretch into an MP4 with `MediaMuxer`, without re-encoding. Trimming works the same way.
- **iPhone:** a ReplayKit broadcast upload extension writes the screen, game sound and mic to 6-second MP4 segments, keeping only the newest ones. The **Save Clip** App Intent (behind Back Tap, Siri and the Action Button) signals the extension, which hands the needed segments to the app through the App Group. The app joins them, mixes the game sound and mic, and exports the finished clip.
