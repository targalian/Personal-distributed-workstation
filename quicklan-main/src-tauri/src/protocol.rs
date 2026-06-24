use serde::{Deserialize, Serialize};

pub const DISCOVERY_PORT: u16 = 45454;
pub const TCP_PORT: u16 = 45455;
pub const LAN_API_PORT: u16 = 45457;
pub const PRESENCE_INTERVAL_SECS: u64 = 2;
pub const LIBRARY_ANNOUNCE_INTERVAL_SECS: u64 = 600;
pub const DEVICE_TTL_SECS: u64 = 8;
pub const CHUNK_SIZE: usize = 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiscoveryPacket {
    pub app: String,
    pub version: u8,
    pub packet_type: String,
    pub device_id: String,
    pub device_name: String,
    pub tcp_port: u16,
    pub api_port: u16,
    pub library_version: i64,
    pub share_count: i64,
    pub manifest_hash: String,
    pub upload_tasks: i64,
}

impl DiscoveryPacket {
    pub fn is_quicklan(&self) -> bool {
        self.app == "quicklan" && self.version == 1
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeviceInfo {
    pub id: String,
    pub name: String,
    pub ip: String,
    pub tcp_port: u16,
    pub api_port: u16,
    pub online: bool,
    pub last_seen_ms: u128,
    pub share_count: i64,
    pub library_version: i64,
    pub manifest_hash: String,
    pub upload_tasks: i64,
    pub latency_ms: Option<u64>,
    pub note: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NetworkStatus {
    pub udp_port: u16,
    pub tcp_port: u16,
    pub api_port: u16,
    pub local_ips: Vec<String>,
    pub broadcast_targets: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SenderInfo {
    pub device_id: String,
    pub device_name: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum TcpHeader {
    QuickSend(FileHeader),
    SharedDownload(SharedDownloadRequest),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FileHeader {
    pub transfer_id: String,
    pub batch_id: String,
    pub file_name: String,
    pub file_size: u64,
    pub sha256: String,
    pub sender: SenderInfo,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SharedDownloadRequest {
    pub transfer_id: String,
    pub share_id: String,
    pub version: i64,
    pub file_hash: String,
    pub requester: SenderInfo,
    pub password: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SharedDownloadResponse {
    pub ok: bool,
    pub message: Option<String>,
    pub name: Option<String>,
    pub size: Option<u64>,
    pub file_hash: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TransferDirection {
    Sending,
    Receiving,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TransferStatus {
    Pending,
    WaitingForReceiver,
    Transferring,
    Completed,
    Rejected,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TransferInfo {
    pub id: String,
    pub batch_id: String,
    pub file_name: String,
    pub file_size: u64,
    pub bytes_done: u64,
    pub speed_bps: f64,
    pub eta_secs: Option<u64>,
    pub direction: TransferDirection,
    pub status: TransferStatus,
    pub peer_name: String,
    pub peer_ip: String,
    pub message: Option<String>,
    pub save_path: Option<String>,
    pub share_id: Option<String>,
    pub version: Option<i64>,
    pub file_hash: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IncomingTransferEvent {
    pub transfer: TransferInfo,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TransferCompletedEvent {
    pub transfer: TransferInfo,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TransferFailedEvent {
    pub transfer: TransferInfo,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShareItem {
    pub share_id: String,
    pub name: String,
    pub category: String,
    pub permission: String,
    pub owner_device_id: String,
    pub owner_name: String,
    pub latest_version: i64,
    pub file_hash: String,
    pub size: u64,
    pub created_at: i64,
    pub updated_at: i64,
    pub download_count: i64,
    pub replica_count: i64,
    pub is_local: bool,
    pub active: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShareVersion {
    pub share_id: String,
    pub version: i64,
    pub file_hash: String,
    pub size: u64,
    pub created_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[allow(dead_code)]
pub struct ReplicaNode {
    pub share_id: String,
    pub version: i64,
    pub file_hash: String,
    pub device_id: String,
    pub device_name: String,
    pub ip: String,
    pub tcp_port: u16,
    pub api_port: u16,
    pub online: bool,
    pub upload_tasks: i64,
    pub latency_ms: Option<u64>,
    pub is_local: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ManifestShare {
    pub share_id: String,
    pub name: String,
    pub category: String,
    pub permission: String,
    pub password_hash: Option<String>,
    pub owner_device_id: String,
    pub owner_name: String,
    pub latest_version: i64,
    pub versions: Vec<ShareVersion>,
    pub download_count: i64,
    pub created_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Manifest {
    pub device_id: String,
    pub device_name: String,
    pub library_version: i64,
    pub manifest_hash: String,
    pub shares: Vec<ManifestShare>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LibrarySettings {
    pub acceleration_enabled: bool,
    pub max_upload_speed: String,
    pub max_upload_tasks: i64,
    pub cache_limit_gb: i64,
}
