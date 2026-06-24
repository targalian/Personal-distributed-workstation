# QuickLAN

QuickLAN is a Windows 10/11 LAN file sharing and transfer desktop app created by xiaolong su.

## Features

- LAN device discovery with UDP broadcast.
- Point-to-point fast file transfer over TCP.
- Distributed shared file library with local Shared Store snapshots.
- Manifest sync over LAN HTTP API.
- SHA-256 verification after transfers and downloads.
- Downloaders automatically become replica nodes for shared resources.
- Password-protected shared resources.
- Local device notes, tray background mode, and single-instance startup.

## Tech Stack

- Frontend: React, TypeScript, Vite
- Desktop: Tauri
- Backend: Rust
- Local database: SQLite
- Networking: UDP broadcast, TCP transfer, LAN HTTP manifest API

## Development

```powershell
npm install
npm run app:dev
```

## Build

```powershell
npm run app:build
```

The Windows NSIS installer is generated under:

```text
src-tauri/target/release/bundle/nsis/
```

## Ports

- UDP `45454`: LAN device discovery
- TCP `45455-45474`: file transfer
- TCP `45457-45476`: LAN manifest HTTP API
- TCP `127.0.0.1:45456`: local control API

## Version

Current version: `0.1.1`

