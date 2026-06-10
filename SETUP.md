# Meeting Alarm — Setup Guide

A hard-to-dismiss macOS alarm that blacks out your screen and plays a sound
until you confirm your meeting. Works with Google Calendar.

---

## What it does

- Runs quietly in the background
- Checks your Google Calendar every 30 seconds
- **2 minutes before any meeting**: blacks out your entire screen, plays an alarm sound on loop
- Shows a **"Join Meeting"** button that opens Google Meet / Zoom / Teams directly
- Screen stays blocked until you click a button — no accidental dismissal

---

## Step 1 — Install Python dependencies

Open **Terminal** and run:

```bash
pip3 install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
```

---

## Step 2 — Create a Google Calendar API credential

1. Go to: https://console.cloud.google.com/
2. Click **"Select a project"** → **"New Project"** → name it "Meeting Alarm" → **Create**
3. In the left menu: **APIs & Services → Library**
4. Search for **"Google Calendar API"** → click it → **Enable**
5. Go to **APIs & Services → Credentials**
6. Click **"+ Create Credentials"** → **OAuth client ID**
7. If prompted to configure consent screen:
   - Choose **External** → **Create**
   - Fill in App name: "Meeting Alarm", your email → **Save and Continue** (skip the rest)
   - Go back to Credentials → Create Credentials → OAuth client ID
8. Application type: **Desktop app** → Name: "Meeting Alarm" → **Create**
9. Click **Download JSON** on the credential that appears
10. Rename the downloaded file to **`credentials.json`**
11. Move it to the **same folder** as `meeting_alarm.py`

---

## Step 3 — Run it

```bash
cd ~/Downloads    # (or wherever you put the files)
python3 meeting_alarm.py
```

The first time, it will open a browser window asking you to authorise the app
with your Google account. Click through and allow it. This only happens once.

---

## Test it works

```bash
python3 meeting_alarm.py --demo
```

This shows the alarm immediately (no calendar needed) so you can see what it looks like.

---

## Run it automatically at login (optional)

To have it start every time you log in:

```bash
# Create a launch agent (replace the path with where your file actually is)
cat > ~/Library/LaunchAgents/com.meetingalarm.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.meetingalarm</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOUR_USERNAME/Downloads/meeting_alarm.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardErrorPath</key>
  <string>/tmp/meetingalarm.log</string>
</dict>
</plist>
EOF

# Load it
launchctl load ~/Library/LaunchAgents/com.meetingalarm.plist
```

Replace `/Users/YOUR_USERNAME/Downloads/meeting_alarm.py` with the real path to your file.

---

## Customise

Open `meeting_alarm.py` in any text editor and change these lines near the top:

| Setting | Default | What it does |
|---|---|---|
| `ALERT_MINUTES_BEFORE` | `2` | How many minutes before the meeting to trigger the alarm |
| `POLL_INTERVAL_SECS` | `30` | How often it checks your calendar |

---

## Troubleshooting

**"credentials.json not found"** — Make sure the file is in the same folder as `meeting_alarm.py`.

**No sound** — The script uses system sounds in `/System/Library/Sounds/`. If missing, it falls back to `osascript beep`. Make sure your Mac volume is turned up.

**Alarm doesn't cover all screens** — The script covers your primary screen. Multi-monitor support can be added on request.

**"Access blocked" in browser** — On the OAuth consent screen, add your email as a test user under "APIs & Services → OAuth consent screen → Test users".
