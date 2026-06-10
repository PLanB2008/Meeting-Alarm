# Meeting Alarm — Setup Guide

A hard-to-dismiss macOS alarm that blacks out your screen and plays a sound
until you confirm your meeting. Runs as a menu bar app. Works with Google Calendar.

---

## What it does

- Lives in your **menu bar**, showing the next upcoming meeting (e.g. `Weekly Sync in 12 min`)
- Checks your Google Calendar every 30 seconds
- **2 minutes before any meeting**: blacks out your entire screen, plays an alarm sound on your built-in speakers
- Shows a **"Join Meeting"** button that opens Google Meet / Zoom / Teams directly
- Screen stays blocked until you click a button — no accidental dismissal
- Supports a **blacklist** (`blacklist.yaml`) to suppress alarms for specific meetings

---

## Step 1 — Install Python dependencies

Open **Terminal** and run:

```bash
pip3 install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client \
             rumps sounddevice soundfile pyyaml
```

---

## Step 2 — Create a Google Calendar API credential

1. Go to: https://console.cloud.google.com/
2. Click **"Select a project"** → **"New Project"** → name it "Meeting Alarm" → **Create**
3. In the left menu: **APIs & Services → Library**
4. Search for **"Google Calendar API"** → click it → **Enable**
5. Go to **APIs & Services → Credentials**
6. Click **"+ Create Credentials"** → **OAuth client ID**
7. If prompted to configure the consent screen:
   - Choose **External** → **Create**
   - Fill in App name: "Meeting Alarm", your email → **Save and Continue** (skip the rest)
   - Go back to Credentials → Create Credentials → OAuth client ID
8. Application type: **Desktop app** → Name: "Meeting Alarm" → **Create**
9. Click **Download JSON** on the credential that appears
10. Rename the downloaded file to **`credentials.json`**
11. Move it to the **same folder** as `meeting_alarm.py`

---

## Step 3 — Build the app (recommended)

Run the build script once to create a standard macOS `.app` bundle:

```bash
bash build_app.sh
```

This creates **`Meeting Alarm.app`** in the same folder. You can:
- Double-click it to launch
- Drag it to `/Applications` for easy access
- Add it to your Dock

The app runs as a **menu bar only** app — no Dock icon, no window until an alarm fires.

To rebuild after changing the code, just run `bash build_app.sh` again.

#### Optional: custom icon

Drop a `1024×1024` PNG named `AppIcon.png` next to `build_app.sh` and re-run — it will be
converted and bundled automatically.

---

## Step 3 (alternative) — Run from Terminal

If you prefer not to build the app:

```bash
cd /path/to/meeting-alarm
python3 meeting_alarm.py
```

The first time it runs, a browser window will open asking you to authorise the app
with your Google account. Click through and allow it. This only happens once.

---

## Test it works

```bash
python3 meeting_alarm.py --demo
```

This shows the alarm immediately (no calendar needed) so you can see what it looks like.

---

## Blacklist

To suppress alarms for specific meetings, edit `blacklist.yaml` in the project folder.
The file is re-read on every poll cycle — no restart needed.

```yaml
patterns:
  - "Lunch"              # any meeting with "Lunch" in the title
  - "^Daily Standup$"    # exact match (anchored regex)
  - "standup|sync"       # matches either word
```

Blacklisted meetings still appear in the menu dropdown with a 🔕 icon, but no alarm fires.

---

## Auto-start at login (optional)

**Easiest:** Go to **System Settings → General → Login Items** and add `Meeting Alarm.app`.

**Alternative (LaunchAgent):** replace the path with where your `.app` actually lives:

```bash
cat > ~/Library/LaunchAgents/com.meetingalarm.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>       <string>com.meetingalarm</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Applications/Meeting Alarm.app/Contents/MacOS/meeting_alarm</string>
  </array>
  <key>RunAtLoad</key>   <true/>
  <key>KeepAlive</key>   <true/>
  <key>StandardErrorPath</key>
  <string>/tmp/meetingalarm.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.meetingalarm.plist
```

---

## Customise

Open `meeting_alarm.py` in any text editor and change these lines near the top:

| Setting | Default | What it does |
|---|---|---|
| `ALERT_MINUTES_BEFORE` | `2` | Minutes before a meeting to trigger the alarm |
| `POLL_INTERVAL_SECS` | `30` | How often the calendar is checked |
| `ALARM_VOLUME` | `0.8` | Built-in speaker volume during the alarm (0.0 – 1.0) |

---

## Troubleshooting

**"credentials.json not found"** — Make sure the file is in the same folder as `meeting_alarm.py`.

**No sound / wrong device** — The app targets the built-in MacBook speakers regardless of
your default audio device. If sound doesn't play, check that your Mac's internal volume is
not muted and that `sounddevice` is installed (`pip3 install sounddevice soundfile`).

**Alarm doesn't cover all screens** — Only the primary screen is covered. Multi-monitor
support can be added on request.

**"Access blocked" in browser** — On the OAuth consent screen, add your email as a test user
under "APIs & Services → OAuth consent screen → Test users".

**Logs** — When running as `.app`, logs go to `~/Library/Logs/meeting_alarm.log`.
