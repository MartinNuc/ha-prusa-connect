# Prusa Connect for Home Assistant

A Home Assistant custom integration for [Prusa Connect](https://connect.prusa3d.com) that provides cloud-based monitoring and control of your Prusa 3D printers.

Uses the official Prusa Connect Mobile API with OAuth 2.0 PKCE authentication — the same API used by the Prusa mobile app.

## Features

- **Sensors**: Printer state, nozzle/bed temperatures, print progress, job info, speed, material, firmware, and more
- **Binary Sensors**: Online status, printing status, attention required, MMU/enclosure detection
- **Buttons**: Pause, resume, stop prints, set ready/cancel ready
- **Camera**: Printer camera snapshots
- **Image**: Print job preview thumbnails
- **Services**: Full print control including start from cloud/USB/URL, dialog responses
- **Diagnostics**: Built-in diagnostic data export for troubleshooting

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu → **Custom repositories**
3. Add this repository URL with category **Integration**
4. Search for "Prusa Connect" and install
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/prusa_connect` folder into your Home Assistant `custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Prusa Connect**
3. Enter your Prusa Account email and password
4. If you have 2FA enabled, enter your TOTP code when prompted
5. Your printers will be discovered automatically

## Entities

### Per Printer

| Entity | Type | Description |
|--------|------|-------------|
| State | Sensor | Current printer state (IDLE, PRINTING, etc.) |
| Nozzle Temperature | Sensor | Current nozzle temperature |
| Nozzle Target | Sensor | Target nozzle temperature |
| Bed Temperature | Sensor | Current bed temperature |
| Bed Target | Sensor | Target bed temperature |
| Print Speed | Sensor | Current print speed percentage |
| Print Progress | Sensor | Current print progress percentage |
| Current Job | Sensor | Name of the current print file |
| Time Remaining | Sensor | Estimated time remaining |
| Time Elapsed | Sensor | Time elapsed since print started |
| Material | Sensor | Loaded material (PLA, PETG, etc.) |
| Online | Binary Sensor | Whether the printer is online |
| Printing | Binary Sensor | Whether a print is active |
| Attention Required | Binary Sensor | Whether the printer needs attention |
| Pause Print | Button | Pause the current print |
| Resume Print | Button | Resume a paused print |
| Stop Print | Button | Stop the current print |
| Camera | Camera | Printer camera snapshot |
| Print Preview | Image | Thumbnail of the current print |

### Services

| Service | Description |
|---------|-------------|
| `prusa_connect.pause_print` | Pause current print |
| `prusa_connect.resume_print` | Resume paused print |
| `prusa_connect.stop_print` | Stop current print |
| `prusa_connect.start_print_cloud` | Start print from cloud storage |
| `prusa_connect.start_print_usb` | Start print from USB |
| `prusa_connect.start_print_url` | Start print from URL |
| `prusa_connect.set_ready` | Mark printer as ready |
| `prusa_connect.cancel_ready` | Cancel ready state |
| `prusa_connect.respond_to_dialog` | Respond to printer dialog |

## Requirements

- Home Assistant 2024.1.0 or newer
- A Prusa Account with at least one printer registered on Prusa Connect

## License

MIT
