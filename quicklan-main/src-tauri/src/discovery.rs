use crate::{
    lan_api,
    library::LibraryService,
    protocol::{
        DeviceInfo, DiscoveryPacket, NetworkStatus, DEVICE_TTL_SECS,
        LIBRARY_ANNOUNCE_INTERVAL_SECS, PRESENCE_INTERVAL_SECS, DISCOVERY_PORT,
    },
    settings::SettingsService,
    storage,
};
use get_if_addrs::{get_if_addrs, IfAddr};
use std::{
    collections::HashMap,
    fs,
    net::{IpAddr, Ipv4Addr, SocketAddr, UdpSocket},
    path::PathBuf,
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::{AppHandle, Emitter};
use uuid::Uuid;

#[derive(Clone)]
pub struct DiscoveryService {
    app: AppHandle,
    device_id: String,
    tcp_port: u16,
    api_port: u16,
    settings: SettingsService,
    library: LibraryService,
    devices: Arc<Mutex<HashMap<String, DeviceEntry>>>,
}

#[derive(Clone)]
struct DeviceEntry {
    info: DeviceInfo,
    last_seen: Instant,
}

impl DiscoveryService {
    pub fn new(
        app: AppHandle,
        tcp_port: u16,
        api_port: u16,
        settings: SettingsService,
        library: LibraryService,
    ) -> Self {
        Self {
            app,
            device_id: library.device_id(),
            tcp_port,
            api_port,
            settings,
            library,
            devices: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub fn start(&self) {
        self.start_presence_loop();
        self.start_library_announce_loop();
        self.start_listen_loop();
        self.start_prune_loop();
    }

    pub fn broadcast_now(&self) {
        let packet = self.packet("library");
        let _ = broadcast_packet(&packet);
    }

    pub fn list_devices(&self) -> Vec<DeviceInfo> {
        self.devices
            .lock()
            .map(|map| snapshot_devices(&map))
            .unwrap_or_default()
    }

    pub fn find_device(&self, id: &str) -> Option<DeviceInfo> {
        self.devices
            .lock()
            .ok()
            .and_then(|map| map.get(id).map(|entry| entry.info.clone()))
    }

    pub fn update_device_note(&self, device_id: String, note: String) -> Result<Vec<DeviceInfo>, String> {
        let note = self.library.update_device_note(&device_id, note)?;
        let snapshot = if let Ok(mut map) = self.devices.lock() {
            if let Some(entry) = map.get_mut(&device_id) {
                entry.info.note = note;
            }
            snapshot_devices(&map)
        } else {
            Vec::new()
        };
        let _ = self.app.emit("devices-updated", snapshot.clone());
        Ok(snapshot)
    }

    pub fn probe_ip(&self, ip: String) -> Result<(), String> {
        let target_ip: IpAddr = ip.parse().map_err(|_| "IP 地址无效".to_string())?;
        let packet = self.packet("library");
        send_discovery_packet(&packet, SocketAddr::new(target_ip, DISCOVERY_PORT))
    }

    pub fn network_status(&self) -> NetworkStatus {
        NetworkStatus {
            udp_port: DISCOVERY_PORT,
            tcp_port: self.tcp_port,
            api_port: self.api_port,
            local_ips: local_ipv4_addresses()
                .into_iter()
                .map(|ip| ip.to_string())
                .collect(),
            broadcast_targets: broadcast_targets()
                .into_iter()
                .map(|addr| addr.to_string())
                .collect(),
        }
    }

    pub fn local_sender(&self) -> crate::protocol::SenderInfo {
        crate::protocol::SenderInfo {
            device_id: self.device_id.clone(),
            device_name: self.settings.nickname(),
        }
    }

    fn packet(&self, packet_type: &str) -> DiscoveryPacket {
        let summary = self.library.summary();
        DiscoveryPacket {
            app: "quicklan".to_string(),
            version: 1,
            packet_type: packet_type.to_string(),
            device_id: self.device_id.clone(),
            device_name: self.settings.nickname(),
            tcp_port: self.tcp_port,
            api_port: self.api_port,
            library_version: summary.library_version,
            share_count: summary.share_count,
            manifest_hash: summary.manifest_hash,
            upload_tasks: 0,
        }
    }

    fn start_presence_loop(&self) {
        let service = self.clone();
        thread::spawn(move || loop {
            let packet = service.packet("presence");
            let _ = broadcast_packet(&packet);
            thread::sleep(Duration::from_secs(PRESENCE_INTERVAL_SECS));
        });
    }

    fn start_library_announce_loop(&self) {
        let service = self.clone();
        thread::spawn(move || loop {
            service.broadcast_now();
            thread::sleep(Duration::from_secs(LIBRARY_ANNOUNCE_INTERVAL_SECS));
        });
    }

    fn start_listen_loop(&self) {
        let app = self.app.clone();
        let devices = self.devices.clone();
        let own_id = self.device_id.clone();
        let service = self.clone();
        thread::spawn(move || {
            let socket = match UdpSocket::bind((Ipv4Addr::UNSPECIFIED, DISCOVERY_PORT)) {
                Ok(socket) => socket,
                Err(err) => {
                    eprintln!("UDP discovery failed on {DISCOVERY_PORT}: {err}");
                    return;
                }
            };
            let mut buf = [0_u8; 4096];

            loop {
                let Ok((len, addr)) = socket.recv_from(&mut buf) else {
                    continue;
                };
                let Ok(packet) = serde_json::from_slice::<DiscoveryPacket>(&buf[..len]) else {
                    continue;
                };
                if !packet.is_quicklan() || packet.device_id == own_id {
                    continue;
                }

                let _ = send_discovery_packet(&service.packet("presence"), addr);

                let info = DeviceInfo {
                    id: packet.device_id.clone(),
                    name: packet.device_name.clone(),
                    ip: addr.ip().to_string(),
                    tcp_port: packet.tcp_port,
                    api_port: packet.api_port,
                    online: true,
                    last_seen_ms: now_ms(),
                    share_count: packet.share_count,
                    library_version: packet.library_version,
                    manifest_hash: packet.manifest_hash.clone(),
                    upload_tasks: packet.upload_tasks,
                    latency_ms: None,
                    note: service.library.device_note(&packet.device_id),
                };

                let should_sync = match packet.packet_type.as_str() {
                    "library" => service
                        .library
                        .observe_device(&packet, info.ip.clone())
                        .unwrap_or(false),
                    "presence" => service.library.has_active_remote_owner(&packet.device_id),
                    _ => false,
                };

                if let Ok(mut map) = devices.lock() {
                    map.insert(
                        info.id.clone(),
                        DeviceEntry {
                            info: info.clone(),
                            last_seen: Instant::now(),
                        },
                    );
                    let snapshot = snapshot_devices(&map);
                    let _ = app.emit("devices-updated", snapshot);
                }

                if should_sync {
                    let library = service.library.clone();
                    let app = app.clone();
                    let ip = info.ip.clone();
                    thread::spawn(move || {
                        if let Ok(manifest) =
                            lan_api::fetch_manifest_blocking(&ip, packet.api_port)
                        {
                            let _ = library.merge_manifest(
                                manifest,
                                ip,
                                packet.tcp_port,
                                packet.api_port,
                            );
                            let _ = app.emit(
                                "library-updated",
                                library.list_shared_resources().unwrap_or_default(),
                            );
                        }
                    });
                }
            }
        });
    }

    fn start_prune_loop(&self) {
        let app = self.app.clone();
        let devices = self.devices.clone();
        thread::spawn(move || loop {
            thread::sleep(Duration::from_secs(5));
            let mut changed = false;
            let snapshot = if let Ok(mut map) = devices.lock() {
                for entry in map.values_mut() {
                    let online = entry.last_seen.elapsed() < Duration::from_secs(DEVICE_TTL_SECS);
                    if entry.info.online != online {
                        entry.info.online = online;
                        changed = true;
                    }
                }
                snapshot_devices(&map)
            } else {
                Vec::new()
            };
            if changed {
                let _ = app.emit("devices-updated", snapshot);
            }
        });
    }
}

fn snapshot_devices(map: &HashMap<String, DeviceEntry>) -> Vec<DeviceInfo> {
    let mut devices: Vec<DeviceInfo> = map.values().map(|entry| entry.info.clone()).collect();
    devices.sort_by(|a, b| {
        b.online
            .cmp(&a.online)
            .then_with(|| a.name.cmp(&b.name))
            .then_with(|| a.ip.cmp(&b.ip))
    });
    devices
}

fn broadcast_packet(packet: &DiscoveryPacket) -> Result<(), String> {
    let socket = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0))
        .map_err(|err| format!("绑定 UDP 广播失败: {err}"))?;
    let _ = socket.set_broadcast(true);
    let payload =
        serde_json::to_vec(packet).map_err(|err| format!("编码发现广播失败: {err}"))?;
    for target in broadcast_targets() {
        let _ = socket.send_to(&payload, SocketAddr::new(IpAddr::V4(target), DISCOVERY_PORT));
    }
    Ok(())
}

fn send_discovery_packet(packet: &DiscoveryPacket, target: SocketAddr) -> Result<(), String> {
    let socket = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0))
        .map_err(|err| format!("绑定 UDP 探测失败: {err}"))?;
    let _ = socket.set_broadcast(true);
    let payload =
        serde_json::to_vec(packet).map_err(|err| format!("编码发现探测失败: {err}"))?;
    socket
        .send_to(&payload, target)
        .map_err(|err| format!("发送发现探测失败: {err}"))?;
    Ok(())
}

fn broadcast_targets() -> Vec<Ipv4Addr> {
    let mut targets = vec![Ipv4Addr::BROADCAST];
    if let Ok(ifaces) = get_if_addrs() {
        for iface in ifaces {
            let IfAddr::V4(v4) = iface.addr else {
                continue;
            };
            if v4.ip.is_loopback() {
                continue;
            }
            targets.push(
                v4.broadcast
                    .unwrap_or_else(|| subnet_broadcast(v4.ip, v4.netmask)),
            );
        }
    }
    targets.sort();
    targets.dedup();
    targets
}

fn local_ipv4_addresses() -> Vec<Ipv4Addr> {
    let mut ips = Vec::new();
    if let Ok(ifaces) = get_if_addrs() {
        for iface in ifaces {
            let IfAddr::V4(v4) = iface.addr else {
                continue;
            };
            if !v4.ip.is_loopback() {
                ips.push(v4.ip);
            }
        }
    }
    ips.sort();
    ips.dedup();
    ips
}

fn subnet_broadcast(ip: Ipv4Addr, mask: Ipv4Addr) -> Ipv4Addr {
    let ip = u32::from(ip);
    let mask = u32::from(mask);
    Ipv4Addr::from(ip | !mask)
}

pub fn load_or_create_device_id() -> String {
    let path = device_id_path();
    if let Ok(value) = fs::read_to_string(&path) {
        let trimmed = value.trim();
        if !trimmed.is_empty() {
            return trimmed.to_string();
        }
    }

    let id = Uuid::new_v4().to_string();
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let _ = fs::write(path, &id);
    id
}

fn device_id_path() -> PathBuf {
    storage::config_dir().join("device_id")
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default()
}
