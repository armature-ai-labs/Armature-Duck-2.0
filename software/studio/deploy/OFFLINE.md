# Offline & Outdoor Operation

Both primary control paths work without internet access:

1. **Mobile Voice Control (Duck Hotspot)**:
   - Connect your phone to `Microduck-xxxx` (default password `microduck`).
   - Open `https://10.42.0.1:8000/m`.
   - Download and trust `Microduck-Local-CA.crt` from **More -> Phone Mic Certificate** so the browser grants microphone access over HTTPS.
   - Use **More -> Outdoor** to stop the hotspot and reconnect to your saved home Wi-Fi (`https://duck-xxxx.local:8000/m`).

2. **Physical Bluetooth Gamepad**:
   - Pair via the **Gamepad** page wizard or `sudo robotctl pad pair`.
   - `padd` reads `/dev/input/event*` locally and communicates directly with `/run/robotd.sock`.

## TLS Commands

```bash
sudo /usr/local/sbin/microduck-helper phone-tls
sudo systemctl restart microduck-studio
```
